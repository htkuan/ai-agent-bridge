from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from agent_bridge.bridge.config import RouterConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.events import BridgeEvent, Completion, Processing, Usage
from agent_bridge.bridge.protocols import AgentController
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.bridge.session import SessionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DedupeClaim:
    """A claimed dedupe entry that the caller must release (mark completed
    or failed) once the run finishes."""

    scope: str
    canonical: str


class Bridge:
    def __init__(
        self,
        config: RouterConfig,
        session_manager: SessionManager,
        controller: AgentController,
        dedupe: PromptDedupeCache | None = None,
        *,
        named_controllers: Mapping[str, AgentController] | None = None,
        default_agent: str | None = None,
    ) -> None:
        self._config = config
        self._session_manager = session_manager
        self._controller = controller
        # Named agents (e.g. Claude profiles). ``agent=None`` routes to the
        # default ``controller``; the semaphore stays shared across all of them.
        self._named_controllers: dict[str, AgentController] = dict(
            named_controllers or {}
        )
        # When set, ``agent=None`` resolves to this *name* instead — early,
        # before the session lookup, so sessions record the actual profile and
        # a redeploy that flips the default abandons them like any remap.
        self._default_agent = default_agent
        self._sem = asyncio.Semaphore(config.max_concurrent_sessions)
        # None ⇒ feature off. Preserves pre-dedupe behaviour for tests/dev.
        self._dedupe = dedupe
        # In-memory per-session usage accumulator. Not persisted — resets on
        # restart. ``_usage_tracked`` holds the session_ids we have followed
        # from their first turn (is_new); only those get a trustworthy running
        # total. Sessions resumed without a tracked start (restart, pre-existing
        # session) are left out, so ``session_usage`` stays None for them.
        self._session_usage: dict[str, Usage] = {}
        self._usage_tracked: set[str] = set()

    def forget_session_usage(self, session_id: str) -> None:
        """Drop a session's accumulated usage (called on TTL purge)."""
        self._session_usage.pop(session_id, None)
        self._usage_tracked.discard(session_id)

    def _attach_usage(self, completion: Completion, session_id: str) -> None:
        """Set ``usage`` (this turn) and, for tracked sessions, ``session_usage``
        (running total). Untracked sessions leave ``session_usage`` None.
        """
        turn = Usage.from_completion(completion)
        completion.usage = turn
        if turn is None or session_id not in self._usage_tracked:
            return
        running = self._session_usage.get(session_id)
        running = turn if running is None else running + turn
        self._session_usage[session_id] = running
        completion.session_usage = running

    def _try_claim_dedupe(
        self, session_key: str, text: str, resumable: bool
    ) -> _DedupeClaim | Completion | None:
        """Run the cross-session dedupe gate before any session is minted.

        Returns a duplicate-detected ``Completion`` when the prompt matches a
        live entry (the caller yields it and stops), a ``_DedupeClaim`` when a
        new entry was claimed, or ``None`` when dedupe doesn't apply. Dedupe is
        skipped for non-resumable triggers (e.g. heartbeat ticks where the same
        prompt firing on a schedule is meaningful, not a duplicate).
        """
        if (
            self._dedupe is None
            or not resumable
            or ":" not in session_key
            or not text.strip()
        ):
            return None
        # session_key format is `{platform}:{scope}:{identifier}` — drop
        # the identifier so cross-thread duplicates collapse.
        scope = session_key.rpartition(":")[0]
        result = self._dedupe.lookup_or_claim(
            scope, text, first_session_key=session_key
        )
        if result.hit is None:
            return _DedupeClaim(scope=scope, canonical=result.canonical)
        state = "in_flight" if result.hit.completed_at is None else "recent_hit"
        logger.info(
            "dedupe_hit scope=%s state=%s match=%s hamming=%d "
            "first_session=%s canonical=%r",
            scope,
            state,
            "exact" if result.hamming == 0 else "simhash",
            result.hamming,
            result.hit.first_session_key,
            result.hit.canonical_text,
        )
        return Completion(
            text=":repeat: Duplicate detected — skipping.",
            is_error=False,
            metadata={
                "dedupe": state,
                "first_session_key": result.hit.first_session_key,
            },
        )

    def _effective_agent(self, agent: str | None) -> str | None:
        """The name a request actually routes (and sticks its session) to:
        an explicit ``agent`` wins, ``None`` falls back to the configured
        default name — which may itself be None (the env-built controller).
        """
        return agent if agent is not None else self._default_agent

    def _resolve_controller(self, agent: str | None) -> AgentController | Completion:
        """The controller ``agent`` names — or the error ``Completion`` to
        yield when the name isn't registered. Resolution must happen before
        the dedupe claim, session mint, and semaphore, so an unknown name
        can't poison any shared state. Startup validation makes this
        unreachable for env-built configs; the guard covers programmatically
        assembled ones.
        """
        if agent is None:
            return self._controller
        named = self._named_controllers.get(agent)
        if named is not None:
            return named
        return Completion(
            text=f"Unknown agent {agent!r} — check the server configuration.",
            is_error=True,
            metadata={"error_code": "unknown_agent"},
        )

    def _release_dedupe(self, claim: _DedupeClaim | None, *, failed: bool) -> None:
        """Release a claimed dedupe entry once its run finished.

        Failed runs (capacity reject, controller error, error Completion) drop
        the entry so retries aren't blocked for the full TTL; successful runs
        mark it completed so duplicates keep collapsing.
        """
        if claim is None or self._dedupe is None:
            return
        if failed:
            self._dedupe.mark_failed(claim.scope, claim.canonical)
        else:
            self._dedupe.mark_completed(claim.scope, claim.canonical)

    async def handle_message(
        self, request: BridgeRequest
    ) -> AsyncIterator[BridgeEvent]:
        """Resolve session, acquire a processing slot, call agent, forward events.

        If no slot is available the call yields a single error
        ``Completion`` and returns immediately — no queuing.

        ``request.system_prompt`` is opaque pass-through: built by the
        calling platform adapter, forwarded to the agent unchanged.

        ``request.resumable`` controls whether passing the same
        ``session_key`` later can resume the same session. When False, the
        bridge mints a fresh ephemeral UUID and skips the SessionManager
        entirely — the session leaves no trace on disk. Use this for
        one-shot, proactive triggers (e.g. heartbeat ticks) where each call
        is conceptually independent.

        ``request.agent`` picks a named controller; ``None`` means the
        default — the configured ``default_agent`` name when one is set,
        otherwise the env-built default ``controller``.
        """
        session_key = request.session_key
        text = request.text
        resumable = request.resumable
        agent = self._effective_agent(request.agent)
        controller = self._resolve_controller(agent)
        if isinstance(controller, Completion):
            logger.warning("Unknown agent %r for session key %s", agent, session_key)
            yield controller
            return

        # Dedupe runs before session mint to avoid wasted work.
        claim = self._try_claim_dedupe(session_key, text, resumable)
        if isinstance(claim, Completion):
            yield claim
            return

        if resumable:
            session_id, is_new = self._session_manager.get_or_create(
                session_key, agent=agent
            )
        else:
            session_id = str(uuid.uuid4())
            is_new = True

        # Mark sessions we own from the start as usage-trackable. Only these get
        # a trustworthy session_usage total; non-resumable triggers and resumes
        # of untracked sessions never accumulate.
        if resumable and is_new:
            self._usage_tracked.add(session_id)
        logger.info(
            "Session %s (new=%s, resumable=%s) for key %s — acquiring slot",
            session_id,
            is_new,
            resumable,
            session_key,
        )

        # --- Global capacity gate: no slot → reject immediately ---
        if self._sem.locked():
            logger.warning("No available slot for session %s", session_key)
            # Free the dedupe slot so the next attempt isn't blocked by a
            # run that never actually started.
            self._release_dedupe(claim, failed=True)
            yield Completion(
                text="Too many requests being processed, please try again later.",
                is_error=True,
                metadata={"error_code": "capacity_full"},
            )
            return

        await self._sem.acquire()
        yield Processing()

        # Track the last Completion's is_error so we can release the dedupe slot
        # when the run failed — otherwise a transient controller error would
        # lock out retries for the full TTL.
        last_completion_error = False
        try:
            async for event in controller.run(
                session_id,
                text,
                is_new,
                context=request.context,
                system_prompt=request.system_prompt,
            ):
                if isinstance(event, Completion):
                    last_completion_error = event.is_error
                    self._attach_usage(event, session_id)
                yield event
        except BaseException:
            # Controller raised — release the dedupe entry so retries aren't
            # blocked. Re-raise after cleanup.
            self._release_dedupe(claim, failed=True)
            raise
        else:
            # An error Completion (timeout, non-zero exit, API error, …) also
            # counts as failed, so the same alert can be retried instead of
            # getting a "recent_hit" pointer back to the failed run.
            self._release_dedupe(claim, failed=last_completion_error)
        finally:
            self._sem.release()

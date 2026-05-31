from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from agent_bridge.dedupe import PromptDedupeCache
from agent_bridge.events import BridgeEvent, Completion, Processing
from agent_bridge.protocols import AgentController
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        session_manager: SessionManager,
        controller: AgentController,
        max_concurrent: int = 5,
        dedupe: PromptDedupeCache | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._controller = controller
        self._sem = asyncio.Semaphore(max_concurrent)
        # None ⇒ feature off. Preserves pre-dedupe behaviour for tests/dev.
        self._dedupe = dedupe

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        """Resolve session, acquire a processing slot, call agent, forward events.

        If no slot is available the call yields a single error
        ``Completion`` and returns immediately — no queuing.

        ``system_prompt`` is opaque pass-through: built by the calling
        platform adapter, forwarded to the agent unchanged.

        ``resumable`` controls whether passing the same ``session_key`` later
        can resume the same session. When False, the bridge mints a fresh
        ephemeral UUID and skips the SessionManager entirely — the session
        leaves no trace on disk. Use this for one-shot, proactive triggers
        (e.g. heartbeat ticks) where each call is conceptually independent.
        """
        # --- Cross-session dedupe (before session mint to avoid wasted work) ---
        # Skip dedupe for non-resumable triggers (e.g. heartbeat ticks where
        # the same prompt firing on a schedule is meaningful, not a duplicate).
        dedupe_on = (
            self._dedupe is not None
            and resumable
            and ":" in session_key
            and bool(text.strip())
        )
        dedupe_scope: str | None = None
        dedupe_canonical: str | None = None
        if dedupe_on:
            assert self._dedupe is not None  # for type-checker; guarded above
            # session_key format is `{platform}:{scope}:{identifier}` — drop
            # the identifier so cross-thread duplicates collapse.
            dedupe_scope = session_key.rpartition(":")[0]
            result = self._dedupe.lookup_or_claim(
                dedupe_scope, text, first_session_key=session_key
            )
            if result.hit is not None:
                state = (
                    "in_flight" if result.hit.completed_at is None else "recent_hit"
                )
                logger.info(
                    "dedupe_hit scope=%s state=%s match=%s hamming=%d "
                    "first_session=%s canonical=%r",
                    dedupe_scope,
                    state,
                    "exact" if result.hamming == 0 else "simhash",
                    result.hamming,
                    result.hit.first_session_key,
                    result.hit.canonical_text,
                )
                yield Completion(
                    text=":repeat: Duplicate detected — skipping.",
                    is_error=False,
                    metadata={
                        "dedupe": state,
                        "first_session_key": result.hit.first_session_key,
                    },
                )
                return
            dedupe_canonical = result.canonical

        if resumable:
            session_id, is_new = self._session_manager.get_or_create(session_key)
        else:
            session_id = str(uuid.uuid4())
            is_new = True
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
            if dedupe_on and dedupe_canonical is not None:
                assert self._dedupe is not None and dedupe_scope is not None
                self._dedupe.mark_failed(dedupe_scope, dedupe_canonical)
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
            async for event in self._controller.run(
                session_id,
                text,
                is_new,
                context=context,
                system_prompt=system_prompt,
            ):
                if isinstance(event, Completion):
                    last_completion_error = event.is_error
                yield event
        except BaseException:
            # Controller raised — release the dedupe entry so retries aren't
            # blocked. Re-raise after cleanup.
            if dedupe_on and dedupe_canonical is not None:
                assert self._dedupe is not None and dedupe_scope is not None
                self._dedupe.mark_failed(dedupe_scope, dedupe_canonical)
            raise
        else:
            if dedupe_on and dedupe_canonical is not None:
                assert self._dedupe is not None and dedupe_scope is not None
                if last_completion_error:
                    # Controller reported failure (timeout, non-zero exit,
                    # API error, …). Drop the cache entry so the same alert
                    # can be retried instead of getting a "recent_hit" pointer
                    # back to the failed run.
                    self._dedupe.mark_failed(dedupe_scope, dedupe_canonical)
                else:
                    self._dedupe.mark_completed(dedupe_scope, dedupe_canonical)
        finally:
            self._sem.release()

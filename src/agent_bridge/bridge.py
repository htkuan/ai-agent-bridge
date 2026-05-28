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
        # None ⇒ feature off, preserves the pre-dedupe behaviour for tests/dev.
        self._dedupe = dedupe

    @staticmethod
    def _scope_of(session_key: str) -> str:
        # Channel-level scope: drop the trailing thread/identifier segment.
        # For Slack "slack:C123:1234.5678" → "slack:C123". Falls back to the
        # full key when there's only one segment.
        head, sep, _tail = session_key.rpartition(":")
        return head if sep else session_key

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
        scope = self._scope_of(session_key) if self._dedupe is not None else ""
        if self._dedupe is not None:
            thread_link = (context or {}).get("thread_permalink")
            hit = self._dedupe.lookup_or_claim(scope, text, thread_link)
            if hit is not None:
                link = hit.first_thread_link or "the original thread"
                in_flight = hit.completed_at is None
                if in_flight:
                    msg = f":repeat: Already investigating the same alert in {link}."
                    dedupe_state = "in_flight"
                else:
                    msg = (
                        f":repeat: Same alert was handled in {link} recently — "
                        "skipping duplicate run."
                    )
                    dedupe_state = "recent_hit"
                logger.info(
                    "Dedupe %s for scope=%s (link=%s)",
                    dedupe_state,
                    scope,
                    hit.first_thread_link,
                )
                yield Completion(
                    text=msg,
                    is_error=False,
                    metadata={
                        "dedupe": dedupe_state,
                        "first_thread_link": hit.first_thread_link,
                    },
                )
                return

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
            # Free the dedupe slot so the next identical attempt isn't blocked
            # by a run that never actually started.
            if self._dedupe is not None:
                self._dedupe.mark_failed(scope, text)
            yield Completion(
                text="Too many requests being processed, please try again later.",
                is_error=True,
                metadata={"error_code": "capacity_full"},
            )
            return

        await self._sem.acquire()
        yield Processing()

        try:
            async for event in self._controller.run(
                session_id,
                text,
                is_new,
                context=context,
                system_prompt=system_prompt,
            ):
                yield event
        except BaseException:
            # Controller blew up — release the dedupe entry so retries aren't
            # blocked. Re-raise after cleanup.
            if self._dedupe is not None:
                self._dedupe.mark_failed(scope, text)
            raise
        else:
            if self._dedupe is not None:
                self._dedupe.mark_completed(scope, text)
        finally:
            self._sem.release()

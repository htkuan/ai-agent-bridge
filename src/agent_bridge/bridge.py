from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

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
    ) -> None:
        self._session_manager = session_manager
        self._controller = controller
        self._sem = asyncio.Semaphore(max_concurrent)

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
        finally:
            self._sem.release()

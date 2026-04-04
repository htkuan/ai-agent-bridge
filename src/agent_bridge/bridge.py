from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from agent_bridge.events import BridgeEvent
from agent_bridge.protocols import AgentController
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        session_manager: SessionManager,
        controller: AgentController,
    ) -> None:
        self._session_manager = session_manager
        self._controller = controller

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Resolve session, call agent, forward events.

        The caller (platform adapter) is responsible for session key
        construction and per-session locking.
        """
        session_id, is_new = self._session_manager.get_or_create(session_key)
        logger.info(
            "Running session %s (new=%s) for key %s", session_id, is_new, session_key
        )
        async for event in self._controller.run(
            session_id, text, is_new, context=context
        ):
            yield event

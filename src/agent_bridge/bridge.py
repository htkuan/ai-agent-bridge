from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from agent_bridge.claude.controller import ClaudeController
from agent_bridge.claude.events import Event
from agent_bridge.claude.session import SessionManager
from agent_bridge.config import Config

logger = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        config: Config,
        session_manager: SessionManager,
        controller: ClaudeController,
    ) -> None:
        self._config = config
        self._session_manager = session_manager
        self._controller = controller
        self._locks: dict[str, asyncio.Lock] = {}

    def session_key(self, platform: str, channel: str, thread: str) -> str:
        return f"{platform}:{channel}:{thread}"

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Get or create a per-session lock (atomic via setdefault)."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def cleanup_stale_locks(self) -> int:
        """Remove locks for sessions that no longer exist. Returns count removed."""
        stale = [
            key
            for key, lock in self._locks.items()
            if not lock.locked() and self._session_manager.get(key) is None
        ]
        for key in stale:
            del self._locks[key]
        if stale:
            logger.info("Cleaned up %d stale session locks", len(stale))
        return len(stale)

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[Event]:
        """Handle an incoming message and yield Claude events.

        Caller is responsible for acquiring the session lock before calling this.
        """
        session_id, is_new = self._session_manager.get_or_create(session_key)
        logger.info("Running session %s (new=%s) for key %s", session_id, is_new, session_key)
        async for event in self._controller.run(
            session_id, text, is_new, context=context
        ):
            yield event

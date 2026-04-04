from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from agent_bridge.events import BridgeEvent


class AgentController(Protocol):
    """Interface for AI agent backends.

    An agent is purely invoked: it receives a session ID + prompt,
    loads the session, executes, and yields events.  It does not
    define session semantics or care how results are rendered.
    """

    def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...


class PlatformAdapter(Protocol):
    """Interface for chat platform frontends.

    A platform defines session semantics (e.g. Slack thread = session),
    manages per-session locking, and decides how to render agent events.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

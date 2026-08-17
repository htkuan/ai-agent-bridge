from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from agent_bridge.bridge.events import BridgeEvent


class AgentController(Protocol):
    """Interface for AI agent backends.

    An agent is purely invoked: it receives a session ID + prompt,
    loads the session, executes, and yields events.  It does not
    define session semantics or care how results are rendered.

    ``system_prompt`` is built by the platform adapter and passed through
    verbatim — the agent must not interpret platform-specific fields out
    of ``context`` to construct it.  ``context`` itself is opaque metadata
    (useful for audit/logging) and platform-defined.
    """

    def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...


class MessageRouter(Protocol):
    """Interface platform adapters send messages through.

    ``Bridge`` is the production implementation.  Adapters depend on this
    protocol — not the concrete class — so tests can substitute a fake
    that replays a scripted event stream.

    ``agent`` selects a named agent controller registered with the router;
    ``None`` routes to the default one. The platform decides which name a
    session uses (e.g. Slack's per-channel profiles); the router only
    resolves it.
    """

    def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
        agent: str | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...


class PlatformAdapter(Protocol):
    """Interface for chat platform frontends.

    A platform defines session semantics (e.g. Slack thread = session),
    manages per-session locking, and decides how to render agent events.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def cleanup(self) -> int:
        """Periodic housekeeping; returns entries removed (0 if none)."""
        ...

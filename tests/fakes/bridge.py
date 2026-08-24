from __future__ import annotations

from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion, Processing
from agent_bridge.bridge.request import BridgeRequest


class FakeBridge:
    """Scripted ``MessageRouter``: replays a fixed event sequence.

    Defaults to the minimal contract-compliant stream
    (``Processing`` → ``Completion``). ``capacity_full`` mimics the real
    bridge's rejection: a single error ``Completion`` with
    ``metadata["error_code"] == "capacity_full"`` and nothing else.
    ``known_agents`` mimics named-agent routing: when set, a call whose
    ``request.agent`` is neither None nor listed gets the real bridge's
    rejection — a single error ``Completion`` with
    ``error_code == "unknown_agent"``. Every request is recorded in
    ``calls`` verbatim.
    """

    def __init__(
        self,
        events: list[BridgeEvent] | None = None,
        *,
        capacity_full: bool = False,
        known_agents: frozenset[str] = frozenset(),
    ) -> None:
        self.events: list[BridgeEvent] = (
            events
            if events is not None
            else [Processing(), Completion(text="ok", is_error=False)]
        )
        self.capacity_full = capacity_full
        self.known_agents = known_agents
        self.calls: list[BridgeRequest] = []

    async def handle_message(
        self, request: BridgeRequest
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(request)
        if request.agent is not None and request.agent not in self.known_agents:
            yield Completion(
                text=f"Unknown agent {request.agent!r} — "
                "check the server configuration.",
                is_error=True,
                metadata={"error_code": "unknown_agent"},
            )
            return
        if self.capacity_full:
            yield Completion(
                text="Too many requests being processed, please try again later.",
                is_error=True,
                metadata={"error_code": "capacity_full"},
            )
            return
        for event in self.events:
            yield event

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion, Processing
from agent_bridge.bridge.request import BridgeRequest


class FakeBridge:
    """Scripted ``MessageRouter``: replays a fixed event sequence.

    Defaults to the minimal contract-compliant stream
    (``Processing`` → ``Completion``). Every request is recorded in ``calls``
    verbatim.

    Rejections mirror the real bridge exactly. ``capacity_full`` yields a
    single error ``Completion`` with ``error_code == "capacity_full"``;
    ``known_agents`` mimics named-agent routing, so a call whose
    ``request.agent`` is neither None nor listed gets a single error
    ``Completion`` with ``error_code == "unknown_agent"``.

    Two knobs model turns that misbehave, so no test needs its own router
    double (one that drifts from the protocol signature is worse than none):

    - ``gate`` — park the turn after its first event until the event is set,
      which is how a test holds a turn "in flight".
    - ``raises`` — blow up after the first event, the shape of any bug that
      escapes the pipeline instead of arriving as an error ``Completion``.
      With ``events=[]`` it raises before yielding anything.
    """

    def __init__(
        self,
        events: list[BridgeEvent] | None = None,
        *,
        capacity_full: bool = False,
        known_agents: frozenset[str] = frozenset(),
        gate: asyncio.Event | None = None,
        raises: bool = False,
    ) -> None:
        self.events: list[BridgeEvent] = (
            events
            if events is not None
            else [Processing(), Completion(text="ok", is_error=False)]
        )
        self.capacity_full = capacity_full
        self.known_agents = known_agents
        self.gate = gate
        self.raises = raises
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
        remaining = iter(self.events)
        first = next(remaining, None)
        if first is not None:
            yield first
        if self.gate is not None:
            await self.gate.wait()
        if self.raises:
            raise RuntimeError("fake bridge exploded")
        for event in remaining:
            yield event

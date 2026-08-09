from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent_bridge.events import BridgeEvent, Completion, Processing


@dataclass(frozen=True)
class RouterCall:
    session_key: str
    text: str
    context: dict[str, str] | None
    system_prompt: str | None
    resumable: bool


class FakeBridge:
    """Scripted ``MessageRouter``: replays a fixed event sequence.

    Defaults to the minimal contract-compliant stream
    (``Processing`` → ``Completion``). ``capacity_full`` mimics the real
    bridge's rejection: a single error ``Completion`` with
    ``metadata["error_code"] == "capacity_full"`` and nothing else.
    Every call is recorded in ``calls``.
    """

    def __init__(
        self,
        events: list[BridgeEvent] | None = None,
        *,
        capacity_full: bool = False,
    ) -> None:
        self.events: list[BridgeEvent] = (
            events
            if events is not None
            else [Processing(), Completion(text="ok", is_error=False)]
        )
        self.capacity_full = capacity_full
        self.calls: list[RouterCall] = []

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(
            RouterCall(session_key, text, context, system_prompt, resumable)
        )
        if self.capacity_full:
            yield Completion(
                text="Too many requests being processed, please try again later.",
                is_error=True,
                metadata={"error_code": "capacity_full"},
            )
            return
        for event in self.events:
            yield event

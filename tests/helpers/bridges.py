from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from agent_bridge.events import BridgeEvent, Completion


class FakeBridge:
    """Bridge stand-in for platform adapter tests.

    ``handle_message()`` records each call (as a kwargs dict in ``calls``)
    and yields the configured event sequence — default: one successful
    Completion.
    """

    def __init__(self, events: Sequence[BridgeEvent] | None = None) -> None:
        self._events: list[BridgeEvent] = (
            list(events) if events is not None else [Completion(text="ok")]
        )
        self.calls: list[dict] = []

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(
            {
                "session_key": session_key,
                "text": text,
                "context": context,
                "system_prompt": system_prompt,
                "resumable": resumable,
            }
        )
        for event in self._events:
            yield event

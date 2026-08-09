from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent_bridge.events import BridgeEvent, Completion


@dataclass(frozen=True)
class ControllerCall:
    session_id: str
    prompt: str
    is_new: bool
    context: dict[str, str] | None
    system_prompt: str | None


class FakeAgentController:
    """Scripted ``AgentController``: replays a fixed event sequence per run.

    ``scripts`` is consumed one entry per call; the last entry repeats once
    calls outnumber scripts. Every call is recorded in ``calls``.

    - ``error``: raised after yielding all but the last scripted event
      (the terminal event is never emitted) — models a controller crash.
    - ``release``: when set, the final event is held back until the test
      sets the event — used to pin a concurrency slot open deterministically.
    """

    def __init__(
        self,
        scripts: list[list[BridgeEvent]] | None = None,
        *,
        error: Exception | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.scripts: list[list[BridgeEvent]] = scripts or [
            [Completion(text="ok", is_error=False)]
        ]
        self.error = error
        self.release = release
        self.calls: list[ControllerCall] = []

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(
            ControllerCall(session_id, prompt, is_new, context, system_prompt)
        )
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        for event in script[:-1]:
            yield event
        if self.error is not None:
            raise self.error
        if self.release is not None:
            await self.release.wait()
        if script:
            yield script[-1]

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from agent_bridge.events import BridgeEvent, Completion, TextDelta


@dataclass
class RunCall:
    session_id: str
    prompt: str
    is_new: bool
    context: dict[str, str] | None
    system_prompt: str | None


class FakeAgentController:
    """In-memory AgentController for platform/bridge/integration tests.

    By default each run() echoes the prompt as one TextDelta + Completion.
    Pass ``events`` to yield a fixed BridgeEvent sequence instead, and
    ``delay`` to simulate slow agent work (e.g. for concurrency tests).
    Every invocation is recorded in ``runs``; ``cleanup_session`` calls are
    recorded in ``cleaned_up``.
    """

    def __init__(
        self,
        events: Sequence[BridgeEvent] | None = None,
        delay: float = 0.0,
    ) -> None:
        self._events = list(events) if events is not None else None
        self.delay = delay
        self.runs: list[RunCall] = []
        self.cleaned_up: list[str] = []

    @property
    def calls(self) -> list[str]:
        return [run.prompt for run in self.runs]

    @property
    def last_context(self) -> dict[str, str] | None:
        return self.runs[-1].context if self.runs else None

    @property
    def last_system_prompt(self) -> str | None:
        return self.runs[-1].system_prompt if self.runs else None

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        self.runs.append(RunCall(session_id, prompt, is_new, context, system_prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._events is not None:
            # Fresh copies: the bridge annotates Completion events in place
            # (usage/session_usage), which must not leak across runs.
            for event in self._events:
                yield copy.deepcopy(event)
        else:
            yield TextDelta(text=f"echo:{prompt}")
            yield Completion(text=f"echo:{prompt}")

    async def cleanup_session(self, session_id: str) -> None:
        self.cleaned_up.append(session_id)

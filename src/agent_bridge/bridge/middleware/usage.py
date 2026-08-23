"""Usage decoration as a pipeline stage: a pure observer of the outbound
event stream that annotates ``Completion`` events in place.

Sits between the session and capacity stages: a session is marked
usage-trackable the moment it's minted (``resumable`` and ``is_new``),
even if this particular turn is then rejected further in — the running
total stays trustworthy because a rejected turn spends nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion, Usage
from agent_bridge.bridge.pipeline import Handler, TurnContext


class UsageStage:
    """In-memory per-session accumulator. Not persisted — resets on restart.

    Only sessions followed from their first turn get a trustworthy running
    total; sessions resumed without a tracked start (process restart,
    pre-existing session) and non-resumable turns keep ``session_usage``
    None rather than reporting a partial number as if it were complete.
    """

    def __init__(self) -> None:
        self._session_usage: dict[str, Usage] = {}
        self._tracked: set[str] = set()

    def forget(self, session_id: str) -> None:
        """Drop a session's accumulated usage (called on TTL purge)."""
        self._session_usage.pop(session_id, None)
        self._tracked.discard(session_id)

    async def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        session_id = ctx.session_id
        if ctx.request.resumable and ctx.is_new and session_id is not None:
            self._tracked.add(session_id)
        async for event in call_next(ctx):
            if isinstance(event, Completion) and session_id is not None:
                self._attach(event, session_id)
            yield event

    def _attach(self, completion: Completion, session_id: str) -> None:
        """Set ``usage`` (this turn) and, for tracked sessions,
        ``session_usage`` (running total)."""
        turn = Usage.from_completion(completion)
        completion.usage = turn
        if turn is None or session_id not in self._tracked:
            return
        running = self._session_usage.get(session_id)
        running = turn if running is None else running + turn
        self._session_usage[session_id] = running
        completion.session_usage = running

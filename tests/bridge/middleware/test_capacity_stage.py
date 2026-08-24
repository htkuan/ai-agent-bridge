"""CapacityStage in isolation: the lease must come back on every exit path."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta
from agent_bridge.bridge.middleware import CapacityStage
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.request import BridgeRequest


class RecordingLease:
    def __init__(self) -> None:
        self.released = 0

    async def release(self) -> None:
        self.released += 1


class RecordingLimiter:
    """CapacityLimiter double: hand out a recording lease, or None when full."""

    def __init__(self, *, full: bool = False) -> None:
        self.full = full
        self.leases: list[RecordingLease] = []

    async def try_acquire(self) -> RecordingLease | None:
        if self.full:
            return None
        lease = RecordingLease()
        self.leases.append(lease)
        return lease


def _ctx() -> TurnContext:
    return TurnContext(request=BridgeRequest(session_key="slack:C1:t1", text="hi"))


def _next(events: list[BridgeEvent], *, error: Exception | None = None) -> Handler:
    async def handler(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        for event in events:
            yield event
        if error is not None:
            raise error

    return handler


async def test_full_limiter_rejects_without_calling_next():
    downstream_ran: list[bool] = []

    async def recording_next(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        downstream_ran.append(True)
        yield Completion(text="never")

    stage = CapacityStage(RecordingLimiter(full=True))
    events = [e async for e in stage(_ctx(), recording_next)]

    assert downstream_ran == []
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, Completion)
    assert only.is_error is True
    assert only.metadata["error_code"] == "capacity_full"


async def test_forwards_events_and_releases_on_normal_end():
    limiter = RecordingLimiter()
    stage = CapacityStage(limiter)

    events = [
        e
        async for e in stage(
            _ctx(), _next([TextDelta(text="a"), Completion(text="ok")])
        )
    ]

    # Forwarded verbatim — the stage adds nothing (Processing is the core's).
    assert [type(e) for e in events] == [TextDelta, Completion]
    assert limiter.leases[0].released == 1


async def test_releases_on_downstream_exception():
    limiter = RecordingLimiter()
    stage = CapacityStage(limiter)
    with pytest.raises(RuntimeError, match="crash"):
        async for _ in stage(_ctx(), _next([], error=RuntimeError("crash"))):
            pass
    assert limiter.leases[0].released == 1


async def test_releases_on_abandoned_stream():
    limiter = RecordingLimiter()
    stage = CapacityStage(limiter)
    agen = stage(_ctx(), _next([TextDelta(text="a"), Completion(text="ok")]))
    await anext(agen)
    await agen.aclose()
    assert limiter.leases[0].released == 1

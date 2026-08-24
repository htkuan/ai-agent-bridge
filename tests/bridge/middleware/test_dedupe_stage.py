"""DedupeStage in isolation: claim lifecycle around a scripted downstream.

The recording cache fake pins the *stage's* obligations — when it looks
up, which token it releases, and on which exit paths — independent of any
real matching algorithm (that's the DedupeCache contract suite's job).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta
from agent_bridge.bridge.middleware import DedupeStage
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.protocols import DedupeDecision, DedupeHit
from agent_bridge.bridge.request import BridgeRequest


class RecordingCache:
    """DedupeCache double: scripted decision, recorded lookups/releases."""

    def __init__(self, hit: DedupeHit | None = None) -> None:
        self.hit = hit
        self.lookups: list[tuple[str, str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    async def lookup_or_claim(
        self, scope: str, text: str, first_session_key: str
    ) -> DedupeDecision:
        self.lookups.append((scope, text, first_session_key))
        return DedupeDecision(hit=self.hit, claim_token="token-1")

    async def mark_completed(self, scope: str, claim_token: str) -> None:
        self.completed.append((scope, claim_token))

    async def mark_failed(self, scope: str, claim_token: str) -> None:
        self.failed.append((scope, claim_token))


def _ctx(key: str = "slack:C1:t1", text: str = "alert", **kw) -> TurnContext:
    return TurnContext(request=BridgeRequest(session_key=key, text=text, **kw))


def _next(events: list[BridgeEvent], *, error: Exception | None = None) -> Handler:
    async def handler(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        for event in events:
            yield event
        if error is not None:
            raise error

    return handler


async def test_claims_with_scope_stripped_of_identifier():
    cache = RecordingCache()
    stage = DedupeStage(cache)
    async for _ in stage(_ctx("slack:C1:t1", "alert"), _next([Completion(text="ok")])):
        pass
    assert cache.lookups == [("slack:C1", "alert", "slack:C1:t1")]


@pytest.mark.parametrize(
    ("key", "text", "resumable"),
    [
        ("heartbeat:tick:T1", "scheduled", False),  # non-resumable triggers
        ("no-colons", "alert", True),  # key without platform:scope shape
        ("slack:C1:t1", "   ", True),  # blank prompt
    ],
)
async def test_skips_without_touching_the_cache(key: str, text: str, resumable: bool):
    cache = RecordingCache()
    stage = DedupeStage(cache)
    ctx = _ctx(key, text, resumable=resumable)

    events = [e async for e in stage(ctx, _next([Completion(text="ran")]))]

    assert cache.lookups == []
    assert [type(e) for e in events] == [Completion]


async def test_duplicate_short_circuits_without_calling_next():
    hit = DedupeHit(
        first_session_key="slack:C1:t0", in_flight=True, matched_text="alert"
    )
    cache = RecordingCache(hit=hit)
    stage = DedupeStage(cache)
    downstream_ran: list[bool] = []

    async def recording_next(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        downstream_ran.append(True)
        yield Completion(text="never")

    events = [e async for e in stage(_ctx(), recording_next)]

    assert downstream_ran == []
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, Completion)
    assert only.is_error is False
    assert only.metadata["dedupe"] == "in_flight"
    assert only.metadata["first_session_key"] == "slack:C1:t0"
    # A hit is not a claim: nothing to release.
    assert cache.completed == [] and cache.failed == []


async def test_recent_hit_state_when_first_run_completed():
    hit = DedupeHit(
        first_session_key="slack:C1:t0", in_flight=False, matched_text="alert"
    )
    stage = DedupeStage(RecordingCache(hit=hit))
    events = [e async for e in stage(_ctx(), _next([]))]
    assert events[0].metadata["dedupe"] == "recent_hit"


async def test_clean_run_marks_completed():
    cache = RecordingCache()
    stage = DedupeStage(cache)
    async for _ in stage(
        _ctx(), _next([TextDelta(text="…"), Completion(text="ok", is_error=False)])
    ):
        pass
    assert cache.completed == [("slack:C1", "token-1")]
    assert cache.failed == []


async def test_error_completion_marks_failed():
    """Timeout / non-zero exit / API error arrive as is_error Completions —
    they must not lock retries out for the full TTL."""
    cache = RecordingCache()
    stage = DedupeStage(cache)
    async for _ in stage(_ctx(), _next([Completion(text="boom", is_error=True)])):
        pass
    assert cache.failed == [("slack:C1", "token-1")]
    assert cache.completed == []


async def test_downstream_exception_marks_failed_and_propagates():
    cache = RecordingCache()
    stage = DedupeStage(cache)
    with pytest.raises(RuntimeError, match="crash"):
        async for _ in stage(
            _ctx(), _next([TextDelta(text="…")], error=RuntimeError("crash"))
        ):
            pass
    assert cache.failed == [("slack:C1", "token-1")]


async def test_abandoned_stream_marks_failed():
    """The platform stopping mid-stream closes the generator chain; the
    finally must still release the claim so retries aren't blocked."""
    cache = RecordingCache()
    stage = DedupeStage(cache)
    agen = stage(_ctx(), _next([TextDelta(text="a"), Completion(text="ok")]))
    assert isinstance(await anext(agen), TextDelta)
    await agen.aclose()
    assert cache.failed == [("slack:C1", "token-1")]


async def test_inner_capacity_reject_marks_failed():
    """The reject Completion from a capacity stage further in flows out
    through this stage — the composition, not extra wiring, releases the
    claim."""
    cache = RecordingCache()
    stage = DedupeStage(cache)
    reject = Completion(
        text="Too many requests being processed, please try again later.",
        is_error=True,
        metadata={"error_code": "capacity_full"},
    )
    events = [e async for e in stage(_ctx(), _next([reject]))]
    assert events == [reject]
    assert cache.failed == [("slack:C1", "token-1")]

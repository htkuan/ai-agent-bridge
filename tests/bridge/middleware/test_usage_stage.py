"""UsageStage in isolation: decoration, tracking rules, forget()."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta
from agent_bridge.bridge.middleware import UsageStage
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.request import BridgeRequest


def _usage_completion(cost_usd: float = 0.01) -> Completion:
    return Completion(
        text="ok",
        cost_usd=cost_usd,
        duration_ms=100,
        metadata={
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "num_turns": 1,
            }
        },
    )


def _ctx(
    session_id: str = "sid-1", *, is_new: bool = True, resumable: bool = True
) -> TurnContext:
    ctx = TurnContext(
        request=BridgeRequest(session_key="slack:C1:t1", text="hi", resumable=resumable)
    )
    ctx.session_id = session_id
    ctx.is_new = is_new
    return ctx


def _next(events: list[BridgeEvent]) -> Handler:
    async def handler(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        for event in events:
            yield event

    return handler


async def _final(stage: UsageStage, ctx: TurnContext, events: list[BridgeEvent]) -> Any:
    out = [e async for e in stage(ctx, _next(events))]
    return out[-1]


async def test_attaches_turn_usage():
    stage = UsageStage()
    completion = await _final(stage, _ctx(), [_usage_completion(cost_usd=0.02)])
    assert completion.usage is not None
    assert completion.usage.input_tokens == 10
    assert completion.usage.cost_usd == 0.02
    assert completion.usage.duration_ms == 100


async def test_session_usage_accumulates_for_tracked_sessions():
    stage = UsageStage()
    first = await _final(stage, _ctx(is_new=True), [_usage_completion()])
    assert first.session_usage is not None
    assert first.session_usage.input_tokens == 10

    second = await _final(stage, _ctx(is_new=False), [_usage_completion()])
    assert second.session_usage.input_tokens == 20
    assert second.session_usage.num_turns == 2
    # Turn usage stays per-turn, not cumulative.
    assert second.usage.input_tokens == 10


async def test_untracked_resume_gets_no_session_total():
    """A session first seen mid-life (restart, pre-existing) would report a
    partial total as if complete — None is the honest answer."""
    stage = UsageStage()
    completion = await _final(stage, _ctx(is_new=False), [_usage_completion()])
    assert completion.usage is not None
    assert completion.session_usage is None


async def test_non_resumable_turns_are_never_tracked():
    stage = UsageStage()
    completion = await _final(
        stage, _ctx(is_new=True, resumable=False), [_usage_completion()]
    )
    assert completion.usage is not None
    assert completion.session_usage is None


async def test_completion_without_usage_metadata_left_unannotated():
    stage = UsageStage()
    completion = await _final(stage, _ctx(), [Completion(text="bridge-minted")])
    assert completion.usage is None
    assert completion.session_usage is None


async def test_forget_resets_tracking():
    stage = UsageStage()
    await _final(stage, _ctx(is_new=True), [_usage_completion()])

    stage.forget("sid-1")

    # Now untracked: the next turn carries usage but no running total.
    completion = await _final(stage, _ctx(is_new=False), [_usage_completion()])
    assert completion.session_usage is None


async def test_non_completion_events_pass_through_untouched():
    stage = UsageStage()
    delta = TextDelta(text="chunk")
    events = [e async for e in stage(_ctx(), _next([delta, _usage_completion()]))]
    assert events[0] is delta

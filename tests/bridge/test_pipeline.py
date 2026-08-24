"""The pipeline machinery itself: compose order, short-circuit, the core."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent_bridge.bridge.events import BridgeEvent, Completion, Processing, TextDelta
from agent_bridge.bridge.pipeline import (
    Handler,
    TurnContext,
    compose,
    run_agent,
)
from agent_bridge.bridge.request import BridgeRequest
from tests.fakes import FakeAgentController


def _ctx(**kw) -> TurnContext:
    return TurnContext(request=BridgeRequest(session_key="t:s:1", text="hi", **kw))


def _tagger(name: str):
    """A middleware that tags the stream on the way in and out."""

    async def stage(ctx: TurnContext, call_next: Handler) -> AsyncIterator[BridgeEvent]:
        yield TextDelta(text=f"enter:{name}")
        async for event in call_next(ctx):
            yield event
        yield TextDelta(text=f"exit:{name}")

    return stage


async def _core_two_events(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
    yield TextDelta(text="core")
    yield Completion(text="done")


async def test_compose_first_middleware_is_outermost():
    handler = compose([_tagger("outer"), _tagger("inner")], _core_two_events)
    events = [e async for e in handler(_ctx())]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["enter:outer", "enter:inner", "core", "exit:inner", "exit:outer"]


async def test_compose_empty_chain_is_the_core():
    handler = compose([], _core_two_events)
    events = [e async for e in handler(_ctx())]
    assert [type(e) for e in events] == [TextDelta, Completion]


async def test_short_circuit_skips_inner_stages():
    inner_ran: list[bool] = []

    async def short_circuit(
        ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        yield Completion(text="stopped here", is_error=True)

    async def recording_core(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        inner_ran.append(True)
        yield Completion(text="never")

    handler = compose([_tagger("outer"), short_circuit], recording_core)
    events = [e async for e in handler(_ctx())]

    assert inner_ran == []
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert completions[0].text == "stopped here"
    # The outer stage still saw the short-circuit flow through it.
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["enter:outer", "exit:outer"]


# --- run_agent (the core) ---


async def test_core_yields_processing_then_controller_events():
    controller = FakeAgentController([[TextDelta(text="hi"), Completion(text="hi")]])
    ctx = _ctx()
    ctx.controller = controller
    ctx.session_id = "sid-1"
    ctx.is_new = True

    events = [e async for e in run_agent(ctx)]
    assert [type(e) for e in events] == [Processing, TextDelta, Completion]


async def test_core_forwards_request_fields_to_controller():
    controller = FakeAgentController()
    ctx = TurnContext(
        request=BridgeRequest(
            session_key="t:s:1",
            text="[alice]: hi",
            context={"user": "alice"},
            system_prompt="be brief",
        )
    )
    ctx.controller = controller
    ctx.session_id = "sid-9"
    ctx.is_new = False

    async for _ in run_agent(ctx):
        pass

    call = controller.calls[0]
    assert call.session_id == "sid-9"
    assert call.prompt == "[alice]: hi"
    assert call.is_new is False
    assert call.context == {"user": "alice"}
    assert call.system_prompt == "be brief"


async def test_core_guards_against_missing_resolution_stages():
    # A pipeline assembled without the resolution stages is a wiring bug —
    # fail loudly, don't invent defaults.
    with pytest.raises(RuntimeError, match="resolution stages"):
        async for _ in run_agent(_ctx()):
            pass

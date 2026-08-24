"""AgentResolutionStage + SessionResolutionStage in isolation, each against
a scripted downstream handler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta
from agent_bridge.bridge.middleware import AgentResolutionStage, SessionResolutionStage
from agent_bridge.bridge.pipeline import TurnContext
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.bridge.session import SessionManager
from tests.fakes import FakeAgentController, InMemorySessionStore


def _ctx(**kw) -> TurnContext:
    return TurnContext(
        request=BridgeRequest(session_key="slack:C1:t1", text="hi", **kw)
    )


def _next(seen: list[TurnContext]):
    """Downstream handler that records the enriched ctx and yields one event."""

    async def handler(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        seen.append(ctx)
        yield Completion(text="inner")

    return handler


# --- AgentResolutionStage ---


async def test_agent_none_resolves_to_default_controller():
    default = FakeAgentController()
    stage = AgentResolutionStage(default, {}, default_agent=None)
    seen: list[TurnContext] = []
    ctx = _ctx()

    events = [e async for e in stage(ctx, _next(seen))]

    assert len(seen) == 1
    assert ctx.agent is None
    assert ctx.controller is default
    assert [type(e) for e in events] == [Completion]


async def test_named_agent_resolves_to_named_controller():
    default = FakeAgentController()
    research = FakeAgentController()
    stage = AgentResolutionStage(default, {"research": research}, default_agent=None)
    seen: list[TurnContext] = []
    ctx = _ctx(agent="research")

    async for _ in stage(ctx, _next(seen)):
        pass

    assert ctx.agent == "research"
    assert ctx.controller is research


async def test_default_agent_name_fills_ctx_agent():
    """agent=None resolves to the configured *name*, so the session stage
    further in sticks the session to the actual profile."""
    fast = FakeAgentController()
    stage = AgentResolutionStage(FakeAgentController(), {"fast": fast}, "fast")
    ctx = _ctx()

    async for _ in stage(ctx, _next([])):
        pass

    assert ctx.agent == "fast"
    assert ctx.controller is fast


async def test_unknown_agent_short_circuits_without_calling_next():
    stage = AgentResolutionStage(FakeAgentController(), {}, default_agent=None)
    seen: list[TurnContext] = []

    events = [e async for e in stage(_ctx(agent="nope"), _next(seen))]

    assert seen == []
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, Completion)
    assert only.is_error is True
    assert only.metadata["error_code"] == "unknown_agent"


async def test_unknown_default_agent_short_circuits_too():
    stage = AgentResolutionStage(FakeAgentController(), {}, default_agent="ghost")
    events = [e async for e in stage(_ctx(), _next([]))]
    assert len(events) == 1
    assert events[0].metadata["error_code"] == "unknown_agent"


async def test_forwards_downstream_events_verbatim():
    stage = AgentResolutionStage(FakeAgentController(), {}, None)

    async def chatty(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        yield TextDelta(text="a")
        yield Completion(text="b")

    events = [e async for e in stage(_ctx(), chatty)]
    assert [type(e) for e in events] == [TextDelta, Completion]


# --- SessionResolutionStage ---


def _session_stage(tmp_path: Path) -> tuple[SessionResolutionStage, SessionManager]:
    manager = SessionManager(
        SessionConfig(store_path=tmp_path / "s.json"), store=InMemorySessionStore()
    )
    return SessionResolutionStage(manager), manager


async def test_resumable_creates_then_resumes(tmp_path: Path):
    stage, _ = _session_stage(tmp_path)

    first = _ctx()
    async for _ in stage(first, _next([])):
        pass
    assert first.is_new is True
    assert first.session_id is not None

    second = _ctx()
    async for _ in stage(second, _next([])):
        pass
    assert second.is_new is False
    assert second.session_id == first.session_id


async def test_session_sticks_to_resolved_agent(tmp_path: Path):
    """The stage passes ctx.agent (set by resolution) into the manager, so a
    remapped key mints a fresh session."""
    stage, _ = _session_stage(tmp_path)

    first = _ctx()
    first.agent = "research"
    async for _ in stage(first, _next([])):
        pass

    second = _ctx()
    second.agent = "ops"
    async for _ in stage(second, _next([])):
        pass

    assert second.is_new is True
    assert second.session_id != first.session_id


async def test_non_resumable_mints_ephemeral_uuid(tmp_path: Path):
    stage, manager = _session_stage(tmp_path)

    first = _ctx(resumable=False)
    async for _ in stage(first, _next([])):
        pass
    second = _ctx(resumable=False)
    async for _ in stage(second, _next([])):
        pass

    assert first.is_new and second.is_new
    assert first.session_id != second.session_id
    assert first.session_id is not None
    assert len(first.session_id) == 36  # UUID-shaped
    # Nothing was persisted for the ephemeral turns.
    assert await manager.list_sessions() == {}

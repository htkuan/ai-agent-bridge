"""Contract suite for the ``MessageRouter`` protocol.

The real ``Bridge`` and ``FakeBridge`` must satisfy the same expectations:
a normal run opens with ``Processing`` and ends with exactly one terminal
``Completion``; a saturated router rejects with a single error ``Completion``
carrying ``error_code == "capacity_full"``; an unknown ``agent`` rejects with
a single error ``Completion`` carrying ``error_code == "unknown_agent"``,
while a registered one routes normally.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agent_bridge.bridge.config import RouterConfig
from agent_bridge.bridge.events import BridgeEvent, Completion, Processing, TextDelta
from agent_bridge.bridge.protocols import MessageRouter
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from tests.fakes import FakeAgentController, FakeBridge


@pytest.fixture(params=["bridge", "fake"])
def router(
    request: pytest.FixtureRequest, session_manager: SessionManager
) -> MessageRouter:
    script: list[BridgeEvent] = [
        TextDelta(text="hi"),
        Completion(text="hi", is_error=False),
    ]
    if request.param == "bridge":
        return Bridge(RouterConfig(), session_manager, FakeAgentController([script]))
    return FakeBridge([Processing(), *script])


@pytest.fixture(params=["bridge", "fake"])
async def saturated_router(
    request: pytest.FixtureRequest, session_manager: SessionManager
) -> AsyncIterator[MessageRouter]:
    if request.param == "fake":
        yield FakeBridge(capacity_full=True)
        return
    release = asyncio.Event()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=1),
        session_manager,
        FakeAgentController(release=release),
    )
    # Occupy the only slot: advance a stream past Processing; the controller
    # holds its terminal event until ``release`` is set, so the semaphore
    # stays taken while the suspended generator is alive.
    holder = bridge.handle_message("slack:c1:hold", "occupy")
    async for event in holder:
        if isinstance(event, Processing):
            break
    yield bridge
    release.set()
    async for _ in holder:
        pass


async def test_normal_flow_opens_processing_ends_completion(
    router: MessageRouter,
) -> None:
    events = [e async for e in router.handle_message("slack:c1:t1", "hi")]
    assert isinstance(events[0], Processing)
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert isinstance(events[-1], Completion)
    assert completions[0].is_error is False


async def test_capacity_rejection_is_a_single_error_completion(
    saturated_router: MessageRouter,
) -> None:
    events = [e async for e in saturated_router.handle_message("slack:c1:t2", "hi")]
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, Completion)
    assert only.is_error is True
    assert only.metadata.get("error_code") == "capacity_full"


@pytest.fixture(params=["bridge", "fake"])
def named_router(
    request: pytest.FixtureRequest, session_manager: SessionManager
) -> MessageRouter:
    script: list[BridgeEvent] = [Completion(text="ok", is_error=False)]
    if request.param == "bridge":
        return Bridge(
            RouterConfig(),
            session_manager,
            FakeAgentController([script]),
            named_controllers={"research": FakeAgentController([script])},
        )
    return FakeBridge([Processing(), *script], known_agents=frozenset({"research"}))


async def test_unknown_agent_is_a_single_error_completion(
    named_router: MessageRouter,
) -> None:
    events = [
        e async for e in named_router.handle_message("slack:c1:t3", "hi", agent="nope")
    ]
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, Completion)
    assert only.is_error is True
    assert only.metadata.get("error_code") == "unknown_agent"


async def test_known_agent_routes_normally(named_router: MessageRouter) -> None:
    events = [
        e
        async for e in named_router.handle_message(
            "slack:c1:t4", "hi", agent="research"
        )
    ]
    assert isinstance(events[0], Processing)
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False

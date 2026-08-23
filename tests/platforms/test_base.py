"""BasePlatformAdapter: the shared pre-process → forward → post-process turn."""

from __future__ import annotations

from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.base import (
    BasePlatformAdapter,
    BridgeRequest,
    make_session_key,
)
from tests.fakes import FakeBridge

type _Log = list[tuple[str, BridgeEvent | None]]


class RecordingAdapter(BasePlatformAdapter[_Log]):
    """Overrides every specific hook; records (hook name, event) into state."""

    async def on_processing(self, state: _Log, event: Processing) -> None:
        state.append(("processing", event))

    async def on_text_delta(self, state: _Log, event: TextDelta) -> None:
        state.append(("text_delta", event))

    async def on_status_update(self, state: _Log, event: StatusUpdate) -> None:
        state.append(("status_update", event))

    async def on_user_question(self, state: _Log, event: UserQuestion) -> None:
        state.append(("user_question", event))

    async def on_completion(self, state: _Log, event: Completion) -> None:
        state.append(("completion", event))

    async def on_stream_end(self, state: _Log) -> None:
        state.append(("stream_end", None))


class CatchAllAdapter(BasePlatformAdapter[_Log]):
    """Overrides only the catch-all, like a log-everything platform."""

    async def on_event(self, state: _Log, event: BridgeEvent) -> None:
        state.append(("event", event))


def _request() -> BridgeRequest:
    return BridgeRequest(session_key="test:scope:1", text="hi")


_ALL_EVENTS: list[BridgeEvent] = [
    Processing(),
    TextDelta(text="hello"),
    StatusUpdate(status="Using Bash...", detail="ls"),
    UserQuestion(questions=[{"question": "which?"}]),
    Completion(text="done"),
]


async def test_process_dispatches_each_event_to_its_hook():
    log: _Log = []
    final = await RecordingAdapter(FakeBridge(list(_ALL_EVENTS))).process(
        _request(), log
    )
    assert [name for name, _ in log] == [
        "processing",
        "text_delta",
        "status_update",
        "user_question",
        "completion",
    ]
    # Hooks receive the whole event objects, unmodified.
    assert [event for _, event in log] == _ALL_EVENTS
    assert final is _ALL_EVENTS[-1]


async def test_stream_without_completion_runs_stream_end():
    bridge = FakeBridge([Processing(), TextDelta(text="partial")])
    log: _Log = []
    final = await RecordingAdapter(bridge).process(_request(), log)
    assert final is None
    assert log[-1] == ("stream_end", None)


async def test_stream_end_skipped_when_completion_arrived():
    log: _Log = []
    await RecordingAdapter(FakeBridge()).process(_request(), log)
    assert all(name != "stream_end" for name, _ in log)


async def test_default_hooks_route_to_on_event():
    log: _Log = []
    await CatchAllAdapter(FakeBridge(list(_ALL_EVENTS))).process(_request(), log)
    assert [event for _, event in log] == _ALL_EVENTS


async def test_default_on_event_noops():
    final = await BasePlatformAdapter[None](FakeBridge()).process(_request(), None)
    assert isinstance(final, Completion)


async def test_request_forwarded_verbatim():
    bridge = FakeBridge(known_agents=frozenset({"researcher"}))
    request = BridgeRequest(
        session_key="tg:chat1:2",
        text="[alice]: hi",
        context={"user": "alice"},
        system_prompt="be brief",
        resumable=False,
        agent="researcher",
    )
    await BasePlatformAdapter[None](bridge).process(request, None)
    # The router receives the request object itself — no lossy re-packing.
    assert bridge.calls == [request]


async def test_request_defaults():
    bridge = FakeBridge()
    await BasePlatformAdapter[None](bridge).process(_request(), None)
    call = bridge.calls[0]
    assert call.context is None
    assert call.system_prompt is None
    assert call.resumable is True
    assert call.agent is None


async def test_default_lifecycle_and_cleanup():
    adapter = BasePlatformAdapter[None](FakeBridge())
    await adapter.start()
    await adapter.stop()
    assert await adapter.cleanup() == 0


def test_make_session_key_format():
    assert make_session_key("slack", "C1", "123.45") == "slack:C1:123.45"

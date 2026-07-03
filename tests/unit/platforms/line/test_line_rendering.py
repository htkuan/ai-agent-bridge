from __future__ import annotations

import asyncio
from typing import Any

from agent_bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.line.adapter import LINE_MSG_MAX_CHARS, LineAdapter
from agent_bridge.platforms.line.config import LineConfig
from tests.helpers import FakeBridge

REPLY = "/v2/bot/message/reply"
PUSH = "/v2/bot/message/push"


class _ApiRecorder:
    """Stands in for LineAdapter._api_post; records every Messaging API call."""

    def __init__(self, *, reply_ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reply_ok = reply_ok

    async def __call__(self, path: str, payload: dict) -> bool:
        self.calls.append((path, payload))
        # False on REPLY simulates an expired/already-used reply token (400)
        return self.reply_ok or path != REPLY

    def named(self, path: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == path]


def _make_adapter(
    events: list[BridgeEvent] | None = None, *, reply_ok: bool = True
) -> tuple[LineAdapter, _ApiRecorder, FakeBridge]:
    bridge = FakeBridge(events)
    adapter = LineAdapter(
        LineConfig(channel_secret="sec", channel_access_token="tok"),
        bridge,  # type: ignore[arg-type]
    )
    recorder = _ApiRecorder(reply_ok=reply_ok)
    adapter._api_post = recorder  # type: ignore[method-assign]
    return adapter, recorder, bridge


def _event(text: str = "hello", *, reply_token: str | None = "rt-1") -> dict:
    event = {
        "type": "message",
        "source": {"type": "user", "userId": "U123"},
        "message": {"id": "m1", "type": "text", "text": text},
    }
    if reply_token is not None:
        event["replyToken"] = reply_token
    return event


async def _run(adapter: LineAdapter, event: dict, text: str) -> None:
    await adapter._handle_event(event, text)


def _texts(messages: list[dict]) -> list[str]:
    return [m["text"] for m in messages]


async def test_completion_replies_with_buffered_deltas():
    adapter, api, _ = _make_adapter(
        [
            Processing(),
            TextDelta(text="part one"),
            TextDelta(text="part two"),
            Completion(text="ignored — deltas win"),
        ]
    )
    await _run(adapter, _event(), "hello")

    replies = api.named(REPLY)
    assert len(replies) == 1
    assert replies[0]["replyToken"] == "rt-1"
    assert _texts(replies[0]["messages"]) == ["part one\n\npart two"]
    assert api.named(PUSH) == []


async def test_completion_without_deltas_uses_completion_text():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="final answer")])
    await _run(adapter, _event(), "hello")
    assert _texts(api.named(REPLY)[0]["messages"]) == ["final answer"]


async def test_status_updates_are_log_only():
    adapter, api, _ = _make_adapter(
        [Processing(), StatusUpdate(status="Using Bash..."), Completion(text="ok")]
    )
    await _run(adapter, _event(), "hello")
    # Exactly one API call: the final reply. No placeholder, no status message.
    assert [name for name, _ in api.calls] == [REPLY]


async def test_error_completion_prefixed():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="boom", is_error=True)])
    await _run(adapter, _event(), "hello")
    assert _texts(api.named(REPLY)[0]["messages"]) == ["❌ boom"]


async def test_lone_error_completion_still_replies():
    # Capacity-full short-circuit: single Completion, no Processing.
    adapter, api, _ = _make_adapter([Completion(text="capacity full", is_error=True)])
    await _run(adapter, _event(), "hello")
    assert _texts(api.named(REPLY)[0]["messages"]) == ["❌ capacity full"]


async def test_empty_completion_renders_fallback_notice():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="")])
    await _run(adapter, _event(), "hello")
    assert _texts(api.named(REPLY)[0]["messages"]) == ["No response from agent."]


async def test_user_question_rendered_at_completion():
    adapter, api, _ = _make_adapter(
        [
            Processing(),
            UserQuestion(questions=[{"question": "Proceed?", "options": ["yes"]}]),
            Completion(text=""),
        ]
    )
    await _run(adapter, _event(), "hello")

    final = _texts(api.named(REPLY)[0]["messages"])[0]
    assert "Proceed?" in final
    assert "• yes" in final


async def test_long_text_splits_within_single_reply():
    long_text = "a" * (LINE_MSG_MAX_CHARS + 500)
    adapter, api, _ = _make_adapter([Processing(), Completion(text=long_text)])
    await _run(adapter, _event(), "hello")

    replies = api.named(REPLY)
    assert len(replies) == 1
    assert _texts(replies[0]["messages"]) == ["a" * LINE_MSG_MAX_CHARS, "a" * 500]
    assert api.named(PUSH) == []


async def test_overflow_beyond_five_chunks_pushed_in_batches():
    # 7 chunks: 5 in the reply, 2 in one push batch.
    long_text = "a" * (LINE_MSG_MAX_CHARS * 6 + 500)
    adapter, api, _ = _make_adapter([Processing(), Completion(text=long_text)])
    await _run(adapter, _event(), "hello")

    replies = api.named(REPLY)
    assert len(replies) == 1
    assert len(replies[0]["messages"]) == 5

    pushes = api.named(PUSH)
    assert len(pushes) == 1
    assert pushes[0]["to"] == "U123"
    assert _texts(pushes[0]["messages"]) == ["a" * LINE_MSG_MAX_CHARS, "a" * 500]


async def test_push_overflow_batches_of_five():
    # 12 chunks: reply 5, push 5, push 2.
    long_text = "a" * (LINE_MSG_MAX_CHARS * 11 + 500)
    adapter, api, _ = _make_adapter([Processing(), Completion(text=long_text)])
    await _run(adapter, _event(), "hello")

    pushes = api.named(PUSH)
    assert [len(p["messages"]) for p in pushes] == [5, 2]


async def test_reply_failure_falls_back_to_push():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="the answer")], reply_ok=False)
    await _run(adapter, _event(), "hello")

    # Reply was attempted first, then everything went out via push.
    assert [name for name, _ in api.calls] == [REPLY, PUSH]
    push = api.named(PUSH)[0]
    assert push["to"] == "U123"
    assert _texts(push["messages"]) == ["the answer"]


async def test_missing_reply_token_pushes_directly():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="the answer")])
    await _run(adapter, _event(reply_token=None), "hello")

    assert api.named(REPLY) == []
    assert _texts(api.named(PUSH)[0]["messages"]) == ["the answer"]


async def test_bridge_receives_tagged_prompt_context_and_system_prompt():
    adapter, _, bridge = _make_adapter()
    await _run(adapter, _event("do the thing"), "do the thing")

    call = bridge.calls[0]
    assert call["session_key"] == "line:user:U123"
    assert call["text"] == "[U123]: do the thing"
    assert call["resumable"] is True
    assert call["context"] == {
        "platform": "line",
        "source_type": "user",
        "chat_id": "U123",
        "user_id": "U123",
    }
    assert "LINE" in call["system_prompt"]


async def test_group_event_scopes_session_to_group():
    adapter, api, bridge = _make_adapter([Processing(), Completion(text="ok")], reply_ok=False)
    event = _event()
    event["source"] = {"type": "group", "groupId": "G456", "userId": "U123"}
    await _run(adapter, event, "hello")

    assert bridge.calls[0]["session_key"] == "line:group:G456"
    # Push fallback targets the group, not the sender.
    assert api.named(PUSH)[0]["to"] == "G456"


async def test_bridge_exception_logged_not_raised():
    class _BoomBridge:
        async def handle_message(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield  # unreachable — makes this an async generator

    adapter, _, _ = _make_adapter()
    adapter._bridge = _BoomBridge()  # type: ignore[assignment]
    await _run(adapter, _event(), "hello")  # must not raise


def test_cleanup_stale_sessions_drops_expired_locks(session_manager):
    adapter = LineAdapter(
        LineConfig(channel_secret="sec", channel_access_token="tok"),
        FakeBridge(),  # type: ignore[arg-type]
        session_manager=session_manager,
    )
    session_manager.get_or_create("line:user:U1")
    adapter._locks["line:user:U1"] = asyncio.Lock()
    adapter._locks["line:user:U2"] = asyncio.Lock()

    removed = adapter.cleanup_stale_sessions()

    assert removed == 1
    assert "line:user:U1" in adapter._locks
    assert "line:user:U2" not in adapter._locks

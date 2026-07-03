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
from agent_bridge.platforms.telegram.adapter import (
    PROCESSING_TEXT,
    TELEGRAM_MSG_MAX_CHARS,
    TelegramAdapter,
)
from agent_bridge.platforms.telegram.config import TelegramConfig
from tests.helpers import FakeBridge


class _ApiRecorder:
    """Stands in for TelegramAdapter._api_call; records every Bot API call."""

    def __init__(self, *, reject_parse_mode: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reject_parse_mode = reject_parse_mode
        self._next_message_id = 100

    async def __call__(
        self, method: str, payload: dict, *, timeout: float | None = None
    ) -> Any | None:
        self.calls.append((method, payload))
        if self.reject_parse_mode and "parse_mode" in payload:
            return None  # simulate a Markdown parse error
        self._next_message_id += 1
        return {"message_id": self._next_message_id}

    def named(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]


def _make_adapter(
    events: list[BridgeEvent] | None = None, *, reject_parse_mode: bool = False
) -> tuple[TelegramAdapter, _ApiRecorder, FakeBridge]:
    bridge = FakeBridge(events)
    adapter = TelegramAdapter(
        TelegramConfig(bot_token="123:abc"),
        bridge,  # type: ignore[arg-type]
    )
    adapter._bot_username = "bridge_bot"
    adapter._bot_id = 999
    recorder = _ApiRecorder(reject_parse_mode=reject_parse_mode)
    adapter._api_call = recorder  # type: ignore[method-assign]
    return adapter, recorder, bridge


def _message(text: str = "hello") -> dict:
    return {
        "message_id": 10,
        "text": text,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 7, "first_name": "Alice", "is_bot": False},
    }


async def _run(adapter: TelegramAdapter, message: dict, prompt: str) -> None:
    await adapter._handle_message(message, prompt)


async def test_processing_posts_placeholder_reply():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="done")])
    await _run(adapter, _message(), "hello")

    sends = api.named("sendMessage")
    assert sends[0]["text"] == PROCESSING_TEXT
    assert sends[0]["chat_id"] == 42
    assert sends[0]["reply_to_message_id"] == 10
    assert sends[0]["allow_sending_without_reply"] is True
    assert "parse_mode" not in sends[0]  # placeholder is plain text


async def test_completion_edits_placeholder_with_buffered_text():
    adapter, api, _ = _make_adapter(
        [
            Processing(),
            TextDelta(text="part one"),
            TextDelta(text="part two"),
            Completion(text="ignored — deltas win"),
        ]
    )
    await _run(adapter, _message(), "hello")

    edits = api.named("editMessageText")
    assert edits[-1]["text"] == "part one\n\npart two"
    assert edits[-1]["message_id"] == 101  # the placeholder message


async def test_completion_without_deltas_uses_completion_text():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="final answer")])
    await _run(adapter, _message(), "hello")
    assert api.named("editMessageText")[-1]["text"] == "final answer"


async def test_status_update_edits_placeholder():
    adapter, api, _ = _make_adapter(
        [Processing(), StatusUpdate(status="Using Bash..."), Completion(text="ok")]
    )
    await _run(adapter, _message(), "hello")

    edits = api.named("editMessageText")
    assert edits[0]["text"] == "⏳ Using Bash..."
    assert "parse_mode" not in edits[0]


async def test_long_completion_splits_into_extra_messages():
    long_text = "a" * (TELEGRAM_MSG_MAX_CHARS + 500)
    adapter, api, _ = _make_adapter([Processing(), Completion(text=long_text)])
    await _run(adapter, _message(), "hello")

    edits = api.named("editMessageText")
    assert len(edits[-1]["text"]) == TELEGRAM_MSG_MAX_CHARS
    # Overflow delivered as a follow-up sendMessage (after the placeholder).
    sends = api.named("sendMessage")
    assert sends[-1]["text"] == "a" * 500


async def test_error_completion_prefixed():
    adapter, api, _ = _make_adapter(
        [Processing(), Completion(text="boom", is_error=True)]
    )
    await _run(adapter, _message(), "hello")
    assert api.named("editMessageText")[-1]["text"] == "❌ boom"


async def test_lone_error_completion_sends_without_placeholder():
    # Capacity-full short-circuit: single Completion, no Processing.
    adapter, api, _ = _make_adapter([Completion(text="capacity full", is_error=True)])
    await _run(adapter, _message(), "hello")

    assert api.named("editMessageText") == []
    sends = api.named("sendMessage")
    assert sends[0]["text"] == "❌ capacity full"
    assert sends[0]["reply_to_message_id"] == 10


async def test_empty_completion_renders_fallback_notice():
    adapter, api, _ = _make_adapter([Processing(), Completion(text="")])
    await _run(adapter, _message(), "hello")
    assert api.named("editMessageText")[-1]["text"] == "No response from agent."


async def test_user_question_rendered_at_completion():
    adapter, api, _ = _make_adapter(
        [
            Processing(),
            UserQuestion(questions=[{"question": "Proceed?", "options": ["yes"]}]),
            Completion(text=""),
        ]
    )
    await _run(adapter, _message(), "hello")

    final = api.named("editMessageText")[-1]["text"]
    assert "Proceed?" in final
    assert "• yes" in final


async def test_markdown_failure_falls_back_to_plain_text():
    adapter, api, _ = _make_adapter(
        [Processing(), Completion(text="*broken markdown")],
        reject_parse_mode=True,
    )
    await _run(adapter, _message(), "hello")

    edits = api.named("editMessageText")
    assert "parse_mode" in edits[-2]  # first attempt: Markdown
    assert "parse_mode" not in edits[-1]  # retry: plain text
    assert edits[-1]["text"] == "*broken markdown"


async def test_bridge_receives_tagged_prompt_context_and_system_prompt():
    adapter, _, bridge = _make_adapter()
    await _run(adapter, _message("do the thing"), "do the thing")

    call = bridge.calls[0]
    assert call["session_key"] == "telegram:42:0"
    assert call["text"] == "[Alice (7)]: do the thing"
    assert call["resumable"] is True
    assert call["context"]["platform"] == "telegram"
    assert call["context"]["chat_id"] == "42"
    assert call["context"]["user_id"] == "7"
    assert call["context"]["message_id"] == "10"
    assert "Telegram" in call["system_prompt"]


async def test_topic_message_scopes_session_and_thread():
    adapter, api, bridge = _make_adapter([Processing(), Completion(text="ok")])
    message = {
        "message_id": 11,
        "text": "hi",
        "chat": {"id": -100123, "type": "supergroup", "title": "Ops"},
        "from": {"id": 7, "first_name": "Alice"},
        "is_topic_message": True,
        "message_thread_id": 55,
    }
    await adapter._handle_message(message, "hi")

    assert bridge.calls[0]["session_key"] == "telegram:-100123:55"
    assert api.named("sendMessage")[0]["message_thread_id"] == 55


async def test_bridge_exception_logged_not_raised():
    class _BoomBridge:
        async def handle_message(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield  # noqa: unreachable — makes this an async generator

    adapter, _, _ = _make_adapter()
    adapter._bridge = _BoomBridge()  # type: ignore[assignment]
    await _run(adapter, _message(), "hello")  # must not raise


def test_cleanup_stale_sessions_drops_expired_locks(session_manager):
    adapter = TelegramAdapter(
        TelegramConfig(bot_token="123:abc"),
        FakeBridge(),  # type: ignore[arg-type]
        session_manager=session_manager,
    )
    session_manager.get_or_create("telegram:1:0")
    adapter._locks["telegram:1:0"] = asyncio.Lock()
    adapter._locks["telegram:2:0"] = asyncio.Lock()

    removed = adapter.cleanup_stale_sessions()

    assert removed == 1
    assert "telegram:1:0" in adapter._locks
    assert "telegram:2:0" not in adapter._locks

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from slack_sdk.errors import SlackApiError

from agent_bridge.bridge.events import Completion, Processing, Usage
from agent_bridge.platforms.slack.adapter import SlackAdapter
from agent_bridge.platforms.slack.config import DEFAULT_MSG_MAX_BYTES, SlackConfig
from tests.fakes import FakeSlackClient
from tests.platforms.slack.harness import build_harness


def _make_adapter() -> SlackAdapter:
    """Build a SlackAdapter with mocked Slack client, skipping __init__."""
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._config = SlackConfig(bot_token="xoxb-x", app_token="xapp-x")
    adapter._app = MagicMock()
    adapter._app.client = MagicMock()
    adapter._app.client.chat_update = AsyncMock()
    adapter._app.client.files_upload_v2 = AsyncMock()
    return adapter


class _FakeBridge:
    """Yields a fixed event sequence from handle_message()."""

    def __init__(self, events: list) -> None:
        self._events = events

    def handle_message(self, **_kwargs):
        async def gen():
            for e in self._events:
                yield e

        return gen()


def _usage_adapter(events: list, *, enabled: bool = True) -> SlackAdapter:
    adapter = _make_adapter()
    adapter._config = SlackConfig(
        bot_token="x", app_token="y", usage_report_enabled=enabled
    )
    adapter._bridge = _FakeBridge(events)
    return adapter


async def test_update_message_cjk_trimmed_before_send():
    """CJK text over the byte ceiling is trimmed client-side on the first try."""
    adapter = _make_adapter()
    # 2000 x '測' = 6000 bytes — old char check (len > 3900) missed this.
    text = "測" * 2000
    await adapter._update_message("C1", "1.0", text)

    adapter._app.client.chat_update.assert_awaited_once()
    sent = adapter._app.client.chat_update.await_args.kwargs["text"]
    assert len(sent.encode("utf-8")) <= DEFAULT_MSG_MAX_BYTES


async def test_update_message_ascii_under_limit_untouched():
    adapter = _make_adapter()
    text = "hello world"
    await adapter._update_message("C1", "1.0", text)
    sent = adapter._app.client.chat_update.await_args.kwargs["text"]
    assert sent == text


async def test_update_message_progressive_fallback_on_msg_too_long():
    """If Slack still returns msg_too_long, retries must progressively shrink."""
    adapter = _make_adapter()
    calls: list[int] = []

    async def fake_update(**kwargs):
        byte_len = len(kwargs["text"].encode("utf-8"))
        calls.append(byte_len)
        if len(calls) <= 2:
            raise SlackApiError("too long", {"error": "msg_too_long"})
        # third attempt succeeds

    adapter._app.client.chat_update = AsyncMock(side_effect=fake_update)

    # Long CJK input that byte-fit still fails on (simulated).
    await adapter._update_message("C1", "1.0", "測" * 2000)

    assert len(calls) == 3
    # First attempt near the full ceiling.
    assert calls[0] <= DEFAULT_MSG_MAX_BYTES
    # Each fallback strictly smaller than the prior.
    assert calls[1] < calls[0]
    assert calls[2] < calls[1]


async def test_update_message_gives_up_after_all_fallbacks():
    adapter = _make_adapter()

    async def always_fail(**_kwargs):
        raise SlackApiError("too long", {"error": "msg_too_long"})

    adapter._app.client.chat_update = AsyncMock(side_effect=always_fail)

    # Should not raise — just log and return.
    await adapter._update_message("C1", "1.0", "測" * 5000)
    # 1 initial + 3 fallbacks = 4 total attempts.
    assert adapter._app.client.chat_update.await_count == 4


async def test_update_message_non_retryable_error_no_retry():
    adapter = _make_adapter()

    async def fail_once(**_kwargs):
        raise SlackApiError("nope", {"error": "channel_not_found"})

    adapter._app.client.chat_update = AsyncMock(side_effect=fail_once)

    await adapter._update_message("C1", "1.0", "hi")
    # No retry for non-msg_too_long errors.
    assert adapter._app.client.chat_update.await_count == 1


async def test_upload_snippet_returns_true_on_success():
    adapter = _make_adapter()
    ok = await adapter._upload_snippet("C1", "1.0", "content")
    assert ok is True
    adapter._app.client.files_upload_v2.assert_awaited_once()


async def test_upload_snippet_returns_false_on_error():
    adapter = _make_adapter()
    adapter._app.client.files_upload_v2 = AsyncMock(
        side_effect=SlackApiError("denied", {"error": "not_authed"})
    )
    ok = await adapter._upload_snippet("C1", "1.0", "content")
    assert ok is False


# --- Usage footer x long-reply upload interaction ---


async def test_long_reply_footer_inline_not_in_uploaded_file():
    """When the body overflows: upload the body alone, show the footer inline."""
    usage = Usage(input_tokens=10, output_tokens=5, cost_usd=0.0123, duration_ms=12300)
    body = "A" * (DEFAULT_MSG_MAX_BYTES + 500)  # exceeds the inline ceiling
    adapter = _usage_adapter(
        [Processing(), Completion(text=body, usage=usage, session_usage=usage)]
    )

    await adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "hi", {}, say=None, existing_message_ts="1.0"
    )

    # Uploaded file = body only, no footer pollution.
    upload_content = adapter._app.client.files_upload_v2.await_args.kwargs["content"]
    assert upload_content == body
    assert "💰" not in upload_content
    assert "──" not in upload_content

    # Inline preview (last chat_update) carries the footer.
    inline = adapter._app.client.chat_update.await_args.kwargs["text"]
    assert "💰" in inline
    assert "$0.0123" in inline
    assert len(inline.encode("utf-8")) <= DEFAULT_MSG_MAX_BYTES


async def test_footer_does_not_push_inline_reply_to_upload():
    """A body that fits inline must not be forced to a file just by the footer."""
    usage = Usage(input_tokens=10, output_tokens=5, cost_usd=0.0123, duration_ms=12300)
    body = "A" * (DEFAULT_MSG_MAX_BYTES - 50)  # fits alone; body+footer would not
    adapter = _usage_adapter([Completion(text=body, usage=usage, session_usage=usage)])

    await adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "hi", {}, say=None, existing_message_ts="1.0"
    )

    adapter._app.client.files_upload_v2.assert_not_awaited()


# --- _delete_message ---


async def test_delete_message_removes_placeholder():
    harness = build_harness()
    posted = await harness.client.chat_postMessage(channel="C1", text="tmp")

    await harness.adapter._delete_message("C1", posted["ts"])

    assert ("C1", posted["ts"]) not in harness.client.messages


async def test_delete_message_error_is_swallowed():
    harness = build_harness()
    harness.client.fail_next["chat_delete"] = "message_not_found"
    await harness.adapter._delete_message("C1", "9.9")  # must not raise


# --- fallback retries stop on non-retryable errors ---


class _SequencedErrorClient(FakeSlackClient):
    """chat_update raises one scripted error code per call until exhausted."""

    def __init__(self, codes: list[str]) -> None:
        super().__init__()
        self.codes = codes

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        if self.codes:
            self.fail_next["chat_update"] = self.codes.pop(0)
        return await super().chat_update(**kwargs)


async def test_update_fallback_stops_on_non_retryable_error():
    client = _SequencedErrorClient(["msg_too_long", "channel_not_found"])
    harness = build_harness(client=client)

    await harness.adapter._update_message("C1", "1.0", "hello")

    # One initial attempt, one fallback, then give up — no further retries.
    assert len(client.calls_to("chat_update")) == 2

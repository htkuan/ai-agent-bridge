from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from agent_bridge.platforms.slack.adapter import (
    SLACK_MSG_MAX_BYTES,
    SlackAdapter,
)


def _make_adapter() -> SlackAdapter:
    """Build a SlackAdapter with mocked Slack client, skipping __init__."""
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._app = MagicMock()
    adapter._app.client = MagicMock()
    adapter._app.client.chat_update = AsyncMock()
    adapter._app.client.files_upload_v2 = AsyncMock()
    return adapter


async def test_update_message_cjk_trimmed_before_send():
    """CJK text over the byte ceiling is trimmed client-side on the first try."""
    adapter = _make_adapter()
    # 2000 × '測' = 6000 bytes — old char check (len > 3900) missed this.
    text = "測" * 2000
    await adapter._update_message("C1", "1.0", text)

    adapter._app.client.chat_update.assert_awaited_once()
    sent = adapter._app.client.chat_update.await_args.kwargs["text"]
    assert len(sent.encode("utf-8")) <= SLACK_MSG_MAX_BYTES


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
    assert calls[0] <= SLACK_MSG_MAX_BYTES
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

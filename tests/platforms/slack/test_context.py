"""Context building: _resolve_context and _prepare_text (attachments)."""

from __future__ import annotations

from tests.fakes import mention_event
from tests.platforms.slack.harness import build_harness


async def test_resolve_context_builds_full_dict():
    harness = build_harness()
    ctx = await harness.adapter._resolve_context("C123", "U123", "1.0", harness.client)
    assert ctx == {
        "workspace": "acme",
        "channel_id": "C123",
        "channel_name": "general",
        "thread_ts": "1.0",
        "user_id": "U123",
        "user_name": "alice",
    }


async def test_resolve_context_includes_bot_user_id_when_known():
    harness = build_harness()
    harness.adapter._bot_user_id = "UBOT"
    ctx = await harness.adapter._resolve_context("C123", "U123", "1.0", harness.client)
    assert ctx["bot_user_id"] == "UBOT"


def test_prepare_text_strips_mention():
    adapter = build_harness().adapter
    event = mention_event(text="<@UBOT> do the thing")
    assert adapter._prepare_text(event["text"], event) == "do the thing"


def test_prepare_text_mention_only_becomes_empty():
    adapter = build_harness().adapter
    event = mention_event(text="<@UBOT>")
    assert adapter._prepare_text(event["text"], event) == ""


def test_prepare_text_appends_attachment_hint():
    adapter = build_harness().adapter
    files = [
        {
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "url_private_download": "https://dl.example/report.pdf",
            "url_private": "https://priv.example/report.pdf",
        },
        {
            "name": "notes.txt",
            "mimetype": "text/plain",
            "url_private": "https://priv.example/notes.txt",
        },
        {},
    ]
    event = mention_event(text="<@UBOT> summarize", files=files)

    text = adapter._prepare_text(event["text"], event)

    assert text.startswith("summarize")
    # Download hint carries the bot token for curl.
    assert "Authorization: Bearer xoxb-x" in text
    # url_private_download wins; url_private is the fallback; missing → blank.
    lines = [line.rstrip() for line in text.splitlines()]
    assert "- report.pdf (application/pdf): https://dl.example/report.pdf" in lines
    assert "- notes.txt (text/plain): https://priv.example/notes.txt" in lines
    assert "- unknown (unknown):" in lines

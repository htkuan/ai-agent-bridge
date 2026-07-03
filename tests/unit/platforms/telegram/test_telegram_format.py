from __future__ import annotations

from agent_bridge.platforms.telegram.adapter import (
    TELEGRAM_MSG_MAX_CHARS,
    TelegramAdapter,
    display_name,
    format_questions,
    split_message,
    tag_prompt,
)
from agent_bridge.platforms.telegram.config import TelegramConfig
from tests.helpers import FakeBridge


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        TelegramConfig(bot_token="123:abc"),
        FakeBridge(),  # type: ignore[arg-type]
    )
    adapter._bot_username = "bridge_bot"
    return adapter


# --- Sender tagging ---


def test_display_name_first_and_last():
    assert display_name({"first_name": "Alice", "last_name": "Wu"}) == "Alice Wu"


def test_display_name_first_only():
    assert display_name({"first_name": "Alice"}) == "Alice"


def test_display_name_falls_back_to_username_then_unknown():
    assert display_name({"username": "alice42"}) == "alice42"
    assert display_name({}) == "unknown"


def test_tag_prompt_includes_name_and_id():
    user = {"id": 7, "first_name": "Alice", "last_name": "Wu"}
    assert tag_prompt("hello", user) == "[Alice Wu (7)]: hello"


def test_tag_prompt_without_id():
    assert tag_prompt("hello", {"first_name": "Alice"}) == "[Alice]: hello"


# --- Message splitting (4096-char limit) ---


def test_split_short_message_single_chunk():
    assert split_message("hi") == ["hi"]


def test_split_exactly_at_limit_single_chunk():
    text = "a" * TELEGRAM_MSG_MAX_CHARS
    assert split_message(text) == [text]


def test_split_long_message_all_chunks_within_limit():
    text = "a" * (TELEGRAM_MSG_MAX_CHARS * 2 + 100)
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= TELEGRAM_MSG_MAX_CHARS for chunk in chunks)
    assert "".join(chunks) == text


def test_split_prefers_newline_boundaries():
    first = "a" * 3000
    second = "b" * 3000
    chunks = split_message(f"{first}\n{second}")
    assert chunks == [first, second]


def test_split_hard_cuts_when_no_newline():
    text = "a" * 5000
    chunks = split_message(text)
    assert chunks == ["a" * TELEGRAM_MSG_MAX_CHARS, "a" * (5000 - TELEGRAM_MSG_MAX_CHARS)]


# --- Question rendering ---


def test_format_single_question_with_options():
    text = format_questions(
        [
            {
                "question": "Proceed?",
                "options": [
                    {"label": "yes", "description": "Apply all"},
                    "no",
                ],
            }
        ]
    )
    assert "Proceed?" in text
    assert "• yes — Apply all" in text
    assert "• no" in text
    assert "Reply in this chat" in text


def test_format_multiple_questions_numbered():
    text = format_questions(
        [
            {"question": "One?", "multiSelect": True},
            {"question": "Two?"},
        ]
    )
    assert "1. One?" in text
    assert "2. Two?" in text
    assert "select multiple" in text


# --- System prompt ---


def test_system_prompt_mentions_platform_and_speaker_convention():
    adapter = _make_adapter()
    prompt = adapter._build_system_prompt(
        {"chat_id": "42", "chat_type": "private", "user_id": "7"}
    )
    assert "Telegram" in prompt
    assert "[display_name (user_id)]" in prompt
    assert "Chat id: 42 (private)" in prompt
    assert "@bridge_bot" in prompt


def test_system_prompt_includes_group_title_and_topic():
    adapter = _make_adapter()
    prompt = adapter._build_system_prompt(
        {
            "chat_id": "-100123",
            "chat_type": "supergroup",
            "chat_title": "Ops Room",
            "message_thread_id": "55",
        }
    )
    assert "Ops Room" in prompt
    assert "-100123" in prompt
    assert "Topic thread: 55" in prompt

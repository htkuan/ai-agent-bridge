from __future__ import annotations

from agent_bridge.platforms.line.adapter import (
    LINE_MSG_MAX_CHARS,
    LineAdapter,
    chat_id,
    extract_text,
    format_questions,
    plan_delivery,
    session_key,
    split_message,
    tag_prompt,
)
from agent_bridge.platforms.line.config import LineConfig
from tests.helpers import FakeBridge


def _make_adapter() -> LineAdapter:
    return LineAdapter(
        LineConfig(channel_secret="sec", channel_access_token="tok"),
        FakeBridge(),  # type: ignore[arg-type]
    )


def _event(text: str = "hello", *, source: dict | None = None) -> dict:
    return {
        "type": "message",
        "replyToken": "rt-1",
        "source": source or {"type": "user", "userId": "U123"},
        "message": {"id": "m1", "type": "text", "text": text},
    }


# --- Session key mapping ---


def test_session_key_user_chat():
    assert session_key({"type": "user", "userId": "U123"}) == "line:user:U123"


def test_session_key_group_uses_group_id():
    source = {"type": "group", "groupId": "G456", "userId": "U123"}
    assert session_key(source) == "line:group:G456"


def test_session_key_room_uses_room_id():
    source = {"type": "room", "roomId": "R789", "userId": "U123"}
    assert session_key(source) == "line:room:R789"


def test_session_key_unknown_source_type_is_none():
    assert session_key({"type": "beacon", "userId": "U123"}) is None
    assert session_key({}) is None


def test_session_key_missing_id_is_none():
    assert session_key({"type": "group"}) is None


def test_chat_id_matches_session_scope():
    assert chat_id({"type": "user", "userId": "U123"}) == "U123"
    assert chat_id({"type": "group", "groupId": "G456", "userId": "U1"}) == "G456"


# --- Prompt tagging ---


def test_tag_prompt_uses_user_id():
    assert tag_prompt("hello", {"type": "user", "userId": "U123"}) == "[U123]: hello"


def test_tag_prompt_without_user_id_falls_back_to_unknown():
    assert tag_prompt("hello", {"type": "group", "groupId": "G1"}) == "[unknown]: hello"


# --- Event filtering ---


def test_extract_text_returns_stripped_text():
    assert extract_text(_event("  hello  ")) == "hello"


def test_non_message_event_ignored():
    assert extract_text({"type": "follow", "source": {"type": "user", "userId": "U1"}}) is None
    assert extract_text({"type": "postback", "source": {"type": "user", "userId": "U1"}}) is None


def test_non_text_message_ignored():
    event = _event()
    event["message"] = {"id": "m1", "type": "sticker", "packageId": "1"}
    assert extract_text(event) is None


def test_empty_text_ignored():
    assert extract_text(_event("   ")) is None


def test_unresolvable_source_ignored():
    event = _event(source={"type": "beacon"})
    assert extract_text(event) is None


# --- Message splitting (5000-char limit) ---


def test_split_short_message_single_chunk():
    assert split_message("hi") == ["hi"]


def test_split_exactly_at_limit_single_chunk():
    text = "a" * LINE_MSG_MAX_CHARS
    assert split_message(text) == [text]


def test_split_long_message_all_chunks_within_limit():
    text = "a" * (LINE_MSG_MAX_CHARS * 2 + 100)
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= LINE_MSG_MAX_CHARS for chunk in chunks)
    assert "".join(chunks) == text


def test_split_prefers_newline_boundaries():
    first = "a" * 4000
    second = "b" * 4000
    chunks = split_message(f"{first}\n{second}")
    assert chunks == [first, second]


def test_split_hard_cuts_when_no_newline():
    text = "a" * 6000
    assert split_message(text) == [
        "a" * LINE_MSG_MAX_CHARS,
        "a" * (6000 - LINE_MSG_MAX_CHARS),
    ]


# --- Reply/Push allocation (5 messages per call) ---


def test_plan_delivery_all_chunks_fit_in_reply():
    reply, push = plan_delivery(["a", "b", "c"])
    assert reply == ["a", "b", "c"]
    assert push == []


def test_plan_delivery_at_cap_no_push():
    chunks = ["1", "2", "3", "4", "5"]
    reply, push = plan_delivery(chunks)
    assert reply == chunks
    assert push == []


def test_plan_delivery_overflow_goes_to_push():
    chunks = [str(i) for i in range(7)]
    reply, push = plan_delivery(chunks)
    assert reply == ["0", "1", "2", "3", "4"]
    assert push == ["5", "6"]


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
        {"platform": "line", "source_type": "user", "chat_id": "U123", "user_id": "U123"}
    )
    assert "LINE" in prompt
    assert "[user_id]" in prompt
    assert "plain text" in prompt
    assert "Chat: user U123" in prompt


def test_system_prompt_group_scope():
    adapter = _make_adapter()
    prompt = adapter._build_system_prompt(
        {"platform": "line", "source_type": "group", "chat_id": "G456", "user_id": "U1"}
    )
    assert "Chat: group G456" in prompt

from __future__ import annotations

from agent_bridge.platforms.telegram.adapter import (
    extract_prompt,
    message_thread_id,
    session_key,
    strip_mention,
)

BOT_USERNAME = "bridge_bot"
BOT_ID = 999


def _message(
    text: str | None = "hello",
    *,
    chat_id: int = 42,
    chat_type: str = "private",
    from_user: dict | None = None,
    **extra,
) -> dict:
    message: dict = {
        "message_id": 1,
        "chat": {"id": chat_id, "type": chat_type},
        "from": from_user or {"id": 7, "first_name": "Alice", "is_bot": False},
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return message


def _extract(message: dict, allow_chats: frozenset[str] = frozenset()) -> str | None:
    return extract_prompt(
        message, bot_username=BOT_USERNAME, bot_id=BOT_ID, allow_chats=allow_chats
    )


# --- Session key ---


def test_session_key_without_thread_uses_zero():
    assert session_key(42, None) == "telegram:42:0"


def test_session_key_with_topic_thread():
    assert session_key(-100123, 55) == "telegram:-100123:55"


def test_message_thread_id_only_for_topic_messages():
    topic = _message(chat_type="supergroup", is_topic_message=True, message_thread_id=55)
    assert message_thread_id(topic) == 55
    # A plain group reply also carries message_thread_id but is not a topic.
    reply = _message(chat_type="supergroup", message_thread_id=55)
    assert message_thread_id(reply) is None


# --- Mention stripping ---


def test_strip_mention_removes_and_reports():
    cleaned, mentioned = strip_mention("@bridge_bot do the thing", BOT_USERNAME)
    assert cleaned == "do the thing"
    assert mentioned is True


def test_strip_mention_case_insensitive():
    cleaned, mentioned = strip_mention("@Bridge_Bot hi", BOT_USERNAME)
    assert cleaned == "hi"
    assert mentioned is True


def test_strip_mention_ignores_other_bots():
    cleaned, mentioned = strip_mention("@bridge_bot2 hi", BOT_USERNAME)
    assert cleaned == "@bridge_bot2 hi"
    assert mentioned is False


def test_strip_mention_mid_text():
    cleaned, mentioned = strip_mention("hey @bridge_bot help", BOT_USERNAME)
    assert cleaned == "hey  help"
    assert mentioned is True


# --- extract_prompt: chat types ---


def test_private_chat_handled_without_mention():
    assert _extract(_message("hello")) == "hello"


def test_non_text_message_ignored():
    assert _extract(_message(None, photo=[{"file_id": "x"}])) is None


def test_whitespace_only_text_ignored():
    assert _extract(_message("   ")) is None


def test_group_without_mention_ignored():
    assert _extract(_message("hello", chat_type="supergroup")) is None


def test_group_with_mention_handled_and_stripped():
    prompt = _extract(_message("@bridge_bot deploy please", chat_type="group"))
    assert prompt == "deploy please"


def test_group_mention_only_text_ignored():
    assert _extract(_message("@bridge_bot", chat_type="group")) is None


def test_group_reply_to_bot_handled():
    message = _message(
        "yes, do it",
        chat_type="supergroup",
        reply_to_message={"from": {"id": BOT_ID, "is_bot": True}},
    )
    assert _extract(message) == "yes, do it"


def test_group_reply_to_other_user_ignored():
    message = _message(
        "sure",
        chat_type="supergroup",
        reply_to_message={"from": {"id": 12345, "is_bot": False}},
    )
    assert _extract(message) is None


def test_channel_post_ignored():
    assert _extract(_message("hello", chat_type="channel")) is None


def test_message_from_bot_ignored():
    message = _message(
        "hello", from_user={"id": 500, "first_name": "OtherBot", "is_bot": True}
    )
    assert _extract(message) is None


# --- extract_prompt: allow-list ---


def test_allow_chats_empty_allows_everything():
    assert _extract(_message("hi", chat_id=1)) == "hi"


def test_allow_chats_blocks_unlisted_chat():
    assert _extract(_message("hi", chat_id=1), allow_chats=frozenset({"2"})) is None


def test_allow_chats_permits_listed_chat():
    assert _extract(_message("hi", chat_id=2), allow_chats=frozenset({"2"})) == "hi"


def test_allow_chats_matches_negative_group_ids():
    message = _message("@bridge_bot hi", chat_id=-100123, chat_type="supergroup")
    assert _extract(message, allow_chats=frozenset({"-100123"})) == "hi"

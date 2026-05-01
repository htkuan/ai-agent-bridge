from __future__ import annotations

from agent_bridge.platforms.slack.adapter import SlackAdapter


# --- Prompt tagging (Slack owns sender identity) ---


def test_tag_prompt_full_context():
    context = {"user_name": "alice", "user_id": "U999"}
    assert SlackAdapter._tag_prompt("hello", context) == "[alice (U999)]: hello"


def test_tag_prompt_missing_user_id():
    context = {"user_name": "alice"}
    assert SlackAdapter._tag_prompt("hi", context) == "[alice]: hi"


def test_tag_prompt_missing_both_falls_back_to_unknown():
    assert SlackAdapter._tag_prompt("hi", {}) == "[unknown]: hi"


# --- System prompt (Slack owns chat-platform framing) ---


def test_build_system_prompt_full_context():
    sp = SlackAdapter._build_system_prompt(
        {
            "workspace": "acme",
            "channel_id": "C123",
            "channel_name": "general",
            "thread_ts": "1700000000.000100",
            "user_id": "U999",
            "user_name": "alice",
            "bot_user_id": "U_BOT",
            "bot_user_name": "agent_bridge",
        }
    )
    assert "Slack" in sp
    assert "[user_name (user_id)]" in sp
    assert "Workspace: acme" in sp
    assert "Channel: #general (C123)" in sp
    assert "Thread: 1700000000.000100" in sp
    assert "You are: agent_bridge (U_BOT)" in sp
    assert "<@U_BOT>" in sp


def test_build_system_prompt_omits_missing_fields():
    sp = SlackAdapter._build_system_prompt({"channel_id": "C123"})
    assert "Channel: C123" in sp
    assert "Workspace" not in sp
    assert "Thread" not in sp
    assert "You are" not in sp


def test_build_system_prompt_bot_id_only():
    sp = SlackAdapter._build_system_prompt(
        {"channel_id": "C123", "bot_user_id": "U_BOT"}
    )
    assert "You are: U_BOT — Slack mention: <@U_BOT>" in sp

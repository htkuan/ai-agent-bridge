from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agent_bridge.platforms.slack.adapter import SlackAdapter, SlackInfoCache
from agent_bridge.platforms.slack.config import (
    DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
    SlackConfig,
    _normalize_channel,
)


def _make_adapter(
    allow_channels: frozenset[str],
    channel_not_allowed_message: str = DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
) -> SlackAdapter:
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._config = SlackConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        allow_channels=allow_channels,
        channel_not_allowed_message=channel_not_allowed_message,
    )
    adapter._name_cache = SlackInfoCache()
    return adapter


def _client_with_channel(name: str | None) -> MagicMock:
    client = MagicMock()
    channel: dict[str, str] = {}
    if name is not None:
        channel["name"] = name
    client.conversations_info = AsyncMock(return_value={"channel": channel})
    return client


# --- _normalize_channel ---


def test_normalize_strips_hash_whitespace_case():
    assert _normalize_channel("  #Ops-Alerts  ") == "ops-alerts"
    assert _normalize_channel("team-eng") == "team-eng"


# --- config parsing ---


def test_from_env_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_ALLOW_CHANNELS", " #Ops-Alerts , team-eng ,, ")
    cfg = SlackConfig.from_env()
    assert cfg.allow_channels == frozenset({"ops-alerts", "team-eng"})


def test_from_env_empty_means_allow_all(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_APP_TOKEN", "xapp-x")
    # Empty string (not delenv) so a local .env can't repopulate it via load_dotenv.
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_ALLOW_CHANNELS", "")
    cfg = SlackConfig.from_env()
    assert cfg.allow_channels == frozenset()


# --- gate behaviour ---


async def test_empty_allowlist_allows_any_channel_without_api_call():
    adapter = _make_adapter(frozenset())
    client = _client_with_channel("anything")
    assert await adapter._channel_allowed("C1", client) is True
    # No resolution needed when allow-list is empty.
    client.conversations_info.assert_not_awaited()


async def test_listed_channel_allowed():
    adapter = _make_adapter(frozenset({"ops-alerts"}))
    client = _client_with_channel("ops-alerts")
    assert await adapter._channel_allowed("C1", client) is True


async def test_unlisted_channel_blocked():
    adapter = _make_adapter(frozenset({"ops-alerts"}))
    client = _client_with_channel("random-chat")
    assert await adapter._channel_allowed("C1", client) is False


async def test_dm_without_name_blocked_when_list_set():
    # A DM has no name; conversations_info returns a nameless channel and the
    # cache falls back to the channel id, which won't match any listed name.
    adapter = _make_adapter(frozenset({"ops-alerts"}))
    client = _client_with_channel(None)
    assert await adapter._channel_allowed("D123", client) is False


# --- end-to-end: rejection reply posted, no agent work ---


async def test_rejected_channel_replies_and_stops():
    adapter = _make_adapter(frozenset({"ops-alerts"}), channel_not_allowed_message="nope, go away")
    adapter._bridge = MagicMock()
    adapter._bridge.handle_message = MagicMock(
        side_effect=AssertionError("agent should not be invoked")
    )
    client = _client_with_channel("random-chat")
    say = AsyncMock()
    event = {"channel": "C1", "user": "U1", "text": "hi", "ts": "1.0"}

    await adapter._process_message(event, say, client)

    say.assert_awaited_once_with(text="nope, go away", thread_ts="1.0")
    adapter._bridge.handle_message.assert_not_called()


async def test_allowed_channel_not_rejected():
    adapter = _make_adapter(frozenset({"ops-alerts"}))
    # Make _stream_response a no-op so we only assert the gate passed.
    adapter._get_state = MagicMock(side_effect=AssertionError("gate passed"))
    client = _client_with_channel("ops-alerts")
    say = AsyncMock()
    event = {"channel": "C1", "user": "U1", "text": "hi", "ts": "1.0"}

    # Gate passes → proceeds past the reply branch into normal handling,
    # which we short-circuit via _get_state raising.
    try:
        await adapter._process_message(event, say, client)
    except AssertionError as e:
        assert "gate passed" in str(e)
    # The rejection reply must NOT have been sent.
    for call in say.await_args_list:
        assert call.kwargs.get("text") != DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE

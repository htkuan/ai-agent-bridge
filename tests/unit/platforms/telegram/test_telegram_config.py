from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.config_loader import ConfigSource
from agent_bridge.platforms.telegram.config import (
    DEFAULT_API_BASE_URL,
    TelegramConfig,
)


def test_missing_bot_token_raises_with_env_var_name():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_TELEGRAM_BOT_TOKEN"):
        TelegramConfig.from_source(ConfigSource({}, env={}))


def test_defaults():
    source = ConfigSource({"platforms": {"telegram": {"bot_token": "123:abc"}}}, env={})
    config = TelegramConfig.from_source(source)
    assert config.bot_token == "123:abc"
    assert config.allow_chats == frozenset()
    assert config.poll_timeout_seconds == 30
    assert config.state_path == Path("./telegram.json")
    assert config.api_base_url == DEFAULT_API_BASE_URL


def test_from_source_yaml_keys():
    source = ConfigSource(
        {
            "platforms": {
                "telegram": {
                    "bot_token": "123:abc",
                    "allow_chats": ["-100123", "42"],
                    "poll_timeout_seconds": 5,
                    "state_path": "/tmp/tg.json",
                    "api_base_url": "http://127.0.0.1:8099/",
                }
            }
        },
        env={},
    )
    config = TelegramConfig.from_source(source)
    assert config.allow_chats == frozenset({"-100123", "42"})
    assert config.poll_timeout_seconds == 5
    assert config.state_path == Path("/tmp/tg.json")
    # Trailing slash is stripped so URL joining stays predictable.
    assert config.api_base_url == "http://127.0.0.1:8099"


def test_env_overrides_yaml():
    source = ConfigSource(
        {"platforms": {"telegram": {"bot_token": "yaml-token"}}},
        env={
            "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN": "env-token",
            "AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS": "7",
        },
    )
    config = TelegramConfig.from_source(source)
    assert config.bot_token == "env-token"
    assert config.poll_timeout_seconds == 7


def test_allow_chats_env_csv_parsing():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN": "123:abc",
            "AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS": " -100123 , 42 ,, ",
        },
    )
    config = TelegramConfig.from_source(source)
    assert config.allow_chats == frozenset({"-100123", "42"})


def test_negative_poll_timeout_raises():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN": "123:abc",
            "AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS": "-1",
        },
    )
    with pytest.raises(ValueError, match="POLL_TIMEOUT_SECONDS"):
        TelegramConfig.from_source(source)


def test_zero_poll_timeout_allowed_for_short_polling():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN": "123:abc",
            "AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS": "0",
        },
    )
    assert TelegramConfig.from_source(source).poll_timeout_seconds == 0


def test_from_env_reads_process_environment(monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("AGENT_BRIDGE_TELEGRAM_BOT_TOKEN", "123:abc")
    config = TelegramConfig.from_env()
    assert config.bot_token == "123:abc"

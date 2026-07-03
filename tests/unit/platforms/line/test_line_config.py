from __future__ import annotations

import pytest

from agent_bridge.config_loader import ConfigSource
from agent_bridge.platforms.line.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_WEBHOOK_PATH,
    LineConfig,
)


def test_missing_channel_secret_raises_with_env_var_name():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_LINE_CHANNEL_SECRET"):
        LineConfig.from_source(ConfigSource({}, env={}))


def test_missing_access_token_raises_with_env_var_name():
    source = ConfigSource({"platforms": {"line": {"channel_secret": "sec"}}}, env={})
    with pytest.raises(ValueError, match="AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN"):
        LineConfig.from_source(source)


def test_defaults():
    source = ConfigSource(
        {"platforms": {"line": {"channel_secret": "sec", "channel_access_token": "tok"}}},
        env={},
    )
    config = LineConfig.from_source(source)
    assert config.channel_secret == "sec"
    assert config.channel_access_token == "tok"
    assert config.webhook_host == "0.0.0.0"
    assert config.webhook_port == 8080
    assert config.webhook_path == DEFAULT_WEBHOOK_PATH
    assert config.api_base_url == DEFAULT_API_BASE_URL


def test_from_source_nested_webhook_keys():
    source = ConfigSource(
        {
            "platforms": {
                "line": {
                    "channel_secret": "sec",
                    "channel_access_token": "tok",
                    "webhook": {
                        "host": "127.0.0.1",
                        "port": 9090,
                        "path": "/hooks/line",
                    },
                    "api_base_url": "http://127.0.0.1:8099/",
                }
            }
        },
        env={},
    )
    config = LineConfig.from_source(source)
    assert config.webhook_host == "127.0.0.1"
    assert config.webhook_port == 9090
    assert config.webhook_path == "/hooks/line"
    # Trailing slash is stripped so URL joining stays predictable.
    assert config.api_base_url == "http://127.0.0.1:8099"


def test_env_overrides_yaml():
    source = ConfigSource(
        {
            "platforms": {
                "line": {
                    "channel_secret": "yaml-sec",
                    "channel_access_token": "yaml-tok",
                    "webhook": {"port": 9090},
                }
            }
        },
        env={
            "AGENT_BRIDGE_LINE_CHANNEL_SECRET": "env-sec",
            "AGENT_BRIDGE_LINE_WEBHOOK_PORT": "7070",
        },
    )
    config = LineConfig.from_source(source)
    assert config.channel_secret == "env-sec"
    assert config.channel_access_token == "yaml-tok"
    assert config.webhook_port == 7070


def test_port_out_of_range_raises():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_LINE_CHANNEL_SECRET": "sec",
            "AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN": "tok",
            "AGENT_BRIDGE_LINE_WEBHOOK_PORT": "65536",
        },
    )
    with pytest.raises(ValueError, match="WEBHOOK_PORT"):
        LineConfig.from_source(source)


def test_port_zero_allowed_for_ephemeral_binding():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_LINE_CHANNEL_SECRET": "sec",
            "AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN": "tok",
            "AGENT_BRIDGE_LINE_WEBHOOK_PORT": "0",
        },
    )
    assert LineConfig.from_source(source).webhook_port == 0


def test_path_without_leading_slash_raises():
    source = ConfigSource(
        {},
        env={
            "AGENT_BRIDGE_LINE_CHANNEL_SECRET": "sec",
            "AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN": "tok",
            "AGENT_BRIDGE_LINE_WEBHOOK_PATH": "line/webhook",
        },
    )
    with pytest.raises(ValueError, match="WEBHOOK_PATH"):
        LineConfig.from_source(source)


def test_from_env_reads_process_environment(monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("AGENT_BRIDGE_LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setenv("AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    config = LineConfig.from_env()
    assert config.channel_secret == "sec"
    assert config.channel_access_token == "tok"

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.config import BridgeConfig
from agent_bridge.config_loader import ConfigSource
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import (
    DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
    SlackConfig,
)


# --- BridgeConfig ---


def test_bridge_defaults_from_empty_source():
    config = BridgeConfig.from_source(ConfigSource({}, env={}))
    assert config.session_store_path == Path("./sessions.json")
    assert config.session_ttl_hours == 72.0
    assert config.max_concurrent_sessions == 5
    assert config.dedupe_ttl_seconds == 0.0
    assert config.dedupe_max_entries == 512
    assert config.dedupe_simhash_threshold == 0


def test_bridge_class_default_matches_env_default():
    # D15: class default and env default must both be 5.
    assert BridgeConfig().max_concurrent_sessions == 5


def test_bridge_from_yaml_values():
    data = {
        "bridge": {
            "session_store_path": "/tmp/s.json",
            "session_ttl_hours": 12,
            "max_concurrent_sessions": 3,
            "dedupe": {"ttl_seconds": 90, "max_entries": 64, "simhash_threshold": 8},
        }
    }
    config = BridgeConfig.from_source(ConfigSource(data, env={}))
    assert config.session_store_path == Path("/tmp/s.json")
    assert config.session_ttl_hours == 12.0
    assert config.max_concurrent_sessions == 3
    assert config.dedupe_ttl_seconds == 90.0
    assert config.dedupe_max_entries == 64
    assert config.dedupe_simhash_threshold == 8


def test_bridge_env_overrides_yaml():
    data = {"bridge": {"max_concurrent_sessions": 3, "session_ttl_hours": 12}}
    env = {"AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS": "7"}
    config = BridgeConfig.from_source(ConfigSource(data, env=env))
    assert config.max_concurrent_sessions == 7
    assert config.session_ttl_hours == 12.0


def test_bridge_from_source_validates():
    data = {"bridge": {"session_ttl_hours": -1}}
    with pytest.raises(ValueError, match="SESSION_TTL_HOURS"):
        BridgeConfig.from_source(ConfigSource(data, env={}))


# --- ClaudeConfig ---


def test_claude_from_yaml_values(tmp_path):
    data = {
        "agents": {
            "claude": {
                "work_dir": str(tmp_path),
                "permission_mode": "plan",
                "timeout_seconds": 42,
                "effort": "low",
            }
        }
    }
    config = ClaudeConfig.from_source(ConfigSource(data, env={}))
    assert config.work_dir == tmp_path.resolve()
    assert config.permission_mode == "plan"
    assert config.timeout_seconds == 42.0
    assert config.worktree_enabled is False
    assert config.effort == "low"


def test_claude_env_overrides_yaml(tmp_path):
    data = {"agents": {"claude": {"work_dir": "/nonexistent", "effort": "low"}}}
    env = {"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path)}
    config = ClaudeConfig.from_source(ConfigSource(data, env=env))
    assert config.work_dir == tmp_path.resolve()
    assert config.effort == "low"


def test_claude_yaml_bool_worktree(tmp_path, monkeypatch):
    data = {"agents": {"claude": {"work_dir": str(tmp_path), "worktree_enabled": False}}}
    config = ClaudeConfig.from_source(ConfigSource(data, env={}))
    assert config.worktree_enabled is False


def test_claude_from_source_validates():
    data = {"agents": {"claude": {"permission_mode": "bogus"}}}
    with pytest.raises(ValueError, match="PERMISSION_MODE"):
        ClaudeConfig.from_source(ConfigSource(data, env={}))


# --- SlackConfig ---


def test_slack_from_yaml_values():
    data = {
        "platforms": {
            "slack": {
                "bot_token": "xoxb-1",
                "app_token": "xapp-1",
                "startup_notify_channel": "C123",
                "startup_notify_message": "hi",
                "allow_channels": ["Ops-Alerts", "#team-eng"],
                "usage_report": {"enabled": True, "template": "{cost_usd}"},
            }
        }
    }
    config = SlackConfig.from_source(ConfigSource(data, env={}))
    assert config.bot_token == "xoxb-1"
    assert config.app_token == "xapp-1"
    assert config.startup_notify_channel == "C123"
    assert config.startup_notify_message == "hi"
    assert config.allow_channels == frozenset({"ops-alerts", "team-eng"})
    assert config.channel_not_allowed_message == DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE
    assert config.usage_report_enabled is True
    assert config.usage_report_template == "{cost_usd}"


def test_slack_missing_tokens_raises():
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        SlackConfig.from_source(ConfigSource({}, env={}))


def test_slack_env_overrides_yaml_token():
    data = {"platforms": {"slack": {"bot_token": "xoxb-yaml", "app_token": "xapp-yaml"}}}
    env = {"AGENT_BRIDGE_SLACK_BOT_TOKEN": "xoxb-env"}
    config = SlackConfig.from_source(ConfigSource(data, env=env))
    assert config.bot_token == "xoxb-env"
    assert config.app_token == "xapp-yaml"


def test_slack_allow_channels_comma_string_via_env():
    env = {
        "AGENT_BRIDGE_SLACK_BOT_TOKEN": "xoxb-1",
        "AGENT_BRIDGE_SLACK_APP_TOKEN": "xapp-1",
        "AGENT_BRIDGE_SLACK_ALLOW_CHANNELS": "ops-alerts, #Team-Eng",
    }
    config = SlackConfig.from_source(ConfigSource({}, env=env))
    assert config.allow_channels == frozenset({"ops-alerts", "team-eng"})


# --- HeartbeatConfig ---


def test_heartbeat_disabled_by_default():
    config = HeartbeatConfig.from_source(ConfigSource({}, env={}))
    assert config.enabled is False


def test_heartbeat_from_yaml_values(tmp_path):
    data = {
        "platforms": {
            "heartbeat": {
                "enabled": True,
                "interval_minutes": 15,
                "prompt": "go",
                "state_path": str(tmp_path / "h.json"),
            }
        }
    }
    config = HeartbeatConfig.from_source(ConfigSource(data, env={}))
    assert config.enabled is True
    assert config.interval_minutes == 15
    assert config.prompt == "go"
    assert config.state_path == tmp_path / "h.json"


def test_heartbeat_env_overrides_yaml():
    data = {
        "platforms": {
            "heartbeat": {"enabled": True, "interval_minutes": 15, "prompt": "yaml"}
        }
    }
    env = {"AGENT_BRIDGE_HEARTBEAT_PROMPT": "env"}
    config = HeartbeatConfig.from_source(ConfigSource(data, env=env))
    assert config.prompt == "env"


def test_heartbeat_enabled_without_prompt_raises():
    data = {"platforms": {"heartbeat": {"enabled": True, "interval_minutes": 15}}}
    with pytest.raises(ValueError, match="HEARTBEAT_PROMPT"):
        HeartbeatConfig.from_source(ConfigSource(data, env={}))

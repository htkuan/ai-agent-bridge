"""Full config chain: YAML file + $(VAR) secrets + env overrides → component configs."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.config import BridgeConfig
from agent_bridge.config_loader import load_config_source
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import SlackConfig

pytestmark = pytest.mark.integration


def _write_yaml(tmp_path: Path) -> Path:
    config_file = tmp_path / "agent-bridge.yaml"
    config_file.write_text(
        f"""\
log_level: DEBUG
agent: claude
bridge:
  session_store_path: {tmp_path / "sessions.json"}
  session_ttl_hours: 12
  max_concurrent_sessions: 3
  dedupe:
    ttl_seconds: 90
platforms:
  slack:
    bot_token: $(TEST_E2E_BOT_TOKEN)
    app_token: xapp-yaml
    allow_channels: [Ops-Alerts, "#team-eng"]
    usage_report:
      enabled: true
  heartbeat:
    enabled: true
    interval_minutes: 15
    prompt: check the queue
    state_path: {tmp_path / "heartbeat.json"}
agents:
  claude:
    work_dir: {tmp_path}
    permission_mode: plan
    timeout_seconds: 42
"""
    )
    return config_file


@pytest.fixture
def source(tmp_path, monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("TEST_E2E_BOT_TOKEN", "xoxb-from-secret")
    monkeypatch.setenv("AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", "7")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_PROMPT", "env wins")
    return load_config_source(_write_yaml(tmp_path))


def test_bridge_config_from_yaml_with_env_override(source):
    config = BridgeConfig.from_source(source)
    assert config.session_ttl_hours == 12.0
    assert config.dedupe_ttl_seconds == 90.0
    # env AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS=7 beats YAML's 3
    assert config.max_concurrent_sessions == 7


def test_slack_config_resolves_secret_and_lists(source):
    config = SlackConfig.from_source(source)
    # $(TEST_E2E_BOT_TOKEN) was substituted at load time
    assert config.bot_token == "xoxb-from-secret"
    assert config.app_token == "xapp-yaml"
    assert config.allow_channels == frozenset({"ops-alerts", "team-eng"})
    assert config.usage_report_enabled is True


def test_heartbeat_config_env_overrides_yaml(source):
    config = HeartbeatConfig.from_source(source)
    assert config.enabled is True
    assert config.interval_minutes == 15
    # env AGENT_BRIDGE_HEARTBEAT_PROMPT beats the YAML prompt
    assert config.prompt == "env wins"


def test_claude_config_from_yaml(source, tmp_path):
    config = ClaudeConfig.from_source(source)
    assert config.work_dir == tmp_path.resolve()
    assert config.permission_mode == "plan"
    assert config.timeout_seconds == 42.0


def test_missing_secret_fails_at_load_time(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_E2E_MISSING_TOKEN", raising=False)
    config_file = tmp_path / "conf.yaml"
    config_file.write_text("platforms:\n  slack:\n    bot_token: $(TEST_E2E_MISSING_TOKEN)\n")
    with pytest.raises(ValueError, match="TEST_E2E_MISSING_TOKEN"):
        load_config_source(config_file)

"""AppConfig: the aggregate every component config hangs off."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import config as app_config
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.bridge.config import BridgeConfig
from agent_bridge.config import DEFAULT_CLEANUP_INTERVAL_SECONDS, AppConfig
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import SlackConfig


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path), **extra}


# --- from_env: aggregation ---


def test_from_env_defaults(tmp_path: Path):
    config = AppConfig.from_env(_env(tmp_path))
    assert config.bridge == BridgeConfig()
    assert config.claude == ClaudeConfig(work_dir=tmp_path.resolve())
    assert config.slack is None
    assert config.heartbeat is None
    assert config.log_level == "INFO"
    assert config.cleanup_interval_seconds == DEFAULT_CLEANUP_INTERVAL_SECONDS


def test_from_env_wires_every_layer(tmp_path: Path):
    config = AppConfig.from_env(
        _env(
            tmp_path,
            AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS="3",
            AGENT_BRIDGE_CLAUDE_EFFORT="low",
            AGENT_BRIDGE_SLACK_BOT_TOKEN="xoxb-x",
            AGENT_BRIDGE_SLACK_APP_TOKEN="xapp-x",
            AGENT_BRIDGE_HEARTBEAT_ENABLED="true",
            AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES="5",
            AGENT_BRIDGE_HEARTBEAT_PROMPT="tick",
            AGENT_BRIDGE_LOG_LEVEL="debug",
        )
    )
    assert config.bridge.router.max_concurrent_sessions == 3
    assert config.claude.effort == "low"
    assert config.slack == SlackConfig(bot_token="xoxb-x", app_token="xapp-x")
    assert config.heartbeat == HeartbeatConfig(interval_minutes=5, prompt="tick")
    assert config.log_level == "DEBUG"


def test_env_defaults_match_dataclass_defaults(tmp_path: Path):
    assert AppConfig.from_env(_env(tmp_path)) == AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path.resolve())
    )


def test_a_layer_error_surfaces_from_from_env(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_SESSION_TTL_HOURS"):
        AppConfig.from_env(_env(tmp_path, AGENT_BRIDGE_SESSION_TTL_HOURS="0"))


def test_process_env_is_the_only_source_that_loads_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    loads: list[bool] = []
    monkeypatch.setattr(app_config, "load_env_file", lambda: loads.append(True))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))

    AppConfig.from_env()  # no mapping → reads os.environ, overlaid by .env
    AppConfig.from_env(_env(tmp_path))  # explicit mapping → no .env overlay

    assert loads == [True]


# --- validation ---


def test_rejects_unknown_log_level(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_LOG_LEVEL"):
        AppConfig(claude=ClaudeConfig(work_dir=tmp_path), log_level="CHATTY")


def test_rejects_non_positive_cleanup_interval(tmp_path: Path):
    with pytest.raises(ValueError, match="cleanup_interval_seconds"):
        AppConfig(claude=ClaudeConfig(work_dir=tmp_path), cleanup_interval_seconds=0)


def test_claude_config_is_required():
    with pytest.raises(TypeError, match="claude"):
        AppConfig()  # pyright: ignore[reportCallIssue]

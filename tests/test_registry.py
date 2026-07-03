from __future__ import annotations

import pytest

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.registry import AGENT_BUILDERS, build_agent
from agent_bridge.bridge import Bridge
from agent_bridge.config_loader import ConfigSource
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.registry import PLATFORM_BUILDERS, build_platforms
from agent_bridge.platforms.slack.adapter import SlackAdapter
from agent_bridge.session import SessionManager


@pytest.fixture
def bridge(tmp_path):
    session_manager = SessionManager(tmp_path / "sessions.json", ttl_hours=1)
    controller = build_agent(
        "claude", ConfigSource({"agents": {"claude": {"work_dir": str(tmp_path)}}}, env={})
    )
    return Bridge(session_manager, controller)


@pytest.fixture
def session_manager(tmp_path):
    return SessionManager(tmp_path / "sessions.json", ttl_hours=1)


# --- Agent registry ---


def test_build_claude_agent(tmp_path):
    source = ConfigSource({"agents": {"claude": {"work_dir": str(tmp_path)}}}, env={})
    controller = build_agent("claude", source)
    assert isinstance(controller, ClaudeController)


def test_unknown_agent_lists_available_names():
    with pytest.raises(ValueError) as exc_info:
        build_agent("gpt9000", ConfigSource({}, env={}))
    message = str(exc_info.value)
    assert "gpt9000" in message
    for name in AGENT_BUILDERS:
        assert name in message


# --- Platform registry ---


def test_slack_builder_returns_none_without_tokens(bridge, session_manager):
    adapter = PLATFORM_BUILDERS["slack"](ConfigSource({}, env={}), bridge, session_manager)
    assert adapter is None


def test_slack_builder_returns_adapter_with_tokens(bridge, session_manager):
    source = ConfigSource(
        {"platforms": {"slack": {"bot_token": "xoxb-t", "app_token": "xapp-t"}}}, env={}
    )
    adapter = PLATFORM_BUILDERS["slack"](source, bridge, session_manager)
    assert isinstance(adapter, SlackAdapter)


def test_heartbeat_builder_returns_none_when_disabled(bridge, session_manager):
    adapter = PLATFORM_BUILDERS["heartbeat"](
        ConfigSource({}, env={}), bridge, session_manager
    )
    assert adapter is None


def test_heartbeat_builder_returns_adapter_when_enabled(bridge, session_manager):
    source = ConfigSource(
        {"platforms": {"heartbeat": {"enabled": True, "interval_minutes": 5, "prompt": "go"}}},
        env={},
    )
    adapter = PLATFORM_BUILDERS["heartbeat"](source, bridge, session_manager)
    assert isinstance(adapter, HeartbeatAdapter)


def test_build_platforms_collects_only_active(bridge, session_manager):
    source = ConfigSource(
        {"platforms": {"heartbeat": {"enabled": True, "interval_minutes": 5, "prompt": "go"}}},
        env={},
    )
    adapters = build_platforms(source, bridge, session_manager)
    assert len(adapters) == 1
    assert isinstance(adapters[0], HeartbeatAdapter)


def test_build_platforms_empty_when_nothing_configured(bridge, session_manager):
    assert build_platforms(ConfigSource({}, env={}), bridge, session_manager) == []

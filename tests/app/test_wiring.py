"""App wiring: _build_dedupe and _build_adapters read config into components."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import app
from agent_bridge.bridge.config import BridgeConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.slack.adapter import SlackAdapter
from tests.fakes import FakeAgentController

# --- _build_dedupe ---


def test_build_dedupe_disabled_by_default():
    assert app._build_dedupe(BridgeConfig()) is None


def test_build_dedupe_enabled_returns_cache():
    config = BridgeConfig(
        dedupe_ttl_seconds=60.0, dedupe_max_entries=32, dedupe_simhash_threshold=4
    )
    assert isinstance(app._build_dedupe(config), PromptDedupeCache)


# --- _build_adapters ---


@pytest.fixture
def wiring(tmp_path: Path) -> tuple[Bridge, SessionManager]:
    session_manager = SessionManager(
        store_path=tmp_path / "sessions.json", ttl_hours=1.0
    )
    return Bridge(session_manager, FakeAgentController()), session_manager


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Neutral baseline — no slack, no heartbeat — immune to any local .env."""
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("AGENT_BRIDGE_SLACK_APP_TOKEN", "")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", "false")
    monkeypatch.setenv(
        "AGENT_BRIDGE_HEARTBEAT_STATE_PATH", str(tmp_path / "heartbeat.json")
    )
    return monkeypatch


def _enable_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_PROMPT", "tick")


def test_no_adapter_configured_raises(
    wiring: tuple[Bridge, SessionManager], platform_env: pytest.MonkeyPatch
):
    with pytest.raises(ValueError, match="No platform adapter configured"):
        app._build_adapters(*wiring)


def test_slack_only(
    wiring: tuple[Bridge, SessionManager], platform_env: pytest.MonkeyPatch
):
    platform_env.setenv("AGENT_BRIDGE_SLACK_BOT_TOKEN", "xoxb-x")
    platform_env.setenv("AGENT_BRIDGE_SLACK_APP_TOKEN", "xapp-x")

    slack, adapters = app._build_adapters(*wiring)

    assert isinstance(slack, SlackAdapter)
    assert adapters == [slack]


def test_heartbeat_only(
    wiring: tuple[Bridge, SessionManager], platform_env: pytest.MonkeyPatch
):
    _enable_heartbeat(platform_env)

    slack, adapters = app._build_adapters(*wiring)

    assert slack is None
    assert len(adapters) == 1
    assert isinstance(adapters[0], HeartbeatAdapter)


def test_slack_and_heartbeat(
    wiring: tuple[Bridge, SessionManager], platform_env: pytest.MonkeyPatch
):
    platform_env.setenv("AGENT_BRIDGE_SLACK_BOT_TOKEN", "xoxb-x")
    platform_env.setenv("AGENT_BRIDGE_SLACK_APP_TOKEN", "xapp-x")
    _enable_heartbeat(platform_env)

    slack, adapters = app._build_adapters(*wiring)

    assert isinstance(slack, SlackAdapter)
    assert len(adapters) == 2
    assert adapters[0] is slack
    assert isinstance(adapters[1], HeartbeatAdapter)

"""App wiring: _build_dedupe and _build_adapters turn an AppConfig into components.

Both are pure functions of the config now — no environment variables involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import app
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.bridge.config import DedupeConfig, RouterConfig, SessionConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.config import AppConfig
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.adapter import SlackAdapter
from agent_bridge.platforms.slack.config import SlackConfig
from tests.fakes import FakeAgentController

# --- _build_dedupe ---


def test_build_dedupe_disabled_by_default():
    assert app._build_dedupe(DedupeConfig()) is None


def test_build_dedupe_enabled_returns_cache():
    config = DedupeConfig(ttl_seconds=60.0, max_entries=32, simhash_threshold=4)
    assert isinstance(app._build_dedupe(config), PromptDedupeCache)


# --- _build_adapters ---


@pytest.fixture
def wiring(tmp_path: Path) -> tuple[Bridge, SessionManager]:
    session_manager = SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=1.0)
    )
    bridge = Bridge(RouterConfig(), session_manager, FakeAgentController())
    return bridge, session_manager


def _config(
    tmp_path: Path, *, slack: bool = False, heartbeat: bool = False
) -> AppConfig:
    return AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        slack=SlackConfig(bot_token="xoxb-x", app_token="xapp-x") if slack else None,
        heartbeat=(
            HeartbeatConfig(interval_minutes=5, prompt="tick") if heartbeat else None
        ),
    )


def test_no_adapter_configured_raises(
    wiring: tuple[Bridge, SessionManager], tmp_path: Path
):
    with pytest.raises(ValueError, match="No platform adapter configured"):
        app._build_adapters(_config(tmp_path), *wiring)


def test_slack_only(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(_config(tmp_path, slack=True), *wiring)

    assert len(adapters) == 1
    assert isinstance(adapters[0], SlackAdapter)


def test_heartbeat_only(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(_config(tmp_path, heartbeat=True), *wiring)

    assert len(adapters) == 1
    assert isinstance(adapters[0], HeartbeatAdapter)


def test_slack_and_heartbeat(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(
        _config(tmp_path, slack=True, heartbeat=True), *wiring
    )

    assert len(adapters) == 2
    assert isinstance(adapters[0], SlackAdapter)
    assert isinstance(adapters[1], HeartbeatAdapter)

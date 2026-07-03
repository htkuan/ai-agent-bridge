"""Registry + ConfigSource assemble a full deployment from one YAML file.

Mirrors app.main()'s wiring steps without starting any real connection:
heartbeat is the only enabled platform and its adapter lifecycle is exercised
with a fresh state file so no tick fires during the test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.registry import build_agent
from agent_bridge.bridge import Bridge
from agent_bridge.config import BridgeConfig
from agent_bridge.config_loader import load_config_source
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.registry import build_platforms
from agent_bridge.session import SessionManager

pytestmark = pytest.mark.integration


def _write_yaml(tmp_path: Path) -> Path:
    config_file = tmp_path / "agent-bridge.yaml"
    config_file.write_text(
        f"""\
agent: claude
bridge:
  session_store_path: {tmp_path / "sessions.json"}
  max_concurrent_sessions: 2
platforms:
  heartbeat:
    enabled: true
    interval_minutes: 60
    prompt: check the queue
    state_path: {tmp_path / "heartbeat.json"}
agents:
  claude:
    work_dir: {tmp_path}
"""
    )
    return config_file


@pytest.fixture
def source(tmp_path, clean_agent_bridge_env):
    return load_config_source(_write_yaml(tmp_path))


def test_wiring_assembles_agent_bridge_and_platforms(source, tmp_path):
    # Same steps app.main() performs, minus lifecycle/signals.
    bridge_config = BridgeConfig.from_source(source)
    agent_name = source.get("AGENT_BRIDGE_AGENT", "agent", "claude") or "claude"
    controller = build_agent(agent_name, source)
    session_manager = SessionManager(
        bridge_config.session_store_path, bridge_config.session_ttl_hours
    )
    bridge = Bridge(
        session_manager, controller, max_concurrent=bridge_config.max_concurrent_sessions
    )
    adapters = build_platforms(source, bridge, session_manager)

    assert isinstance(controller, ClaudeController)
    assert bridge_config.session_store_path == tmp_path / "sessions.json"
    # Slack has no tokens configured → only heartbeat is built.
    assert len(adapters) == 1
    assert isinstance(adapters[0], HeartbeatAdapter)


async def test_wired_heartbeat_adapter_lifecycle(source, tmp_path):
    controller = build_agent("claude", source)
    session_manager = SessionManager(tmp_path / "sessions.json")
    bridge = Bridge(session_manager, controller, max_concurrent=2)
    adapters = build_platforms(source, bridge, session_manager)
    [adapter] = adapters

    # Recent state file → the loop sleeps instead of firing the (real) agent.
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()})
    )

    await adapter.start()
    await adapter.stop()


def test_wiring_with_nothing_configured_builds_no_platforms(
    tmp_path, clean_agent_bridge_env
):
    empty = tmp_path / "empty.yaml"
    empty.write_text(f"agents:\n  claude:\n    work_dir: {tmp_path}\n")
    source = load_config_source(empty)
    controller = build_agent("claude", source)
    session_manager = SessionManager(tmp_path / "sessions.json")
    bridge = Bridge(session_manager, controller)

    # app.main() turns this empty list into a startup error.
    assert build_platforms(source, bridge, session_manager) == []

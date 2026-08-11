"""E2E: one heartbeat round through the real bridge and fake CLI."""

from __future__ import annotations

import json
from pathlib import Path

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge import Bridge
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.session import SessionManager
from tests.e2e.stack import wait_until
from tests.fakes import claude_cli


async def test_heartbeat_round_runs_prompt_and_leaves_no_session(tmp_path: Path):
    cli = claude_cli.install(
        tmp_path / "fake-claude", claude_cli.reply_steps("rounds done")
    )
    session_manager = SessionManager(
        store_path=tmp_path / "sessions.json", ttl_hours=1.0
    )
    bridge = Bridge(session_manager, ClaudeController(cli.config), max_concurrent=2)
    config = HeartbeatConfig(
        enabled=True,
        interval_minutes=60,
        prompt="do the rounds",
        state_path=tmp_path / "heartbeat.json",
    )
    adapter = HeartbeatAdapter(config, bridge)

    # No prior state → the first tick fires immediately on start.
    await adapter.start()
    try:
        await wait_until(config.state_path.exists)
    finally:
        await adapter.stop()

    # The tick ran the configured prompt through the real controller.
    (argv,) = cli.invocations()
    assert argv[argv.index("-p") + 1] == "do the rounds"
    # Heartbeat sessions are ephemeral: fresh --session-id, never resumed,
    # and nothing persisted for the tick.
    assert "--session-id" in argv
    assert "--resume" not in argv
    assert session_manager.list_sessions() == {}

    # The tick was recorded so a restart doesn't immediately re-fire.
    assert json.loads(config.state_path.read_text())["last_run"]

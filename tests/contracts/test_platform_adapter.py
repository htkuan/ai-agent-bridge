"""Contract suite for the ``PlatformAdapter`` protocol.

Every implementation must conform structurally (the typed fixture return is
the check — this directory is pyright-strict), survive a start → stop cycle,
and answer ``cleanup()`` with a non-negative count, repeatably.

``SlackAdapter`` is deliberately absent: its ``start()`` opens a real Socket
Mode connection; its lifecycle is covered in
``tests/platforms/slack/test_lifecycle.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_bridge.bridge.protocols import PlatformAdapter
from agent_bridge.platforms.base import BasePlatformAdapter
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from tests.fakes import FakeBridge, FakePlatformAdapter


@pytest.fixture(params=["fake", "base", "heartbeat"])
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> PlatformAdapter:
    if request.param == "fake":
        return FakePlatformAdapter()
    if request.param == "base":
        return BasePlatformAdapter[None](FakeBridge())
    state_path = tmp_path / "heartbeat.json"
    # A just-fired state file so start() schedules the first tick a full
    # interval out instead of firing during the test.
    state_path.write_text(json.dumps({"last_run": datetime.now(UTC).isoformat()}))
    return HeartbeatAdapter(
        HeartbeatConfig(interval_minutes=60, prompt="tick", state_path=state_path),
        FakeBridge(),
    )


async def test_start_stop_cycle_completes(adapter: PlatformAdapter) -> None:
    await adapter.start()
    await adapter.stop()


async def test_cleanup_returns_nonnegative_count(adapter: PlatformAdapter) -> None:
    for _ in range(2):
        removed = await adapter.cleanup()
        assert removed >= 0

"""Shared builder: a HeartbeatAdapter with its log output captured.

Heartbeat is the unattended archetype — its Surface is the logger, so the
harness's recorder is a logging handler attached to the adapter's module
logger. ``output()`` is what an operator would read in the log.

``deliver()`` calls ``_fire_once`` rather than waiting out ``start()``'s
timer: that *is* the trigger (the loop's body), and driving it directly
keeps the turn tests free of real sleeps. The scheduling around it — restart
catch-up, stop-during-sleep — is what ``test_adapter.py`` exercises through
the real ``start()``/``stop()`` pair.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from tests.fakes import FakeBridge

LOGGER_NAME = "agent_bridge.platforms.heartbeat.adapter"


class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class HeartbeatHarness:
    adapter: HeartbeatAdapter
    bridge: FakeBridge
    config: HeartbeatConfig
    recorder: _Recorder = field(default_factory=_Recorder)

    # --- the shared PlatformHarness shape ---

    async def deliver(self) -> None:
        # The loop body is the trigger; calling it directly is what keeps
        # turn tests free of real sleeps.
        await self.adapter._fire_once()  # pyright: ignore[reportPrivateUsage]

    def requests(self) -> list[BridgeRequest]:
        return self.bridge.calls

    def output(self) -> list[str]:
        return [record.getMessage() for record in self.recorder.records]

    # --- heartbeat-specific ---

    def records_at(self, level: int) -> list[logging.LogRecord]:
        return [r for r in self.recorder.records if r.levelno == level]


@contextlib.asynccontextmanager
async def heartbeat_harness(
    tmp_path: Path,
    *,
    events: list[BridgeEvent] | None = None,
    capacity_full: bool = False,
    known_agents: frozenset[str] = frozenset(),
    raises: bool = False,
    config: HeartbeatConfig | None = None,
    interval_minutes: int = 60,
    prompt: str = "ping",
    agent: str | None = None,
) -> AsyncGenerator[HeartbeatHarness]:
    config = config or HeartbeatConfig(
        interval_minutes=interval_minutes,
        prompt=prompt,
        state_path=tmp_path / "heartbeat.json",
        agent=agent,
    )
    bridge = FakeBridge(
        events, capacity_full=capacity_full, known_agents=known_agents, raises=raises
    )
    harness = HeartbeatHarness(HeartbeatAdapter(config, bridge), bridge, config)

    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(harness.recorder)
    # The recorder decides what it keeps, not the logger — but a record only
    # reaches a handler if the logger admits it, and another test may have
    # left the root at WARNING (app.run() calls basicConfig). setLevel, not a
    # bare `.level =`: only the former clears logging's isEnabledFor cache,
    # so a stale False would otherwise swallow INFO and DEBUG here.
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield harness
    finally:
        logger.removeHandler(harness.recorder)
        logger.setLevel(previous)
        await harness.adapter.stop()

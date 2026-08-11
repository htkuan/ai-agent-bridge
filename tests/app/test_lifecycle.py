"""App lifecycle: the periodic cleanup loop and main()'s signal shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

import agent_bridge
from agent_bridge.bridge import Bridge
from agent_bridge.events import Usage
from agent_bridge.session import SessionManager
from tests.fakes import FakeAgentController, FakePlatformAdapter
from tests.platforms.slack.harness import build_harness


async def _wait_until(
    predicate: Callable[[], bool],
    # Deadline for a sync-predicate poll loop; asyncio.timeout can't help here.
    timeout: float = 5.0,  # noqa: ASYNC109
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met within timeout")
        await asyncio.sleep(0.01)


class _RecordingController:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    async def cleanup_session(self, session_id: str) -> None:
        self.cleaned.append(session_id)


class _ExplodingController:
    def __init__(self) -> None:
        self.calls = 0

    async def cleanup_session(self, session_id: str) -> None:
        self.calls += 1
        raise RuntimeError("worktree removal failed")


# --- _periodic_cleanup ---


async def test_cleanup_round_purges_sessions_usage_and_slack_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(agent_bridge, "CLEANUP_INTERVAL_SECONDS", 0.01)
    # Sessions expire (effectively) immediately.
    session_manager = SessionManager(store_path=tmp_path / "s.json", ttl_hours=1e-9)
    controller = _RecordingController()
    bridge = Bridge(session_manager, FakeAgentController())
    sid, _ = session_manager.get_or_create("slack:C1:1.0")
    bridge._session_usage[sid] = Usage(cost_usd=1.0)
    bridge._usage_tracked.add(sid)
    harness = build_harness(session_manager=session_manager)
    harness.adapter._get_state("slack:C1:1.0")  # stale once the session purges

    shutdown = asyncio.Event()
    task = asyncio.create_task(
        agent_bridge._periodic_cleanup(
            shutdown, session_manager, harness.adapter, bridge, controller
        )
    )
    try:
        await _wait_until(lambda: controller.cleaned == [sid])
    finally:
        shutdown.set()
        await task

    assert session_manager.get("slack:C1:1.0") is None
    assert sid not in bridge._session_usage
    assert harness.adapter._sessions == {}


async def test_cleanup_loop_survives_controller_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(agent_bridge, "CLEANUP_INTERVAL_SECONDS", 0.01)
    session_manager = SessionManager(store_path=tmp_path / "s.json", ttl_hours=1e-9)
    session_manager.get_or_create("slack:C1:1.0")
    controller = _ExplodingController()
    bridge = Bridge(session_manager, FakeAgentController())

    shutdown = asyncio.Event()
    task = asyncio.create_task(
        agent_bridge._periodic_cleanup(
            shutdown, session_manager, None, bridge, controller
        )
    )
    try:
        await _wait_until(lambda: controller.calls >= 1)
        await asyncio.sleep(0.05)  # give a crash time to surface
        assert not task.done()
    finally:
        shutdown.set()
        await task  # returns cleanly despite the failed cleanup


# --- main(): startup, signal handling, shutdown ---


async def test_main_starts_adapters_and_stops_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakePlatformAdapter()
    monkeypatch.setattr(
        agent_bridge, "_build_adapters", lambda bridge, sm: (None, [fake])
    )
    monkeypatch.setenv(
        "AGENT_BRIDGE_SESSION_STORE_PATH", str(tmp_path / "sessions.json")
    )
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))

    task = asyncio.create_task(agent_bridge.main())
    loop = asyncio.get_running_loop()
    try:
        # started == 1 implies the signal handlers are already installed:
        # main() registers them before starting any adapter.
        await _wait_until(lambda: fake.started == 1)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, RuntimeError):
                loop.remove_signal_handler(sig)

    assert fake.started == 1
    assert fake.stopped == 1

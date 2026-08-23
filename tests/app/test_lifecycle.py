"""App lifecycle: the periodic cleanup loop and main()'s signal shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge import app
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.bridge.config import BridgeConfig, RouterConfig, SessionConfig
from agent_bridge.bridge.events import Usage
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.config import AppConfig
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


async def test_cleanup_round_purges_sessions_usage_and_slack_state(tmp_path: Path):
    # Sessions expire (effectively) immediately.
    session_manager = SessionManager(
        SessionConfig(store_path=tmp_path / "s.json", ttl_hours=1e-9)
    )
    controller = _RecordingController()
    bridge = Bridge(RouterConfig(), session_manager, FakeAgentController())
    sid, _ = session_manager.get_or_create("slack:C1:1.0")
    bridge._session_usage[sid] = Usage(cost_usd=1.0)
    bridge._usage_tracked.add(sid)
    harness = build_harness(session_manager=session_manager)
    harness.adapter._get_state("slack:C1:1.0")  # stale once the session purges

    shutdown = asyncio.Event()
    task = asyncio.create_task(
        app._periodic_cleanup(
            0.01, shutdown, session_manager, [harness.adapter], bridge, [controller]
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


async def test_cleanup_loop_survives_controller_errors(tmp_path: Path):
    session_manager = SessionManager(
        SessionConfig(store_path=tmp_path / "s.json", ttl_hours=1e-9)
    )
    session_manager.get_or_create("slack:C1:1.0")
    controller = _ExplodingController()
    bridge = Bridge(RouterConfig(), session_manager, FakeAgentController())

    shutdown = asyncio.Event()
    task = asyncio.create_task(
        app._periodic_cleanup(0.01, shutdown, session_manager, [], bridge, [controller])
    )
    try:
        await _wait_until(lambda: controller.calls >= 1)
        await asyncio.sleep(0.05)  # give a crash time to surface
        assert not task.done()
    finally:
        shutdown.set()
        await task  # returns cleanly despite the failed cleanup


# --- run(): profile prerequisite probes fail fast, naming the profile ---


async def test_run_fails_fast_on_bad_claude_profile_prereqs(tmp_path: Path):
    config = AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        claude_profiles={"backend": ClaudeConfig(work_dir=tmp_path / "nope")},
    )
    with pytest.raises(ValueError, match=r"claude\.profiles\.backend"):
        await app.run(config)


async def test_run_fails_fast_on_bad_pi_profile_prereqs(tmp_path: Path):
    config = AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        pi_profiles={"fast": PiConfig(work_dir=tmp_path / "nope")},
    )
    with pytest.raises(ValueError, match=r"pi\.profiles\.fast"):
        await app.run(config)


async def test_run_fails_fast_on_bad_codex_profile_prereqs(tmp_path: Path):
    config = AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        codex_profiles={"reviewer": CodexConfig(work_dir=tmp_path / "nope")},
    )
    with pytest.raises(ValueError, match=r"codex\.profiles\.reviewer"):
        await app.run(config)


async def test_run_fails_fast_on_bad_opencode_profile_prereqs(tmp_path: Path):
    config = AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        opencode_profiles={"oc": OpencodeConfig(work_dir=tmp_path / "nope")},
    )
    with pytest.raises(ValueError, match=r"opencode\.profiles\.oc"):
        await app.run(config)


# --- run(): startup, signal handling, shutdown ---


async def test_run_starts_adapters_and_stops_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakePlatformAdapter()
    monkeypatch.setattr(
        app, "_build_adapters", lambda config, bridge, sm, http_server=None: [fake]
    )
    config = AppConfig(
        bridge=BridgeConfig(
            session=SessionConfig(store_path=tmp_path / "sessions.json")
        ),
        claude=ClaudeConfig(work_dir=tmp_path),
    )

    task = asyncio.create_task(app.run(config))
    loop = asyncio.get_running_loop()
    try:
        # started == 1 implies the signal handlers are already installed:
        # run() registers them before starting any adapter.
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


# --- main(): the only place the environment is read ---


async def test_run_probes_prerequisites_before_starting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The startup probe must run whatever built the config — from_env only
    parses, so run() is what guarantees a bad work_dir never reaches an adapter.
    """
    fake = FakePlatformAdapter()
    monkeypatch.setattr(
        app, "_build_adapters", lambda config, bridge, sm, http_server=None: [fake]
    )
    config = AppConfig(claude=ClaudeConfig(work_dir=tmp_path / "gone"))

    # Bounded: without the probe, run() reaches `await shutdown_event.wait()`
    # and this test would hang forever instead of failing.
    with pytest.raises(ValueError, match="does not exist"):
        async with asyncio.timeout(5):
            await app.run(config)

    assert fake.started == 0


def test_configure_logging_sets_the_level(monkeypatch: pytest.MonkeyPatch):
    seen: list[object] = []
    monkeypatch.setattr(
        app.logging, "basicConfig", lambda **kwargs: seen.append(kwargs["level"])
    )
    app._configure_logging("WARNING")
    assert seen == ["WARNING"]


async def test_main_reads_env_configures_logging_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = AppConfig(claude=ClaudeConfig(work_dir=tmp_path), log_level="WARNING")
    levels: list[str] = []
    ran: list[AppConfig] = []

    async def _run(cfg: AppConfig) -> None:
        ran.append(cfg)

    monkeypatch.setattr(AppConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(app, "_configure_logging", levels.append)
    monkeypatch.setattr(app, "run", _run)

    await app.main()

    assert levels == ["WARNING"]
    assert ran == [config]

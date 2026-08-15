"""Fixtures for the live e2e: the real ``claude`` CLI behind the same rig.

Every other scenario under ``tests/e2e/`` drives the scripted CLI from
``tests/fakes/claude_cli.py`` — fast, free, deterministic, but it can only
ever prove we handle the stream-json shape *we wrote down*. The scenarios in
``test_live_claude.py`` spawn the actual Claude Code CLI instead, so the argv
we build, the stream we parse and the session ids we resume are checked
against the thing itself.

They cost money, need ``claude`` authenticated (``claude login`` or
``ANTHROPIC_API_KEY``) and take tens of seconds, so they only run behind
``--live`` (declared in ``tests/conftest.py``, which also skips them when the
flag is absent):

    uv run pytest -m live --live --no-cov -v

The Slack update throttle is a SlackConfig field; tests/e2e/stack.py builds
every stack with a shrunk one so e2e turns don't idle 1.5s each. Throttle
behaviour itself is pinned by tests/platforms/slack/test_rendering.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from tests.e2e.stack import SlackStack, session_manager_for, wire_slack


@pytest.fixture
def live_claude_config(pytestconfig: pytest.Config, tmp_path: Path) -> ClaudeConfig:
    """The real CLI, sandboxed to a throwaway work dir.

    ``work_dir`` is the directory a real agent gets loose in, so it is a fresh
    tmp dir and never the repo — ``acceptEdits`` (the production default) lets
    it write there. ``effort=low`` keeps the bill down: these scenarios assert
    plumbing, not reasoning.
    """
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    return ClaudeConfig(
        work_dir=work_dir,
        permission_mode="acceptEdits",
        timeout_seconds=pytestconfig.getoption("live_timeout"),
        effort="low",
        cli_path=pytestconfig.getoption("live_cli"),
    )


@pytest.fixture
def live_controller(live_claude_config: ClaudeConfig) -> ClaudeController:
    return ClaudeController(live_claude_config)


@pytest.fixture
def live_stack(live_controller: ClaudeController, tmp_path: Path) -> SlackStack:
    """``build_stack``'s live twin: same wiring, real CLI behind it."""
    session_manager = session_manager_for(tmp_path)
    # A real turn takes seconds, so throttling updates gains nothing — and
    # un-throttled rendering makes the tool-status assertion deterministic.
    wiring = wire_slack(live_controller, session_manager, update_throttle_seconds=0.01)
    return SlackStack(
        adapter=wiring.adapter,
        app=wiring.app,
        client=wiring.client,
        bridge=wiring.bridge,
        session_manager=session_manager,
        controller=live_controller,
    )

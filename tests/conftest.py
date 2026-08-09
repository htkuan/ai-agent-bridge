from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.session import SessionManager
from tests.fakes import claude_cli
from tests.fakes.claude_cli import FakeClaudeCLI, Step

type FakeClaudeFactory = Callable[..., FakeClaudeCLI]


@pytest.fixture
def fake_claude(tmp_path: Path) -> FakeClaudeFactory:
    """Factory: materialise a scripted claude CLI and get its ClaudeConfig.

    Each call gets its own directory, so one test can install several
    scenarios (e.g. different behaviour per session).

        cli = fake_claude(claude_cli.reply_steps("hi"))
        controller = ClaudeController(cli.config)
    """
    counter = itertools.count()

    def factory(
        steps: list[Step],
        *,
        work_dir: Path | None = None,
        timeout_seconds: float = 600.0,
    ) -> FakeClaudeCLI:
        return claude_cli.install(
            tmp_path / f"fake-claude-{next(counter)}",
            steps,
            work_dir=work_dir,
            timeout_seconds=timeout_seconds,
        )

    return factory


@pytest.fixture
def session_manager(tmp_path: Path) -> SessionManager:
    """A real SessionManager persisting into the test's tmp_path."""
    return SessionManager(store_path=tmp_path / "sessions.json", ttl_hours=1.0)

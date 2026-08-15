from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.session import SessionManager
from tests.fakes import claude_cli
from tests.fakes.claude_cli import FakeClaudeCLI, Step

type FakeClaudeFactory = Callable[..., FakeClaudeCLI]

_E2E_DIR = Path(__file__).parent / "e2e"
_LAYER_MARKERS = ("unit", "integration", "e2e")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply layer markers: tests/e2e/ → ``e2e``; unmarked → ``unit``.

    ``integration`` is declared per module (``pytestmark``) where tests cross
    a process boundary, e.g. spawning the scripted claude CLI.
    """
    for item in items:
        if _E2E_DIR in item.path.parents:
            item.add_marker(pytest.mark.e2e)
        elif not any(item.get_closest_marker(name) for name in _LAYER_MARKERS):
            item.add_marker(pytest.mark.unit)


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
    return SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=1.0)
    )

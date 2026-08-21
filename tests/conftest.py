from __future__ import annotations

import itertools
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.session import SessionManager
from tests.fakes import claude_cli, pi_cli
from tests.fakes.claude_cli import FakeClaudeCLI, Step
from tests.fakes.pi_cli import FakePiCLI

type FakeClaudeFactory = Callable[..., FakeClaudeCLI]
type FakePiFactory = Callable[..., FakePiCLI]

_E2E_DIR = Path(__file__).parent / "e2e"
_LAYER_MARKERS = ("unit", "integration", "e2e")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Flags for the ``live`` scenarios (tests/e2e/test_live_claude.py).

    They spawn the real claude CLI, so they need a switch — a flag rather than
    an env var, which keeps the rule that no test reads the environment.
    """
    group = parser.getgroup("live", "live claude e2e")
    group.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run the `live` scenarios: spawns the real claude CLI, spends tokens",
    )
    group.addoption(
        "--live-cli",
        default="claude",
        metavar="PATH",
        help="claude CLI --live spawns (default: `claude`, resolved on PATH)",
    )
    group.addoption(
        "--live-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="per-turn budget for --live scenarios (default: 300)",
    )


def _live_skip(config: pytest.Config) -> pytest.MarkDecorator | None:
    """Why ``live`` tests can't run this session — None when they can.

    A missing CLI skips rather than fails: `pytest --live` runs the whole
    suite, and the rest of it is still worth reporting on.
    """
    if not config.getoption("live"):
        return pytest.mark.skip(reason="needs --live (spawns the real claude CLI)")
    cli: str = config.getoption("live_cli")
    if shutil.which(cli) is None:
        return pytest.mark.skip(reason=f"--live-cli {cli!r} not found on PATH")
    return None


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-apply layer markers: tests/e2e/ → ``e2e``; unmarked → ``unit``.
    Then gate anything marked ``live`` behind ``--live``.

    ``integration`` is declared per module (``pytestmark``) where tests cross
    a process boundary, e.g. spawning the scripted claude CLI. ``live`` is an
    opt-in flag on top of a layer, not a layer of its own.
    """
    skip_live = _live_skip(config)
    for item in items:
        if _E2E_DIR in item.path.parents:
            item.add_marker(pytest.mark.e2e)
        elif not any(item.get_closest_marker(name) for name in _LAYER_MARKERS):
            item.add_marker(pytest.mark.unit)
        if skip_live is not None and item.get_closest_marker("live"):
            item.add_marker(skip_live)


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
def fake_pi(tmp_path: Path) -> FakePiFactory:
    """Factory: materialise a scripted pi CLI and get its PiConfig.

    cli = fake_pi(pi_cli.reply_steps("hi"))
    controller = PiController(cli.config)
    """
    counter = itertools.count()

    def factory(
        steps: list[Step],
        *,
        work_dir: Path | None = None,
        timeout_seconds: float = 600.0,
    ) -> FakePiCLI:
        return pi_cli.install(
            tmp_path / f"fake-pi-{next(counter)}",
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

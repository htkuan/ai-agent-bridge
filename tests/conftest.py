from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.session import SessionManager
from tests.fakes import claude_cli, codex_cli, opencode_cli, pi_cli
from tests.fakes.claude_cli import FakeClaudeCLI, Step
from tests.fakes.codex_cli import FakeCodexCLI
from tests.fakes.opencode_cli import FakeOpencodeCLI
from tests.fakes.pi_cli import FakePiCLI

type FakeClaudeFactory = Callable[..., FakeClaudeCLI]
type FakeCodexFactory = Callable[..., FakeCodexCLI]
type FakeOpencodeFactory = Callable[..., FakeOpencodeCLI]
type FakePiFactory = Callable[..., FakePiCLI]

_E2E_DIR = Path(__file__).parent / "e2e"
_LAYER_MARKERS = ("unit", "integration", "e2e")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Flags for the ``live`` scenarios (tests/e2e/test_live_*.py).

    They spawn the real agent CLIs, so they need a switch — a flag rather than
    an env var, which keeps the rule that no test reads the environment.
    """
    group = parser.getgroup("live", "live agent e2e")
    group.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run the `live` scenarios: spawns the real agent CLIs, spends tokens",
    )
    group.addoption(
        "--live-cli",
        default="claude",
        metavar="PATH",
        help="claude CLI --live spawns (default: `claude`, resolved on PATH)",
    )
    group.addoption(
        "--live-pi-cli",
        default="pi",
        metavar="PATH",
        help="pi CLI --live spawns (default: `pi`, resolved on PATH)",
    )
    group.addoption(
        "--live-codex-cli",
        default="codex",
        metavar="PATH",
        help="codex CLI --live spawns (default: `codex`, resolved on PATH)",
    )
    group.addoption(
        "--live-opencode-cli",
        default="opencode",
        metavar="PATH",
        help="opencode CLI --live spawns (default: `opencode`, resolved on PATH)",
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

    Only the flag is gated here: each agent's CLI presence is probed by its
    ``live_*_config`` fixture (tests/e2e/conftest.py), so a missing claude
    CLI skips only the claude scenarios, not pi's — and vice versa.
    """
    if not config.getoption("live"):
        return pytest.mark.skip(reason="needs --live (spawns a real agent CLI)")
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
def fake_codex(tmp_path: Path) -> FakeCodexFactory:
    """Factory: materialise a scripted codex CLI and get its CodexConfig.

    cli = fake_codex(codex_cli.reply_steps("hi"))
    controller = CodexController(cli.config)
    """
    counter = itertools.count()

    def factory(
        steps: list[Step],
        *,
        work_dir: Path | None = None,
        timeout_seconds: float = 600.0,
    ) -> FakeCodexCLI:
        return codex_cli.install(
            tmp_path / f"fake-codex-{next(counter)}",
            steps,
            work_dir=work_dir,
            timeout_seconds=timeout_seconds,
        )

    return factory


@pytest.fixture
def fake_opencode(tmp_path: Path) -> FakeOpencodeFactory:
    """Factory: materialise a scripted opencode CLI and get its OpencodeConfig.

    cli = fake_opencode(opencode_cli.reply_steps("hi"))
    controller = OpencodeController(cli.config)
    """
    counter = itertools.count()

    def factory(
        steps: list[Step],
        *,
        work_dir: Path | None = None,
        timeout_seconds: float = 600.0,
    ) -> FakeOpencodeCLI:
        return opencode_cli.install(
            tmp_path / f"fake-opencode-{next(counter)}",
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

"""Fixtures for the live e2e: the real agent CLIs behind the same rigs.

Every other scenario under ``tests/e2e/`` drives a scripted CLI from
``tests/fakes/`` — fast, free, deterministic, but it can only ever prove we
handle the stream shapes *we wrote down*. The scenarios in
``test_live_controllers.py``, ``test_live_claude.py`` and
``test_live_webhook.py`` spawn the actual agent CLIs (claude, pi, codex,
opencode) instead, so the argv we build, the streams we parse and the
session ids we resume are checked against the thing itself.

They cost money, need the CLIs authenticated (``claude login`` /
``ANTHROPIC_API_KEY``; ``pi auth``; ``codex login``; ``opencode auth``) and
take tens of seconds, so they only run behind ``--live`` (declared in
``tests/conftest.py``, which skips them when the flag is absent). A missing
CLI skips just that agent's scenarios — probed here, per config fixture —
because ``pytest --live`` runs the whole suite and the rest is still worth
reporting on:

    uv run pytest -m live --live --no-cov -v

The Slack update throttle is a SlackConfig field; tests/e2e/stack.py builds
every stack with a shrunk one so e2e turns don't idle 1.5s each. Throttle
behaviour itself is pinned by tests/platforms/slack/test_rendering.py.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.controller import OpencodeController
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.protocols import AgentController
from tests.e2e.live_matrix import (
    FLAG_SPECS,
    LIVE_MATRIX,
    FlagSpec,
    LiveCase,
    Mutate,
    build_flags,
    model_override,
)
from tests.e2e.stack import (
    SlackStack,
    WebhookStack,
    session_manager_for,
    wire_slack,
    wire_webhook,
)

# Any id works for deriving argv — nothing is spawned with it.
_FLAG_PROBE_SESSION = "flag-probe-session"
_CLI_VERSIONS: dict[str, str] = {}


def _cli_or_skip(pytestconfig: pytest.Config, option: str) -> str:
    """The CLI path an option names — or skip this agent's scenarios."""
    cli: str = pytestconfig.getoption(option)
    if shutil.which(cli) is None:
        pytest.skip(f"--{option.replace('_', '-')} {cli!r} not found on PATH")
    return cli


# --- claude ---


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
        cli_path=_cli_or_skip(pytestconfig, "live_cli"),
    )


@pytest.fixture
def live_claude_controller(live_claude_config: ClaudeConfig) -> ClaudeController:
    return ClaudeController(live_claude_config)


@pytest.fixture
def live_stack(live_claude_controller: ClaudeController, tmp_path: Path) -> SlackStack:
    """``build_stack``'s live twin: same wiring, real CLI behind it."""
    session_manager = session_manager_for(tmp_path)
    # A real turn takes seconds, so throttling updates gains nothing — and
    # un-throttled rendering makes the tool-status assertion deterministic.
    wiring = wire_slack(
        live_claude_controller, session_manager, update_throttle_seconds=0.01
    )
    return SlackStack(
        adapter=wiring.adapter,
        app=wiring.app,
        client=wiring.client,
        bridge=wiring.bridge,
        session_manager=session_manager,
        controller=live_claude_controller,
    )


# --- pi ---


@pytest.fixture
def live_pi_config(pytestconfig: pytest.Config, tmp_path: Path) -> PiConfig:
    """The real pi CLI, sandboxed like claude's. Provider/model/thinking stay
    None so pi's own settings decide — same as an unset production base."""
    work_dir = tmp_path / "pi-workspace"
    work_dir.mkdir()
    return PiConfig(
        work_dir=work_dir,
        timeout_seconds=pytestconfig.getoption("live_timeout"),
        cli_path=_cli_or_skip(pytestconfig, "live_pi_cli"),
    )


@pytest.fixture
def live_pi_controller(live_pi_config: PiConfig) -> PiController:
    return PiController(live_pi_config)


# --- codex ---


@pytest.fixture
def live_codex_config(pytestconfig: pytest.Config, tmp_path: Path) -> CodexConfig:
    """The real codex CLI, sandboxed like claude's. Model/effort stay None so
    codex's own settings decide — same as an unset production base."""
    work_dir = tmp_path / "codex-workspace"
    work_dir.mkdir()
    return CodexConfig(
        work_dir=work_dir,
        timeout_seconds=pytestconfig.getoption("live_timeout"),
        cli_path=_cli_or_skip(pytestconfig, "live_codex_cli"),
        # The throwaway work dir is not a git repo, and initialising one just
        # to appease the probe would exercise git, not codex.
        skip_git_repo_check=True,
        # Outside work_dir, so "nothing appeared in the work dir" assertions
        # see only what the agent itself wrote — not the controller's map.
        session_map_path=tmp_path / "codex-sessions.json",
    )


@pytest.fixture
def live_codex_git_config(
    live_codex_config: CodexConfig, tmp_path: Path
) -> CodexConfig:
    """The production shape: a git work dir and no skip flag, so codex's
    trusted-directory probe runs for real — what ``check_prerequisites``
    promises at boot."""
    repo = tmp_path / "codex-git-workspace"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    config = replace(live_codex_config, work_dir=repo, skip_git_repo_check=False)
    config.check_prerequisites()
    return config


# --- opencode ---


@pytest.fixture
def live_opencode_config(pytestconfig: pytest.Config, tmp_path: Path) -> OpencodeConfig:
    """The real opencode CLI, sandboxed like claude's. Model/variant stay None
    so opencode's own settings decide — same as an unset production base."""
    work_dir = tmp_path / "opencode-workspace"
    work_dir.mkdir()
    return OpencodeConfig(
        work_dir=work_dir,
        timeout_seconds=pytestconfig.getoption("live_timeout"),
        cli_path=_cli_or_skip(pytestconfig, "live_opencode_cli"),
        # Outside work_dir for the same reason as codex's (see above).
        session_map_path=tmp_path / "opencode-sessions.json",
    )


# --- claude, worktree mode ---


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def live_claude_worktree_config(
    pytestconfig: pytest.Config, tmp_path: Path
) -> ClaudeConfig:
    """Worktree mode's prerequisites built for real: a git work dir with an
    origin remote whose ``origin/HEAD`` resolves (what ``check_prerequisites``
    demands at boot)."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("live worktree sandbox\n")
    _git(repo, "add", "README.md")
    author = ["-c", "user.email=live@test", "-c", "user.name=live"]
    _git(repo, *author, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "--auto")
    config = ClaudeConfig(
        work_dir=repo,
        permission_mode="acceptEdits",
        timeout_seconds=pytestconfig.getoption("live_timeout"),
        effort="low",
        cli_path=_cli_or_skip(pytestconfig, "live_cli"),
        worktree_enabled=True,
    )
    config.check_prerequisites()  # the same probe app.run() trusts at boot
    return config


# --- bare controller x every agent (test_live_controllers.py) ---


@dataclass(frozen=True)
class LiveControllerRig:
    """One real CLI behind its bare controller — no bridge or platform."""

    name: str
    controller: AgentController
    work_dir: Path
    cli_path: str
    # Codex reports no cost; opencode's is provider-dependent (0.0 under
    # subscription auth). Only agents proven to report it get asserted on.
    reports_cost: bool


# name → (config fixture, controller class, reports_cost). The rig builds the
# controller from the config directly, so derived rigs (a shrunk timeout, a
# flipped knob) are one `replace()` away.
_LIVE_CONTROLLERS = {
    "claude": ("live_claude_config", ClaudeController, True),
    "pi": ("live_pi_config", PiController, True),
    "codex": ("live_codex_config", CodexController, False),
    "opencode": ("live_opencode_config", OpencodeController, False),
}

# The agents this layer knows about — what the matrix must stay in step with
# (pinned by tests/e2e/test_live_matrix_spec.py, no CLI needed).
LIVE_CONTROLLER_AGENTS: tuple[str, ...] = tuple(sorted(_LIVE_CONTROLLERS))


def _rig_for(
    request: pytest.FixtureRequest,
    agent: str,
    *,
    timeout_seconds: float | None = None,
    mutate: Mutate | None = None,
    base_fixture: str | None = None,
) -> LiveControllerRig:
    config_name, controller_cls, reports_cost = _LIVE_CONTROLLERS[agent]
    config = request.getfixturevalue(base_fixture or config_name)
    if timeout_seconds is not None:
        config = replace(config, timeout_seconds=timeout_seconds)
    if mutate is not None:
        config = mutate(config)
    return LiveControllerRig(
        name=agent,
        controller=controller_cls(config),
        work_dir=config.work_dir,
        cli_path=config.cli_path,
        reports_cost=reports_cost,
    )


@pytest.fixture(params=sorted(_LIVE_CONTROLLERS))
def live_controller_rig(request: pytest.FixtureRequest) -> LiveControllerRig:
    """The bare controller for the real CLI ``request.param`` names."""
    return _rig_for(request, request.param)


@pytest.fixture(params=sorted(_LIVE_CONTROLLERS))
def live_short_timeout_rig(request: pytest.FixtureRequest) -> LiveControllerRig:
    """Same agents, but a deadline no real turn can meet — the engine's
    timeout path (deadline, tree kill, error Completion) against each CLI."""
    return _rig_for(request, request.param, timeout_seconds=1.0)


# --- the config-axis matrix (tests/e2e/live_matrix.py) ---


@dataclass(frozen=True)
class LiveMatrixRig(LiveControllerRig):
    """A rig with one config knob flipped, plus the row that flipped it."""

    case: LiveCase


_MATRIX_PARAMS = [
    pytest.param(
        (agent, case),
        id=f"{agent}-{case.name}",
        marks=pytest.mark.live_tier(case.tier),
    )
    for agent in sorted(LIVE_MATRIX)
    for case in LIVE_MATRIX[agent]
]


@pytest.fixture(params=_MATRIX_PARAMS)
def live_matrix_rig(request: pytest.FixtureRequest) -> LiveMatrixRig:
    """One matrix row: the agent's live config with that row's knob applied."""
    agent, case = request.param
    mutate = case.mutate
    if case.model_from_option:
        # Which models an account can reach is local, so this row exists for
        # every agent but only runs for the ones the operator named.
        model = model_override(request.config.getoption("live_model"), agent)
        if model is None:
            pytest.skip(f"needs --live-model {agent}=<model>")

        def with_model(config: Any) -> Any:
            return replace(case.mutate(config), model=model)

        mutate = with_model
    rig = _rig_for(request, agent, mutate=mutate, base_fixture=case.base)
    return LiveMatrixRig(
        name=rig.name,
        controller=rig.controller,
        work_dir=rig.work_dir,
        cli_path=rig.cli_path,
        reports_cost=rig.reports_cost,
        case=case,
    )


# --- tier 0: the CLI itself, no tokens spent ---


@dataclass(frozen=True)
class FlagCheck:
    """The flags one ``build_command`` branch emits, and the help page that
    has to list them."""

    source: str
    help_args: tuple[str, ...]
    flags: tuple[str, ...]


@dataclass(frozen=True)
class LiveFlagProbe:
    name: str
    cli_path: str
    checks: tuple[FlagCheck, ...]


def _checks_for(agent: str, config: object, spec: FlagSpec) -> tuple[FlagCheck, ...]:
    _, controller_cls, _ = _LIVE_CONTROLLERS[agent]
    checks: list[FlagCheck] = []
    for index, mutate in enumerate(spec.configs):
        flag_config = mutate(config)
        for page in spec.pages:
            if not page.is_new and spec.seeded_handle is not None:
                # The resume branch only triggers when a handle is stored, and
                # the store is read at construction — seed, then build.
                path = Path(flag_config.resolved_session_map_path)  # pyright: ignore[reportAttributeAccessIssue]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({_FLAG_PROBE_SESSION: spec.seeded_handle}))
            argv = controller_cls(flag_config).build_command(
                _FLAG_PROBE_SESSION,
                "probe prompt",
                page.is_new,
                system_prompt="probe directives",
            )
            branch = "new-session" if page.is_new else "resume"
            checks.append(
                FlagCheck(
                    source=f"config#{index} {branch}",
                    help_args=page.args,
                    flags=build_flags(argv),
                )
            )
    return tuple(checks)


@pytest.fixture(params=sorted(FLAG_SPECS))
def live_flag_probe(request: pytest.FixtureRequest) -> LiveFlagProbe:
    """Everything tier 0 needs for one agent: its CLI path, and the flags each
    of its help pages must still list. Builds argv only — nothing is spawned
    with these configs, so no tokens are spent."""
    agent: str = request.param
    config_name, _, _ = _LIVE_CONTROLLERS[agent]
    config = request.getfixturevalue(config_name)
    spec = FLAG_SPECS[agent]
    return LiveFlagProbe(
        name=agent,
        cli_path=config.cli_path,
        checks=_checks_for(agent, config, spec),
    )


def record_cli_version(agent: str, version: str) -> None:
    """Remember an agent CLI's version for the terminal summary, so a failing
    live run says which CLI build it was talking to."""
    _CLI_VERSIONS[agent] = version


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _CLI_VERSIONS:
        return
    terminalreporter.write_sep("-", "live agent CLI versions")
    for agent in sorted(_CLI_VERSIONS):
        terminalreporter.write_line(f"{agent}: {_CLI_VERSIONS[agent]}")


# --- webhook → bridge → agent ---


@contextlib.asynccontextmanager
async def _webhook_stack(
    controller: AgentController, work_dir: Path, tmp_path: Path
) -> AsyncIterator[WebhookStack]:
    """Assemble and start a WebhookStack; tear it down on exit."""
    session_manager = session_manager_for(tmp_path)
    wiring = wire_webhook(controller, session_manager)
    stack = WebhookStack(
        adapter=wiring.adapter,
        http=wiring.http,
        bridge=wiring.bridge,
        session_manager=session_manager,
        callbacks=wiring.callbacks,
        work_dir=work_dir,
    )
    await stack.adapter.start()
    try:
        yield stack
    finally:
        await stack.adapter.stop()
        await stack.http.aclose()


# The webhook adapter always routes to the bridge's default controller
# (WebhookMessage carries no agent field), so each agent gets its own stack
# with that agent as the default — the scenarios run once per agent.
_LIVE_AGENTS = {
    "claude": ("live_claude_config", "live_claude_controller"),
    "pi": ("live_pi_config", "live_pi_controller"),
}


@pytest.fixture(params=sorted(_LIVE_AGENTS))
async def live_webhook_stack(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[WebhookStack]:
    """Webhook → Bridge → the real CLI ``request.param`` names."""
    config_name, controller_name = _LIVE_AGENTS[request.param]
    config = request.getfixturevalue(config_name)
    controller = request.getfixturevalue(controller_name)
    async with _webhook_stack(controller, config.work_dir, tmp_path) as stack:
        yield stack


@pytest.fixture
async def live_webhook_pi_readonly(
    live_pi_config: PiConfig, tmp_path: Path
) -> AsyncIterator[WebhookStack]:
    """A pi profile restricted to read-only tools — its permission model."""
    config = replace(live_pi_config, tools=("read", "ls"))
    async with _webhook_stack(PiController(config), config.work_dir, tmp_path) as stack:
        yield stack

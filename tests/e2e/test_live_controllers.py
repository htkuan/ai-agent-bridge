"""E2E: every real agent CLI behind its bare ``CliAgentController`` — opt-in.

    uv run pytest -m live --live --no-cov -v            # tiers 0-2
    uv run pytest -m live --live --live-tier=0 --no-cov # free, after a CLI upgrade

Skipped without ``--live``; a missing CLI skips just that agent's scenarios
(``--live-cli`` / ``--live-pi-cli`` / ``--live-codex-cli`` /
``--live-opencode-cli`` — flags in ``tests/conftest.py``, rigs in
``conftest.py`` here).

Where ``test_live_claude.py`` and ``test_live_webhook.py`` prove whole
platform paths, these scenarios isolate the controller seam itself: config
in, ``BridgeEvent``s out, no bridge and no platform in between — so a failure
points at the agent implementation or at the CLI version, and nothing else.

The layer has three kinds of scenario, split by tier (see
``tests/e2e/live_matrix.py`` for the tier table and the rows themselves):

- **tier 0**, no tokens: the CLI's version is recorded, and its own ``--help``
  still lists every flag ``build_command`` emits — on both the new-session and
  the resume branch. This is the drift class that has actually bitten us
  (codex dropping ``--skip-git-repo-check`` from the resume branch).
- **tier 1**, one turn per row: the real CLI accepts the argv one flipped
  config knob produces.
- **tier 2**: behaviour. The shared scenarios below run once per agent and pin
  what the scripted CLIs replay by construction instead of implementing — the
  stream shape each parser reads (with real usage numbers), the
  exactly-one-``Completion`` engine guarantee, session resume (claude
  ``--resume``, pi ``--session-id`` reattach, codex/opencode through the
  ``SessionHandleStore`` round-trip), system-prompt delivery (native flag for
  claude/pi, stdin folding for codex/opencode), and real tool use surfacing as
  ``StatusUpdate``s. The matrix's tier-2 rows add each agent's restriction
  knob, and two knobs too bespoke for a row stay hand-written here.

Prompts are written to force a token we can assert on. Everything else about
the reply is the model's business — never assert on its prose.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    StatusUpdate,
    TextDelta,
    Usage,
)
from agent_bridge.bridge.protocols import AgentController
from tests.e2e.conftest import (
    LiveControllerRig,
    LiveFlagProbe,
    LiveMatrixRig,
    record_cli_version,
)
from tests.e2e.live_matrix import FORBIDDEN_FILE, PONG_PROMPT, lists_flag

pytestmark = pytest.mark.live

_TOOL_USE_PROMPT = (
    "Create a file named ready.txt in the current directory whose only "
    "content is the word: ok\nThen reply with just: DONE"
)


async def _run(
    controller: AgentController,
    prompt: str,
    *,
    session_id: str | None = None,
    is_new: bool = True,
    system_prompt: str | None = None,
) -> list[BridgeEvent]:
    return [
        e
        async for e in controller.run(
            session_id or str(uuid.uuid4()),
            prompt,
            is_new=is_new,
            system_prompt=system_prompt,
        )
    ]


def _streamed_text(events: list[BridgeEvent]) -> str:
    return " ".join(e.text for e in events if isinstance(e, TextDelta))


def _sole_completion(events: list[BridgeEvent]) -> Completion:
    """The engine guarantee, asserted on every live stream: exactly one
    ``Completion``, in the terminal position."""
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1, events
    assert isinstance(events[-1], Completion), events
    return completions[0]


def _said(events: list[BridgeEvent], completion: Completion, token: str) -> bool:
    return token in f"{_streamed_text(events)} {completion.text}".upper()


def _cli_output(cli_path: str, *args: str) -> str:
    """Run a CLI's own metadata command (``--version``, ``--help``).

    Spends no tokens: these never reach a model. stderr is folded in because
    some CLIs print help there, and a non-zero exit is left to the caller's
    assertion — an empty result is the failure, not the exit code.
    """
    proc = subprocess.run(
        [cli_path, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return f"{proc.stdout}\n{proc.stderr}"


# --- tier 0: the CLI itself, no tokens ---


@pytest.mark.live_tier(0)
def test_live_cli_version_is_reported(live_flag_probe: LiveFlagProbe):
    """Record which CLI build the live run talked to.

    Nothing here can fail except the CLI being unusable — the point is
    attribution: the terminal summary prints these, so a live failure can be
    blamed on (or cleared of) a version bump.
    """
    version = _cli_output(live_flag_probe.cli_path, "--version").strip()

    assert version, f"{live_flag_probe.cli_path} --version printed nothing"
    record_cli_version(live_flag_probe.name, version.splitlines()[0])


@pytest.mark.live_tier(0)
def test_live_cli_help_still_lists_every_flag_we_build(live_flag_probe: LiveFlagProbe):
    """Every flag ``build_command`` emits is still an option of the CLI.

    Free drift detection: a renamed or removed flag fails here in a second,
    instead of surfacing as a mystery error ``Completion`` in a paid scenario
    — or, worse, in production.
    """
    for check in live_flag_probe.checks:
        assert check.flags, f"{live_flag_probe.name} ({check.source}) built no flags"
        help_text = _cli_output(live_flag_probe.cli_path, *check.help_args)
        assert help_text.strip(), f"`{' '.join(check.help_args)}` printed nothing"

        missing = [flag for flag in check.flags if not lists_flag(help_text, flag)]
        assert not missing, (
            f"{live_flag_probe.name} ({check.source}): "
            f"{missing} not listed by `{live_flag_probe.cli_path} "
            f"{' '.join(check.help_args)}` — the CLI renamed or dropped them"
        )


# --- tiers 1 and 2: one config knob per matrix row ---


async def test_live_config_axis(live_matrix_rig: LiveMatrixRig):
    """One row of ``LIVE_MATRIX``: flip a knob, run one turn, assert what the
    row says the real CLI enforces.

    Deliberately unmarked — each parametrized row carries its own
    ``live_tier`` mark, so a function-level one could only get in the way.
    """
    case = live_matrix_rig.case
    events = await _run(live_matrix_rig.controller, case.prompt, is_new=case.is_new)
    completion = _sole_completion(events)

    if case.tolerate_error and completion.is_error:
        # Reachability of a model/variant is account-local; the argv is what
        # this row is about, and a rejection here proves nothing either way.
        pytest.skip(f"{case.name}: CLI rejected it — {completion.text[:200]}")
    assert not completion.is_error, completion.text

    if case.expect == "pong":
        assert _said(events, completion, "PONG"), events
        return

    forbidden = live_matrix_rig.work_dir / FORBIDDEN_FILE
    assert not forbidden.exists(), (
        f"{case.name}: the knob did not restrict the agent — {FORBIDDEN_FILE} "
        "was written"
    )
    if case.strict_empty:
        # Nothing else either: these CLIs leave no state of their own in the
        # work dir, so anything present would be the agent's doing.
        assert list(live_matrix_rig.work_dir.iterdir()) == []


# --- tier 2: the shared behaviour scenarios, once per agent ---


@pytest.mark.live_tier(2)
async def test_live_run_streams_exactly_one_completion_with_usage(
    live_controller_rig: LiveControllerRig,
):
    """The stream contract, checked against the CLI that actually emits it."""
    events = await _run(live_controller_rig.controller, PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert _said(events, completion, "PONG"), events

    # The real stream carried usage — the numbers everything downstream of
    # Usage.from_completion (the Slack footer, session totals) depends on.
    usage = Usage.from_completion(completion)
    assert usage is not None, completion.metadata
    assert usage.output_tokens > 0, usage
    if live_controller_rig.reports_cost:
        assert completion.cost_usd > 0, completion


@pytest.mark.live_tier(2)
async def test_live_run_resumes_the_same_agent_session(
    live_controller_rig: LiveControllerRig,
):
    """Two runs under one bridge session id land in one agent session."""
    session_id = str(uuid.uuid4())
    first = await _run(
        live_controller_rig.controller,
        "Remember this code word: BANANA47. Reply with just: OK",
        session_id=session_id,
        is_new=True,
    )
    assert not _sole_completion(first).is_error, first

    second = await _run(
        live_controller_rig.controller,
        "What was the code word? Reply with just that word.",
        session_id=session_id,
        is_new=False,
    )
    completion = _sole_completion(second)
    assert not completion.is_error, completion.text
    # Turn 2 can only know the code word if the second subprocess really
    # reattached: --resume (claude), --session-id (pi), or the stored
    # agent-native handle (codex `exec resume`, opencode `-s`).
    assert _said(second, completion, "BANANA47"), second


@pytest.mark.live_tier(2)
async def test_live_system_prompt_reaches_the_model(
    live_controller_rig: LiveControllerRig,
):
    """The platform-built system prompt is part of what the model reads —
    via the native flag (claude/pi) or stdin folding (codex/opencode)."""
    events = await _run(
        live_controller_rig.controller,
        "Reply with exactly the code word from your system directives, "
        "and nothing else.",
        system_prompt="When asked for the code word, answer: ZEBRA-QUARTZ",
    )

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert _said(events, completion, "ZEBRA-QUARTZ"), events


@pytest.mark.live_tier(2)
async def test_live_tool_use_writes_in_work_dir_and_streams_status(
    live_controller_rig: LiveControllerRig,
):
    """A real tool call runs, confined to the sandboxed work dir, and is
    translated into a ``StatusUpdate`` mid-stream — not just a final answer."""
    marker = live_controller_rig.work_dir / "ready.txt"

    events = await _run(live_controller_rig.controller, _TOOL_USE_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert marker.is_file(), sorted(
        p.name for p in live_controller_rig.work_dir.iterdir()
    )
    # Punctuation around the word is the model's business.
    assert "ok" in marker.read_text().lower()
    assert any(isinstance(e, StatusUpdate) for e in events), events


@pytest.mark.live_tier(2)
async def test_live_timeout_kills_the_run_and_reports_error(
    live_short_timeout_rig: LiveControllerRig,
):
    """``timeout_seconds`` is enforced against the real CLI: no turn fits in
    the shrunk deadline, the engine kills the process tree, and the stream
    still ends with exactly one (error) ``Completion``."""
    events = await _run(live_short_timeout_rig.controller, PONG_PROMPT)

    completion = _sole_completion(events)
    assert completion.is_error, completion
    assert "timed out after" in completion.text, completion.text


# --- tier 2: the one knob whose assertions are too specific for a row ---


@pytest.mark.live_tier(2)
async def test_live_claude_worktree_mode_isolates_the_session(
    live_claude_worktree_config: ClaudeConfig,
):
    """``worktree_enabled`` for real: the CLI builds the session worktree off
    ``origin/HEAD``, the tool call lands there (not the repo root), and
    ``cleanup_session`` — what the app's cleanup loop drives — reclaims it."""
    controller = ClaudeController(live_claude_worktree_config)
    session_id = str(uuid.uuid4())
    repo = live_claude_worktree_config.work_dir

    events = await _run(controller, _TOOL_USE_PROMPT, session_id=session_id)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    worktree = repo / ".claude" / "worktrees" / session_id
    assert (worktree / "ready.txt").is_file(), sorted(
        p.name for p in repo.rglob("ready.txt")
    )
    assert not (repo / "ready.txt").exists()

    await controller.cleanup_session(session_id)
    assert not worktree.exists()

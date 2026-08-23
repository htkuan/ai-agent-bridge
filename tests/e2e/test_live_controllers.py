"""E2E: every real agent CLI behind its bare ``CliAgentController`` — opt-in.

    uv run pytest -m live --live --no-cov -v

Skipped without ``--live``; a missing CLI skips just that agent's scenarios
(``--live-cli`` / ``--live-pi-cli`` / ``--live-codex-cli`` /
``--live-opencode-cli`` — flags in ``tests/conftest.py``, rig in
``conftest.py`` here).

Where ``test_live_claude.py`` and ``test_live_webhook.py`` prove whole
platform paths, these scenarios isolate the controller seam itself: prompt
in → ``BridgeEvent``s out, once per agent, no bridge or platform in between.
Each pins something the scripted CLIs replay by construction instead of
implementing: the stream shape each parser reads (with real usage numbers),
the exactly-one-``Completion`` engine guarantee, session resume (claude
``--resume``, pi ``--session-id`` reattach, codex/opencode through the
``SessionHandleStore`` round-trip), system-prompt delivery (native flag for
claude/pi, stdin folding for codex/opencode), and real tool use surfacing as
``StatusUpdate``s.

The config-axis scenarios then flip one knob each and assert the behaviour
the real CLI enforces: the engine's ``timeout_seconds`` deadline, claude's
``permission_mode``/``model``/``worktree_enabled``, pi's ``exclude_tools``
and ``thinking``, codex's ``sandbox_mode``/``effort`` and its git-repo probe.
Knobs that pin a *specific* model, provider or variant are deliberately not
tested live — they depend on what the local account can reach.

Prompts are written to force a token we can assert on. Everything else about
the reply is the model's business — never assert on its prose.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    StatusUpdate,
    TextDelta,
    Usage,
)
from agent_bridge.bridge.protocols import AgentController
from tests.e2e.conftest import LiveControllerRig

pytestmark = pytest.mark.live

_PONG_PROMPT = "Reply with exactly the word PONG and nothing else."
_BLOCKED_WRITE_PROMPT = (
    "Create a file named forbidden.txt in the current directory with "
    "content: x. If you cannot, reply with just: BLOCKED"
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


async def test_live_run_streams_exactly_one_completion_with_usage(
    live_controller_rig: LiveControllerRig,
):
    """The stream contract, checked against the CLI that actually emits it."""
    events = await _run(live_controller_rig.controller, _PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper(), events

    # The real stream carried usage — the numbers everything downstream of
    # Usage.from_completion (the Slack footer, session totals) depends on.
    usage = Usage.from_completion(completion)
    assert usage is not None, completion.metadata
    assert usage.output_tokens > 0, usage
    if live_controller_rig.reports_cost:
        assert completion.cost_usd > 0, completion


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
    assert "BANANA47" in f"{_streamed_text(second)} {completion.text}".upper(), second


async def test_live_system_prompt_reaches_the_model(
    live_controller_rig: LiveControllerRig,
):
    """The platform-built system prompt is part of what the model reads —
    via the native flag (claude, pi) or stdin folding (codex, opencode)."""
    events = await _run(
        live_controller_rig.controller,
        "Reply with exactly the code word from your system directives, "
        "and nothing else.",
        system_prompt="When asked for the code word, answer: ZEBRA-QUARTZ",
    )

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "ZEBRA-QUARTZ" in (f"{_streamed_text(events)} {completion.text}".upper()), (
        events
    )


async def test_live_tool_use_writes_in_work_dir_and_streams_status(
    live_controller_rig: LiveControllerRig,
):
    """A real tool call runs, confined to the sandboxed work dir, and is
    translated into a ``StatusUpdate`` mid-stream — not just a final answer."""
    marker = live_controller_rig.work_dir / "ready.txt"

    events = await _run(
        live_controller_rig.controller,
        "Create a file named ready.txt in the current directory whose only "
        "content is the word: ok\nThen reply with just: DONE",
    )

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert marker.is_file(), sorted(
        p.name for p in live_controller_rig.work_dir.iterdir()
    )
    # Punctuation around the word is the model's business.
    assert "ok" in marker.read_text().lower()
    assert any(isinstance(e, StatusUpdate) for e in events), events


# --- config axes: one knob flipped per scenario, CLI-enforced behaviour ---


async def test_live_timeout_kills_the_run_and_reports_error(
    live_short_timeout_rig: LiveControllerRig,
):
    """``timeout_seconds`` is enforced against the real CLI: no turn fits in
    the shrunk deadline, the engine kills the process tree, and the stream
    still ends with exactly one (error) ``Completion``."""
    events = await _run(live_short_timeout_rig.controller, _PONG_PROMPT)

    completion = _sole_completion(events)
    assert completion.is_error, completion
    assert "timed out after" in completion.text, completion.text


async def test_live_claude_default_permission_mode_blocks_writes(
    live_claude_config: ClaudeConfig,
):
    """``permission_mode="default"`` in print mode has no one to ask, so the
    write is denied by the CLI's permission system — not model restraint."""
    controller = ClaudeController(
        replace(live_claude_config, permission_mode="default")
    )
    events = await _run(controller, _BLOCKED_WRITE_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert not (live_claude_config.work_dir / "forbidden.txt").exists()


async def test_live_claude_model_alias_is_accepted(live_claude_config: ClaudeConfig):
    """``--model`` carries a value the real CLI resolves. `haiku` is a stable
    CLI alias; an unknown model errors the run before any result."""
    controller = ClaudeController(replace(live_claude_config, model="haiku"))
    events = await _run(controller, _PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper(), events


async def test_live_claude_worktree_mode_isolates_the_session(
    live_claude_worktree_config: ClaudeConfig,
):
    """``worktree_enabled`` for real: the CLI builds the session worktree off
    ``origin/HEAD``, the tool call lands there (not the repo root), and
    ``cleanup_session`` — what the app's cleanup loop drives — reclaims it."""
    controller = ClaudeController(live_claude_worktree_config)
    session_id = str(uuid.uuid4())
    repo = live_claude_worktree_config.work_dir

    events = await _run(
        controller,
        "Create a file named ready.txt in the current directory whose only "
        "content is the word: ok\nThen reply with just: DONE",
        session_id=session_id,
    )

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    worktree = repo / ".claude" / "worktrees" / session_id
    assert (worktree / "ready.txt").is_file(), sorted(
        p.name for p in repo.rglob("ready.txt")
    )
    assert not (repo / "ready.txt").exists()

    await controller.cleanup_session(session_id)
    assert not worktree.exists()


async def test_live_pi_exclude_tools_blocks_writes(live_pi_config: PiConfig):
    """``--exclude-tools`` really restricts pi — the allowlist's other half
    (the allowlist itself is pinned live through the webhook rig)."""
    controller = PiController(
        replace(live_pi_config, exclude_tools=("write", "edit", "bash"))
    )
    events = await _run(controller, _BLOCKED_WRITE_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert list(live_pi_config.work_dir.iterdir()) == []


async def test_live_pi_thinking_flag_is_accepted(live_pi_config: PiConfig):
    """``--thinking`` carries a level the real CLI accepts — the value set our
    config validates must stay a subset of what pi itself takes."""
    controller = PiController(replace(live_pi_config, thinking="low"))
    events = await _run(controller, _PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper(), events


async def test_live_codex_readonly_sandbox_blocks_writes(
    live_codex_config: CodexConfig,
):
    """sandbox_mode="read-only" really restricts codex: the file's absence is
    CLI-enforced, not model behaviour."""
    config = replace(live_codex_config, sandbox_mode="read-only")
    events = await _run(CodexController(config), _BLOCKED_WRITE_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert list(config.work_dir.iterdir()) == []


async def test_live_codex_effort_override_is_accepted(live_codex_config: CodexConfig):
    """``-c model_reasoning_effort="…"`` is a spelling the real CLI accepts —
    the same class of drift as the resume flag this suite already caught."""
    controller = CodexController(replace(live_codex_config, effort="low"))
    events = await _run(controller, _PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper(), events


async def test_live_codex_git_work_dir_needs_no_skip_flag(
    live_codex_config: CodexConfig, tmp_path: Path
):
    """The production default — a git work dir, no skip flag — passes codex's
    trusted-directory probe, which is the premise ``check_prerequisites``
    (and our docs) rely on."""
    repo = tmp_path / "codex-git-workspace"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    config = replace(live_codex_config, work_dir=repo, skip_git_repo_check=False)
    events = await _run(CodexController(config), _PONG_PROMPT)

    completion = _sole_completion(events)
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper(), events

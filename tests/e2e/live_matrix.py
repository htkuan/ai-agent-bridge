"""The live conformance matrix: what every agent must prove against its real CLI.

The scripted CLIs in ``tests/fakes/`` replay the stream shapes *we wrote
down*, so they can never notice a CLI that renamed a flag, dropped a
subcommand or changed its event names. Only the real binary can, and the
cheapest place to ask it is the controller on its own — config in,
``BridgeEvent``s out, no bridge and no platform to confuse the attribution.

This module is the declarative half of that layer (the runners live in
``test_live_controllers.py``): adding an agent means adding rows here, not
writing new test functions. Rows carry a **tier**, which is what
``--live-tier`` selects:

- **tier 0** — no tokens: the CLI exists, its version is recorded, and its own
  help still lists every flag ``build_command`` emits.
- **tier 1** — one turn: one config knob flipped, and the real CLI *accepts*
  the argv we build for it.
- **tier 2** — one or two turns: behaviour. The knob restricts what it claims
  to, the stream parses, resume reattaches, tool use surfaces.
- **tier 3** — several turns: prompt-shape robustness. Reserved; opt in with
  ``--live-tier=3``.

Tier 0 is the one to run after upgrading an agent CLI: it spends nothing and
catches the drift class that has actually bitten us (codex dropping
``--skip-git-repo-check`` from the resume branch).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

# The file the blocked-write prompt tries to create; the restriction cases
# assert on this exact name rather than "the work dir stayed empty", because
# some CLIs keep their own state next to the agent's output.
FORBIDDEN_FILE = "forbidden.txt"

PONG_PROMPT = "Reply with exactly the word PONG and nothing else."
BLOCKED_WRITE_PROMPT = (
    f"Create a file named {FORBIDDEN_FILE} in the current directory with "
    "content: x. If you cannot, reply with just: BLOCKED"
)

# A config mutation. Untyped in the config it takes: one matrix holds rows for
# four unrelated config classes, and each row is only ever applied to its own.
type Mutate = Callable[[Any], Any]


def _identity(config: Any) -> Any:
    return config


# --- tier 0: the flags we build must still exist in the CLI's own help ------


@dataclass(frozen=True)
class HelpPage:
    """One help invocation, and the ``build_command`` branch it must cover.

    ``args`` is appended to ``cli_path`` (e.g. ``("exec", "resume", "--help")``).
    ``is_new`` picks the branch whose argv is derived from it, so a resume
    branch that grew a flag the resume help page doesn't list fails here —
    for free, before any token is spent.
    """

    args: tuple[str, ...]
    is_new: bool


@dataclass(frozen=True)
class FlagSpec:
    """Where an agent's flags come from and which help pages must list them.

    ``configs`` are mutations that switch every optional flag on, so the
    derived argv is the full one rather than the default subset — mutually
    exclusive branches (claude's two permission spellings) get one entry each.
    ``seeded_handle`` is written into the agent's session-handle map before
    the ``is_new=False`` argv is built, for the CLIs whose resume branch needs
    a stored handle to trigger at all.
    """

    pages: tuple[HelpPage, ...]
    configs: tuple[Mutate, ...]
    seeded_handle: str | None = None


_BOTH_BRANCHES = (
    HelpPage(("--help",), is_new=True),
    HelpPage(("--help",), is_new=False),
)

FLAG_SPECS: dict[str, FlagSpec] = {
    "claude": FlagSpec(
        pages=_BOTH_BRANCHES,
        configs=(
            lambda c: replace(c, model="probe-model", worktree_enabled=True),
            # The other permission spelling: a flag, not a --permission-mode value.
            lambda c: replace(c, permission_mode="dangerously-skip-permissions"),
        ),
    ),
    "pi": FlagSpec(
        pages=_BOTH_BRANCHES,
        configs=(
            lambda c: replace(
                c,
                provider="probe-provider",
                model="probe-model",
                thinking="low",
                tools=("read",),
                exclude_tools=("bash",),
            ),
        ),
    ),
    "codex": FlagSpec(
        pages=(
            HelpPage(("exec", "--help"), is_new=True),
            HelpPage(("exec", "resume", "--help"), is_new=False),
        ),
        configs=(
            lambda c: replace(
                c, model="probe-model", effort="low", skip_git_repo_check=True
            ),
        ),
        seeded_handle="probe-thread-id",
    ),
    "opencode": FlagSpec(
        pages=(
            HelpPage(("run", "--help"), is_new=True),
            HelpPage(("run", "--help"), is_new=False),
        ),
        configs=(lambda c: replace(c, model="probe-model", variant="high"),),
        seeded_handle="ses_probe",
    ),
}


def build_flags(argv: Sequence[str]) -> tuple[str, ...]:
    """The flags in one built command, in order, without their values.

    Values never start with ``-`` in anything we build (that is the whole
    point of keeping the prompt out of argv), and codex's trailing ``-``
    marker is a stdin sentinel, not a flag.
    """
    return tuple(
        token
        for token in argv[1:]
        if token.startswith("-") and token != "-" and not token.startswith("---")
    )


def lists_flag(help_text: str, flag: str) -> bool:
    """Whether a help page documents ``flag`` as an option of its own.

    Bounded on both sides, so ``-p`` doesn't match inside ``--print`` and
    ``--model`` doesn't match inside ``--models`` — a substring check would
    make tier 0 pass on flags the CLI no longer has.
    """
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


# --- tiers 1 and 2: one config knob per row --------------------------------


@dataclass(frozen=True)
class LiveCase:
    """One config axis, run against the real CLI.

    ``expect`` says what the turn must show: ``"pong"`` (the CLI accepted the
    argv and the reply came back) or ``"no_write"`` (the knob restricted the
    agent — the file it was told to create is absent). ``strict_empty`` adds
    "and nothing else appeared either", for the agents whose CLI leaves no
    state of its own in the work dir.

    ``base`` names an alternative config fixture when ``replace()`` cannot
    build the world the case needs (a git work dir, a repo with an origin).
    ``tolerate_error`` turns a CLI-side rejection into a skip instead of a
    failure: whether *this account* can reach a given model or variant is not
    something the suite should have an opinion about.

    ``is_new=False`` runs the resume branch — with no stored handle, that is
    the graceful-degrade path our docs promise. ``model_from_option`` makes
    the row opt-in on ``--live-model AGENT=MODEL`` (see ``model_override``).
    """

    name: str
    tier: int
    mutate: Mutate = _identity
    prompt: str = PONG_PROMPT
    expect: Literal["pong", "no_write"] = "pong"
    strict_empty: bool = False
    base: str | None = None
    tolerate_error: bool = False
    is_new: bool = True
    model_from_option: bool = False


def _model_case() -> LiveCase:
    """Every agent's model axis: same row, opt-in per agent on the flag."""
    return LiveCase("model_override_accepted", tier=1, model_from_option=True)


def _lost_handle_case() -> LiveCase:
    """For the agents that map bridge id → agent-native handle: a resume with
    nothing stored must degrade to a fresh session, not fail the turn."""
    return LiveCase("resume_without_stored_handle", tier=2, is_new=False)


LIVE_MATRIX: dict[str, tuple[LiveCase, ...]] = {
    "claude": (
        LiveCase(
            # print mode has no one to ask, so the write is denied by the CLI's
            # permission system — not by model restraint.
            "permission_mode_default_blocks_writes",
            tier=2,
            mutate=lambda c: replace(c, permission_mode="default"),
            prompt=BLOCKED_WRITE_PROMPT,
            expect="no_write",
        ),
        LiveCase(
            # `haiku` is a stable CLI alias, so this is an argv check, not a
            # bet on what the account can reach.
            "model_alias_accepted",
            tier=1,
            mutate=lambda c: replace(c, model="haiku"),
        ),
        LiveCase(
            # The --dangerously-skip-permissions branch of build_command: a
            # different argv shape, never exercised live before.
            "skip_permissions_argv_accepted",
            tier=1,
            mutate=lambda c: replace(c, permission_mode="dangerously-skip-permissions"),
        ),
        _model_case(),
    ),
    "pi": (
        LiveCase(
            # The allowlist's other half; the allowlist itself is pinned
            # through the webhook rig (tests/e2e/test_live_webhook.py).
            "exclude_tools_blocks_writes",
            tier=2,
            mutate=lambda c: replace(c, exclude_tools=("write", "edit", "bash")),
            prompt=BLOCKED_WRITE_PROMPT,
            expect="no_write",
            strict_empty=True,
        ),
        LiveCase(
            # The levels our config validates must stay a subset of pi's own.
            "thinking_level_accepted",
            tier=1,
            mutate=lambda c: replace(c, thinking="low"),
        ),
        _model_case(),
    ),
    "codex": (
        LiveCase(
            "readonly_sandbox_blocks_writes",
            tier=2,
            mutate=lambda c: replace(c, sandbox_mode="read-only"),
            prompt=BLOCKED_WRITE_PROMPT,
            expect="no_write",
            strict_empty=True,
        ),
        LiveCase(
            # `-c model_reasoning_effort="…"` is the spelling most likely to
            # drift — the same class as the resume flag this suite caught.
            "effort_override_accepted",
            tier=1,
            mutate=lambda c: replace(c, effort="low"),
        ),
        LiveCase(
            # The production default — git work dir, no skip flag — must pass
            # codex's trusted-directory probe, which check_prerequisites and
            # our docs both rely on.
            "git_work_dir_needs_no_skip_flag",
            tier=1,
            base="live_codex_git_config",
        ),
        _lost_handle_case(),
        _model_case(),
    ),
    "opencode": (
        LiveCase(
            # Provider-specific, so a rejection is reachability, not argv.
            "variant_accepted",
            tier=1,
            mutate=lambda c: replace(c, variant="high"),
            tolerate_error=True,
        ),
        _lost_handle_case(),
        _model_case(),
    ),
}

# Agents with no restriction knob to pin at tier 2. Kept explicit so a new
# agent's missing permission axis reads as a decision, not an oversight.
NO_RESTRICTION_AXIS = {
    "opencode": (
        "opencode run has no sandbox and OpencodeConfig exposes no permission "
        "knob (the CLI's own --auto is not one we pass), so there is nothing "
        "for a restriction case to flip."
    ),
}


def model_override(models: Sequence[str], agent: str) -> str | None:
    """The ``--live-model AGENT=MODEL`` value for one agent, if given.

    The model axis is opt-in because which models an account can reach is
    local: unset means the case skips, not that it passes.
    """
    for entry in models:
        name, _, model = entry.partition("=")
        if name.strip() == agent and model.strip():
            return model.strip()
    return None

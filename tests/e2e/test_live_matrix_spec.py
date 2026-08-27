"""The live matrix's own invariants — enforced in CI, no CLI and no tokens.

``tests/e2e/test_live_controllers.py`` only runs behind ``--live``, so nothing
there can stop a new agent from shipping with an empty live spec. These checks
run in the ordinary e2e job (``-m "e2e and not live"``) and fail the build
instead: every agent the live layer knows about must declare what its real CLI
has to prove, and the parts of that declaration that pair up must pair up.
"""

from __future__ import annotations

from tests.e2e.conftest import LIVE_CONTROLLER_AGENTS
from tests.e2e.live_matrix import (
    FLAG_SPECS,
    LIVE_MATRIX,
    NO_RESTRICTION_AXIS,
    LiveCase,
    build_flags,
    lists_flag,
)


def test_every_agent_has_a_flag_spec():
    """Tier 0 is the free drift check — no agent may opt out of it."""
    assert sorted(FLAG_SPECS) == sorted(LIVE_CONTROLLER_AGENTS)


def test_every_agent_has_matrix_rows():
    assert sorted(LIVE_MATRIX) == sorted(LIVE_CONTROLLER_AGENTS)
    for agent, cases in LIVE_MATRIX.items():
        assert cases, f"{agent} has no live config-axis rows"


def test_flag_specs_cover_both_command_branches():
    """``build_command`` has a new-session and a resume branch; a help page
    that only covers one leaves the other free to drift."""
    for agent, spec in FLAG_SPECS.items():
        branches = {page.is_new for page in spec.pages}
        assert branches == {True, False}, f"{agent} probes only {branches}"
        assert spec.configs, f"{agent} declares no flag-source config"


def test_handle_mapping_agents_pin_the_degrade_path():
    """An agent whose resume needs a stored handle must also prove what
    happens when the handle is gone — the documented graceful degrade."""
    for agent, spec in FLAG_SPECS.items():
        if spec.seeded_handle is None:
            continue
        resume_rows = [case for case in LIVE_MATRIX[agent] if not case.is_new]
        assert resume_rows, (
            f"{agent} maps bridge ids to agent handles but has no "
            "resume-without-stored-handle row"
        )


def test_every_agent_pins_or_explains_its_restriction_axis():
    """A permission model nobody pins live is the gap most likely to go
    unnoticed, so an absence has to be a recorded decision."""
    for agent, cases in LIVE_MATRIX.items():
        restricts = any(case.expect == "no_write" for case in cases)
        assert restricts or agent in NO_RESTRICTION_AXIS, (
            f"{agent} has no live restriction row and no NO_RESTRICTION_AXIS "
            "entry explaining why"
        )
    assert not set(NO_RESTRICTION_AXIS) - set(LIVE_MATRIX), (
        "NO_RESTRICTION_AXIS names an agent the matrix doesn't know"
    )


def test_rows_are_uniquely_named_and_tiered():
    for agent, cases in LIVE_MATRIX.items():
        names = [case.name for case in cases]
        assert len(names) == len(set(names)), f"{agent} has duplicate row names"
        for case in cases:
            assert case.tier in {0, 1, 2, 3}, case
            # Tier 0 spends nothing by never running the CLI; a row always
            # runs a turn, so it cannot be tier 0.
            assert case.tier >= 1, f"{agent}.{case.name} must be tier 1 or higher"


def test_no_write_rows_use_the_blocked_write_prompt():
    """A restriction row that asks for anything else proves nothing about the
    file it then asserts is missing."""
    for agent, cases in LIVE_MATRIX.items():
        for case in cases:
            if case.expect == "no_write":
                assert "forbidden.txt" in case.prompt, f"{agent}.{case.name}"


def test_build_flags_keeps_flags_and_drops_values():
    argv = ["codex", "exec", "--json", "--sandbox", "read-only", "-c", 'k="v"', "-"]

    # "exec" is a subcommand, "read-only"/'k="v"' are values, and the trailing
    # "-" is codex's stdin sentinel — none of them are flags to look up.
    assert build_flags(argv) == ("--json", "--sandbox", "-c")


def test_lists_flag_requires_a_whole_option_not_a_substring():
    help_text = "  -p, --print   print mode\n      --models  list models\n"

    assert lists_flag(help_text, "-p")
    assert lists_flag(help_text, "--print")
    assert lists_flag(help_text, "--models")
    # The two ways a substring match would make tier 0 pass on a flag that is
    # no longer there.
    assert not lists_flag(help_text, "--model")
    assert not lists_flag(help_text, "-r")


def test_default_case_shape_is_a_pong_turn():
    """The cheap default: a row that says nothing else is one accepted-argv
    turn, so adding a knob costs one line."""
    case = LiveCase("probe", tier=1)
    assert case.expect == "pong"
    assert case.is_new is True
    assert case.mutate("unchanged") == "unchanged"

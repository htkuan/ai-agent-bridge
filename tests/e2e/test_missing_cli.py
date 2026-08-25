"""E2E: a CLI path that isn't there still ends the turn."""

from __future__ import annotations

from pathlib import Path

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from tests.e2e.stack import SlackStack, session_manager_for, wire_slack


def _stack_without_a_cli(tmp_path: Path) -> SlackStack:
    """The whole real stack, pointed at a binary that does not exist."""
    controller = ClaudeController(
        ClaudeConfig(work_dir=tmp_path, cli_path=str(tmp_path / "nonexistent-claude"))
    )
    session_manager = session_manager_for(tmp_path)
    wiring = wire_slack(controller, session_manager)
    return SlackStack(
        adapter=wiring.adapter,
        app=wiring.app,
        client=wiring.client,
        bridge=wiring.bridge,
        session_manager=session_manager,
        controller=controller,
    )


async def test_missing_cli_reports_and_keeps_the_bridge_alive(tmp_path: Path):
    stack = _stack_without_a_cli(tmp_path)

    await stack.send("first", ts="1.0")

    # A terminal notice, not the ":hourglass_flowing_sand: Processing..."
    # placeholder the thread used to sit on forever: the spawn's
    # FileNotFoundError escaped the pipeline instead of completing the stream.
    first = stack.replies()
    assert len(first) == 1
    assert first[0].startswith(":no_entry:")

    # The session is idle again, so the thread can retry immediately.
    state = stack.adapter._get_state("slack:C123:1.0")
    assert state.processing is False
    assert state.waiting_for_answer is False

    # ...and the bridge is still serving other threads.
    await stack.send("second", ts="2.0")
    assert [m.startswith(":no_entry:") for m in stack.replies()] == [True, True]

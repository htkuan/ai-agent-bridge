"""Contract suite for the ``AgentController`` protocol.

Every implementation — the real ``ClaudeController`` (driven by the scripted
fake CLI) and ``FakeAgentController`` — must satisfy these expectations.
If the fake drifts from real behaviour, this suite is where it shows up.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.events import Completion, TextDelta
from agent_bridge.bridge.protocols import AgentController
from tests.conftest import FakeClaudeFactory, FakePiFactory
from tests.fakes import FakeAgentController, claude_cli, pi_cli

type ControllerFactory = Callable[..., AgentController]

# The "claude"/"pi" parametrizations drive a real CLI subprocess.
pytestmark = pytest.mark.integration


@pytest.fixture(params=["claude", "pi", "fake"])
def make_controller(
    request: pytest.FixtureRequest,
    fake_claude: FakeClaudeFactory,
    fake_pi: FakePiFactory,
) -> ControllerFactory:
    def make(*, text: str = "hello", error: bool = False) -> AgentController:
        if request.param == "claude":
            steps = (
                [claude_cli.result("boom", is_error=True)]
                if error
                else claude_cli.reply_steps(text)
            )
            return ClaudeController(fake_claude(steps).config)
        if request.param == "pi":
            # Pi has no error payload in-stream; failures exit without the
            # terminal agent_end and the engine reports them.
            steps = (
                [pi_cli.stderr_line("boom"), pi_cli.exit_code(1)]
                if error
                else pi_cli.reply_steps(text)
            )
            return PiController(fake_pi(steps).config)
        if error:
            return FakeAgentController([[Completion(text="boom", is_error=True)]])
        return FakeAgentController(
            [[TextDelta(text=text), Completion(text=text, is_error=False)]]
        )

    return make


async def test_stream_ends_with_exactly_one_completion(
    make_controller: ControllerFactory,
) -> None:
    controller = make_controller(text="hi")
    events = [e async for e in controller.run("s1", "prompt", is_new=True)]
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert isinstance(events[-1], Completion)
    assert completions[0].is_error is False


async def test_reply_text_reaches_the_stream(
    make_controller: ControllerFactory,
) -> None:
    controller = make_controller(text="the reply")
    events = [e async for e in controller.run("s1", "prompt", is_new=True)]
    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "the reply" in deltas


async def test_failure_yields_error_completion_instead_of_raising(
    make_controller: ControllerFactory,
) -> None:
    controller = make_controller(error=True)
    events = [e async for e in controller.run("s1", "prompt", is_new=True)]
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is True


async def test_cleanup_session_on_unknown_session_is_a_quiet_noop(
    make_controller: ControllerFactory,
) -> None:
    # The app's cleanup loop calls every controller for every purged id.
    controller = make_controller()
    await controller.cleanup_session("never-seen-session")

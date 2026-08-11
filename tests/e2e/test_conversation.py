"""E2E: multi-turn thread conversations through the real stack."""

from __future__ import annotations

from pathlib import Path

from tests.e2e.stack import build_stack
from tests.fakes import claude_cli


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


async def test_thread_resume_across_turns(tmp_path: Path):
    stack = build_stack(tmp_path, claude_cli.reply_steps("first reply"))

    await stack.send("hello", ts="1.0")
    stack.swap_scenario(claude_cli.reply_steps("second reply"))
    await stack.send("and again", ts="2.0", thread_ts="1.0")

    # The user sees one reply message per turn, in order.
    assert list(stack.client.messages.values()) == ["first reply", "second reply"]

    # Turn 1 started the session; turn 2 resumed the very same one.
    argv1, argv2 = stack.cli.invocations()
    session_id = _flag_value(argv1, "--session-id")
    assert _flag_value(argv1, "-p") == "[alice (U123)]: hello"
    assert _flag_value(argv2, "--resume") == session_id
    assert "--session-id" not in argv2

    # One persisted session, keyed by the thread.
    sessions = stack.session_manager.list_sessions()
    assert set(sessions) == {"slack:C123:1.0"}
    assert sessions["slack:C123:1.0"]["session_id"] == session_id


async def test_ask_user_question_waits_then_resumes(tmp_path: Path):
    questions = [
        {"question": "Which env?", "options": [{"label": "dev"}, {"label": "prod"}]}
    ]
    stack = build_stack(
        tmp_path, [claude_cli.ask_user_question(questions), claude_cli.result("")]
    )

    await stack.send("deploy", ts="1.0")

    # The question is rendered to the thread and the session parks itself.
    posted = list(stack.client.messages.values())
    assert len(posted) == 1
    assert "Claude needs your input" in posted[0]
    assert "Which env?" in posted[0]
    assert "`dev`" in posted[0]
    state = stack.adapter._get_state("slack:C123:1.0")
    assert state.waiting_for_answer is True

    # The user's next message in the thread answers the question.
    stack.swap_scenario(claude_cli.reply_steps("deploying to dev"))
    await stack.send("dev", ts="2.0", thread_ts="1.0")

    assert "deploying to dev" in stack.client.messages.values()
    assert state.waiting_for_answer is False
    argv1, argv2 = stack.cli.invocations()
    assert _flag_value(argv2, "--resume") == _flag_value(argv1, "--session-id")
    assert _flag_value(argv2, "-p") == "[alice (U123)]: dev"

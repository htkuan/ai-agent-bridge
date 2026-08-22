"""E2E against the *real* claude CLI — opt-in, spends real tokens.

    uv run pytest -m live --live --no-cov -v

Skipped without ``--live`` (see ``tests/conftest.py`` for the flags and
``conftest.py`` here for the rig). Each scenario pins one thing the scripted
CLI cannot prove, because the scripted CLI replays the contract instead of
implementing it: the stream-json shape, session resume, and real tool use
surfacing in Slack.

Prompts are written to force a token we can assert on. Everything else about
the reply is the model's business — never assert on its prose.
"""

from __future__ import annotations

import uuid

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta, Usage
from tests.e2e.stack import SlackStack

pytestmark = pytest.mark.live


def _streamed_text(events: list[BridgeEvent]) -> str:
    return " ".join(e.text for e in events if isinstance(e, TextDelta))


async def test_live_controller_streams_a_real_completion(
    live_claude_controller: ClaudeController,
):
    """The stream-json contract, checked against the CLI that emits it."""
    events = [
        e
        async for e in live_claude_controller.run(
            str(uuid.uuid4()),
            "Reply with exactly the word PONG and nothing else.",
            is_new=True,
        )
    ]

    completion = events[-1]
    assert isinstance(completion, Completion), events
    assert not completion.is_error, completion.text
    assert "PONG" in f"{_streamed_text(events)} {completion.text}".upper()

    # The result line carried real usage — the fields the Slack usage footer
    # and everything else downstream of Usage.from_completion depend on.
    usage = Usage.from_completion(completion)
    assert usage is not None, completion.metadata
    assert usage.input_tokens > 0 and usage.output_tokens > 0, usage
    assert usage.num_turns >= 1, usage
    assert completion.duration_ms > 0


async def test_live_thread_resumes_the_same_claude_session(live_stack: SlackStack):
    """Two turns in one Slack thread land in one Claude session."""
    await live_stack.send(
        "Remember this code word: BANANA47. Reply with just: OK", ts="1.0"
    )
    await live_stack.send(
        "What was the code word? Reply with just that word.",
        ts="2.0",
        thread_ts="1.0",
    )

    replies = live_stack.replies()
    assert len(replies) == 2, replies
    # Turn 2 can only know the code word if --resume really reattached to the
    # session turn 1 opened with --session-id.
    assert "BANANA47" in replies[1].upper(), replies

    assert set(live_stack.session_manager.list_sessions()) == {"slack:C123:1.0"}


async def test_live_tool_use_reaches_slack(
    live_stack: SlackStack, live_claude_config: ClaudeConfig
):
    """A real tool call runs in the sandbox and is rendered while it happens."""
    marker = live_claude_config.work_dir / "ready.txt"

    await live_stack.send(
        "Create a file named ready.txt in the current directory whose only "
        "content is: ok. Then reply with just: DONE",
        ts="1.0",
    )

    assert marker.is_file(), sorted(p.name for p in marker.parent.iterdir())
    assert marker.read_text().strip() == "ok"

    # The tool use was streamed into the thread, not just the final answer.
    updates = [
        c.kwargs.get("text", "") for c in live_stack.client.calls_to("chat_update")
    ]
    assert any("_Using " in text for text in updates), updates
    assert "DONE" in live_stack.replies()[0].upper(), live_stack.replies()

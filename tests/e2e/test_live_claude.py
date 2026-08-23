"""E2E against the *real* claude CLI through the Slack rig — opt-in.

    uv run pytest -m live --live --no-cov -v

Skipped without ``--live`` (see ``tests/conftest.py`` for the flags and
``conftest.py`` here for the rig). The bare-controller scenarios live in
``test_live_controllers.py`` (once per agent); what's pinned here is the
Slack path on top: thread → session-key resume, and real tool use rendered
into the thread while it happens.

Prompts are written to force a token we can assert on. Everything else about
the reply is the model's business — never assert on its prose.
"""

from __future__ import annotations

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from tests.e2e.stack import SlackStack

pytestmark = pytest.mark.live


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

    assert set(await live_stack.session_manager.list_sessions()) == {"slack:C123:1.0"}


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

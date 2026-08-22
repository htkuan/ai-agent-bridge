"""E2E against the real agent CLIs through the webhook platform — opt-in.

    uv run pytest -m live --live --no-cov -v

Skipped without ``--live``; a missing CLI skips just that agent's scenarios
(``--live-cli`` / ``--live-pi-cli``, see ``conftest.py`` here). The shared
scenarios run once per agent (claude and pi) through the same stack —
webhook adapter → bridge → controller — so what's pinned is the whole path a
machine caller exercises: the 202-then-callback contract carrying a real
completion, session resume across POSTs of one ``conversation_id``, sender
pre-tagging reaching the model, real tool use confined to the sandboxed work
dir, and pi's tool allowlist actually restricting the agent.

Prompts are written to force a token we can assert on. Everything else about
the reply is the model's business — never assert on its prose.
"""

from __future__ import annotations

import pytest

from tests.e2e.stack import WebhookStack

pytestmark = pytest.mark.live


async def test_live_webhook_delivers_a_real_completion(
    live_webhook_stack: WebhookStack,
):
    """The 202-then-callback contract, with a real agent behind it."""
    payload = await live_webhook_stack.send(
        "Reply with exactly the word PONG and nothing else."
    )

    assert payload["is_error"] is False, payload
    assert "PONG" in str(payload["text"]).upper(), payload
    assert payload["conversation_id"] == "conv-1"
    # Real usage flowed all the way into the payload the caller sees.
    assert payload["cost_usd"] > 0, payload
    assert payload["duration_ms"] > 0, payload

    # The turn was resumable, so the conversation is persisted under the key
    # the adapter derives from conversation_id.
    assert set(live_webhook_stack.session_manager.list_sessions()) == {
        "webhook:default:conv-1"
    }


async def test_live_webhook_conversation_resumes_the_agent_session(
    live_webhook_stack: WebhookStack,
):
    """Two POSTs with one conversation_id land in one agent session."""
    first = await live_webhook_stack.send(
        "Remember this code word: BANANA47. Reply with just: OK",
        conversation_id="memory",
    )
    assert first["is_error"] is False, first

    second = await live_webhook_stack.send(
        "What was the code word? Reply with just that word.",
        conversation_id="memory",
    )
    # Turn 2 can only know the code word if the second subprocess really
    # reattached to the session turn 1 created.
    assert "BANANA47" in str(second["text"]).upper(), second

    assert set(live_webhook_stack.session_manager.list_sessions()) == {
        "webhook:default:memory"
    }


async def test_live_webhook_sender_reaches_the_agent(
    live_webhook_stack: WebhookStack,
):
    """The `[sender]:` pre-tag is part of what the model actually reads."""
    payload = await live_webhook_stack.send(
        "Reply with exactly the sender name this message is tagged with, "
        "and nothing else.",
        sender="zebra-quartz",
    )
    assert "ZEBRA-QUARTZ" in str(payload["text"]).upper(), payload


async def test_live_webhook_tool_use_writes_in_the_work_dir(
    live_webhook_stack: WebhookStack,
):
    """A real tool call runs, confined to the sandboxed work dir."""
    marker = live_webhook_stack.work_dir / "ready.txt"

    payload = await live_webhook_stack.send(
        "Create a file named ready.txt in the current directory whose only "
        "content is the word: ok\nThen reply with just: DONE"
    )

    assert payload["is_error"] is False, payload
    assert marker.is_file(), sorted(p.name for p in marker.parent.iterdir())
    # Punctuation around the word is the model's business.
    assert "ok" in marker.read_text().lower()
    assert "DONE" in str(payload["text"]).upper(), payload


async def test_live_webhook_non_resumable_turn_leaves_no_session(
    live_webhook_stack: WebhookStack,
):
    """resumable=False mints an ephemeral session the store never sees —
    and the real CLI accepts the fresh id it is handed."""
    payload = await live_webhook_stack.send(
        "Reply with exactly the word PONG and nothing else.",
        resumable=False,
    )
    assert payload["is_error"] is False, payload
    assert "PONG" in str(payload["text"]).upper(), payload
    assert live_webhook_stack.session_manager.list_sessions() == {}


async def test_live_webhook_pi_tool_allowlist_blocks_writes(
    live_webhook_pi_readonly: WebhookStack,
):
    """--tools really restricts pi: with read-only tools the file cannot be
    created — its absence is CLI-enforced, not model behaviour."""
    payload = await live_webhook_pi_readonly.send(
        "Create a file named forbidden.txt in the current directory with "
        "content: x. If you cannot, reply with just: BLOCKED"
    )

    assert payload["is_error"] is False, payload
    assert list(live_webhook_pi_readonly.work_dir.iterdir()) == []

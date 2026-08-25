"""E2E: a hung CLI is killed at the timeout and the user is told."""

from __future__ import annotations

import time
from pathlib import Path

from tests.e2e.stack import build_stack
from tests.fakes import claude_cli


async def test_hung_cli_killed_after_timeout(tmp_path: Path):
    stack = build_stack(
        tmp_path,
        [claude_cli.assistant_text("working on it"), claude_cli.hang(30.0)],
        timeout_seconds=1.0,
    )

    start = time.monotonic()
    await stack.send("do it", ts="1.0")
    elapsed = time.monotonic() - start

    # The real timeout path ran: past the 1s budget (plus the render throttle
    # gap), nowhere near the CLI's 30s hang. Guards against a fast-path fake
    # pass where the process died early instead of being killed.
    assert 1.0 <= elapsed < 5.0

    # The user is told what actually went wrong — the timeout, not a
    # blanket "too many requests" — and keeps the partial that streamed.
    final = list(stack.client.messages.values())
    assert len(final) == 1
    assert final[0].startswith(":warning: Claude process timed out after 1.0s")
    assert "working on it" in final[0]

    # The session is idle again — the thread can retry immediately.
    state = stack.adapter._get_state("slack:C123:1.0")
    assert state.processing is False
    assert state.waiting_for_answer is False

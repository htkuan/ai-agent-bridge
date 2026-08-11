"""E2E: global capacity gating and per-thread pending drain."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.e2e.stack import build_stack, wait_until
from tests.fakes import claude_cli

CAPACITY_REJECTION = (
    ":no_entry: Too many requests being processed, please try again later."
)
WAITING_PLACEHOLDER = ":hourglass: Waiting for previous task to finish..."


async def test_second_thread_rejected_when_capacity_full(tmp_path: Path):
    stack = build_stack(
        tmp_path,
        [claude_cli.hang(1.0), *claude_cli.reply_steps("slow done")],
        max_concurrent=1,
    )

    first = asyncio.create_task(stack.send("long task", ts="1.0"))
    # The slot is held once the (hanging) CLI has been spawned.
    await wait_until(lambda: len(stack.cli.invocations()) == 1)

    await stack.send("me too", ts="2.0")  # a different thread
    await first

    texts = list(stack.client.messages.values())
    assert CAPACITY_REJECTION in texts  # the rejected thread
    assert "slow done" in texts  # the slot holder still finishes
    # The rejected message never reached the CLI.
    assert len(stack.cli.invocations()) == 1


async def test_message_parked_during_run_drains_afterwards(tmp_path: Path):
    stack = build_stack(
        tmp_path, [claude_cli.hang(1.0), *claude_cli.reply_steps("first done")]
    )

    first = asyncio.create_task(stack.send("first", ts="1.0"))
    await wait_until(lambda: len(stack.cli.invocations()) == 1)

    # Same thread while busy → parked with a placeholder, handler returns.
    await stack.send("queued", ts="2.0", thread_ts="1.0")
    parked = [t for t in stack.client.messages.values() if t == WAITING_PLACEHOLDER]
    assert len(parked) == 1

    stack.swap_scenario(claude_cli.reply_steps("queued done"))
    await first

    # Both replies are visible; the placeholder was rewritten by the drain.
    assert sorted(stack.client.messages.values()) == ["first done", "queued done"]

    # The drained turn resumed the same thread session.
    argv1, argv2 = stack.cli.invocations()
    assert "--session-id" in argv1
    assert "--resume" in argv2
    state = stack.adapter._get_state("slack:C123:1.0")
    assert state.pending is None
    assert state.processing is False

"""E2E: cross-thread prompt dedupe through the real bridge."""

from __future__ import annotations

from pathlib import Path

from agent_bridge.bridge.config import DedupeConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from tests.e2e.stack import build_stack
from tests.fakes import claude_cli


async def test_duplicate_prompt_across_threads_collapses(tmp_path: Path):
    dedupe = PromptDedupeCache(
        DedupeConfig(ttl_seconds=60.0, max_entries=16, simhash_threshold=0)
    )
    stack = build_stack(tmp_path, claude_cli.reply_steps("done"), dedupe=dedupe)

    await stack.send("deploy the report", ts="1.0")
    await stack.send("deploy the report", ts="2.0")  # new thread, same prompt

    texts = list(stack.client.messages.values())
    assert texts[0] == "done"
    assert texts[1] == ":repeat: Duplicate detected — skipping."
    # The duplicate never reached the CLI.
    assert len(stack.cli.invocations()) == 1


async def test_different_prompts_are_not_collapsed(tmp_path: Path):
    dedupe = PromptDedupeCache(
        DedupeConfig(ttl_seconds=60.0, max_entries=16, simhash_threshold=0)
    )
    stack = build_stack(tmp_path, claude_cli.reply_steps("done"), dedupe=dedupe)

    await stack.send("deploy the report", ts="1.0")
    await stack.send("restart the worker", ts="2.0")

    assert list(stack.client.messages.values()) == ["done", "done"]
    assert len(stack.cli.invocations()) == 2

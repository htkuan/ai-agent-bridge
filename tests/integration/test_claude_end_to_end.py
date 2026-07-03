"""Fake claude CLI → real ClaudeController → real Bridge/SessionManager."""

from __future__ import annotations

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge import Bridge
from agent_bridge.config_loader import ConfigSource
from agent_bridge.events import Completion, Processing, TextDelta
from agent_bridge.session import SessionManager
from tests.helpers import (
    claude_assistant_line,
    claude_result_line,
    collect_events,
    event_types,
    install_fake_cli,
)

pytestmark = pytest.mark.integration

USAGE = {
    "input_tokens": 100,
    "output_tokens": 200,
    "cache_read_input_tokens": 300,
    "cache_creation_input_tokens": 50,
}


@pytest.fixture
def bridge_setup(tmp_path, prepend_path, clean_agent_bridge_env):
    args_log = tmp_path / "cli-args.log"
    install_fake_cli(
        tmp_path / "bin",
        lines=[
            claude_assistant_line("working on it"),
            claude_result_line("all done", cost_usd=0.05, usage=USAGE),
        ],
        args_log=args_log,
    )
    prepend_path(tmp_path / "bin")

    config = ClaudeConfig.from_source(
        ConfigSource({"agents": {"claude": {"work_dir": str(tmp_path)}}})
    )
    session_manager = SessionManager(tmp_path / "sessions.json")
    bridge = Bridge(session_manager, ClaudeController(config), max_concurrent=2)
    return bridge, session_manager, args_log


async def test_first_turn_streams_full_event_sequence(bridge_setup):
    bridge, session_manager, args_log = bridge_setup

    events = await collect_events(
        bridge.handle_message("slack:C1:t1", "[alice]: hi", system_prompt="be brief")
    )

    assert event_types(events) == [Processing, TextDelta, Completion]
    assert events[1].text == "working on it"
    completion = events[2]
    assert completion.is_error is False
    assert completion.text == "all done"
    assert completion.usage is not None
    assert completion.usage.input_tokens == 100
    assert completion.usage.cache_read_tokens == 300
    assert completion.usage.cost_usd == 0.05
    # First tracked turn → session usage equals turn usage
    assert completion.session_usage is not None
    assert completion.session_usage.output_tokens == 200

    # The session key is now persisted and the CLI got a new-session invocation
    session_id = session_manager.get("slack:C1:t1")
    assert session_id is not None
    invocation = args_log.read_text().splitlines()[0]
    assert f"--session-id {session_id}" in invocation
    assert "[alice]: hi" in invocation
    assert "--append-system-prompt be brief" in invocation


async def test_second_turn_resumes_same_session(bridge_setup):
    bridge, session_manager, args_log = bridge_setup

    await collect_events(bridge.handle_message("slack:C1:t1", "first"))
    events = await collect_events(bridge.handle_message("slack:C1:t1", "second"))

    assert event_types(events) == [Processing, TextDelta, Completion]

    session_id = session_manager.get("slack:C1:t1")
    invocations = args_log.read_text().splitlines()
    assert len(invocations) == 2
    assert f"--session-id {session_id}" in invocations[0]
    # Resume: same session id, --resume instead of --session-id
    assert f"--resume {session_id}" in invocations[1]
    assert "--session-id" not in invocations[1]


async def test_cli_error_result_surfaces_as_error_completion(
    tmp_path, prepend_path, clean_agent_bridge_env
):
    install_fake_cli(
        tmp_path / "bin",
        lines=[claude_result_line("boom", is_error=True)],
    )
    prepend_path(tmp_path / "bin")
    config = ClaudeConfig.from_source(
        ConfigSource({"agents": {"claude": {"work_dir": str(tmp_path)}}})
    )
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        ClaudeController(config),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "boom"

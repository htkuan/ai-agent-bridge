"""Fake codex CLI → real CodexController → real Bridge/SessionManager."""

from __future__ import annotations

import json

import pytest

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.bridge import Bridge
from agent_bridge.config_loader import ConfigSource
from agent_bridge.events import Completion, Processing, StatusUpdate, TextDelta
from agent_bridge.session import SessionManager
from tests.helpers import (
    codex_agent_message_line,
    codex_command_start_line,
    codex_thread_started_line,
    codex_turn_completed_line,
    codex_turn_started_line,
    install_fake_cli,
)
from tests.helpers.events import collect_events, event_types

pytestmark = pytest.mark.integration

THREAD_ID = "0199a213-81ac-7f20-af0f-a314f9d6f83d"


def _happy_lines(text: str = "all done") -> list[str]:
    return [
        codex_thread_started_line(THREAD_ID),
        codex_turn_started_line(),
        codex_command_start_line("ls -la"),
        codex_agent_message_line(text),
        codex_turn_completed_line(input_tokens=1200, cached_input_tokens=1000, output_tokens=300),
    ]


def _make_config(tmp_path) -> CodexConfig:
    return CodexConfig.from_source(
        ConfigSource(
            {
                "agents": {
                    "codex": {
                        "work_dir": str(tmp_path),
                        "session_map_path": str(tmp_path / "codex-sessions.json"),
                    }
                }
            },
            env={},
        )
    )


@pytest.fixture
def bridge_setup(tmp_path, prepend_path, clean_agent_bridge_env):
    args_log = tmp_path / "cli-args.log"
    install_fake_cli(tmp_path / "bin", name="codex", lines=_happy_lines(), args_log=args_log)
    prepend_path(tmp_path / "bin")

    config = _make_config(tmp_path)
    session_manager = SessionManager(tmp_path / "sessions.json")
    bridge = Bridge(session_manager, CodexController(config), max_concurrent=2)
    return bridge, session_manager, config, args_log


async def test_first_turn_streams_full_event_sequence(bridge_setup):
    bridge, session_manager, config, args_log = bridge_setup

    events = await collect_events(
        bridge.handle_message("slack:C1:t1", "[alice]: hi", system_prompt="be brief")
    )

    assert event_types(events) == [Processing, StatusUpdate, TextDelta, Completion]
    assert events[1].status == "Running command..."
    assert events[2].text == "all done"
    completion = events[3]
    assert completion.is_error is False
    # Final text comes from the last agent message (turn.completed has none)
    assert completion.text == "all done"
    assert completion.usage is not None
    assert completion.usage.input_tokens == 200  # 1200 minus 1000 cached
    assert completion.usage.cache_read_tokens == 1000
    assert completion.usage.output_tokens == 300
    assert completion.usage.cost_usd == 0.0
    assert completion.session_usage is not None
    assert completion.session_usage.output_tokens == 300

    # bridge session persisted + mapped to the codex-minted thread id
    session_id = session_manager.get("slack:C1:t1")
    assert session_id is not None
    assert json.loads(config.session_map_path.read_text()) == {session_id: THREAD_ID}

    # The prepended system prompt contains newlines, so this single
    # invocation spans multiple lines in the log — assert on the whole text.
    invocation = args_log.read_text()
    assert invocation.startswith("exec --json")
    assert "resume" not in invocation
    assert "--sandbox workspace-write" in invocation
    assert "--skip-git-repo-check" in invocation
    assert "[alice]: hi" in invocation
    assert "<platform-directives>" in invocation
    assert "be brief" in invocation


async def test_second_turn_resumes_native_thread(bridge_setup):
    bridge, session_manager, config, args_log = bridge_setup

    await collect_events(bridge.handle_message("slack:C1:t1", "first"))
    events = await collect_events(bridge.handle_message("slack:C1:t1", "second"))

    assert event_types(events)[-1] is Completion
    assert events[-1].is_error is False

    invocations = args_log.read_text().splitlines()
    assert len(invocations) == 2
    assert "resume" not in invocations[0]
    # Resume targets the codex thread id, not the bridge session id
    assert invocations[1].startswith(f"exec resume {THREAD_ID} --json")

    session_id = session_manager.get("slack:C1:t1")
    assert json.loads(config.session_map_path.read_text()) == {session_id: THREAD_ID}


async def test_timeout_yields_error_completion(tmp_path, prepend_path, clean_agent_bridge_env):
    install_fake_cli(tmp_path / "bin", name="codex", lines=_happy_lines(), line_delay=2.0)
    prepend_path(tmp_path / "bin")
    config = CodexConfig.from_source(
        ConfigSource(
            {
                "agents": {
                    "codex": {
                        "work_dir": str(tmp_path),
                        "timeout_seconds": 0.5,
                        "session_map_path": str(tmp_path / "codex-sessions.json"),
                    }
                }
            },
            env={},
        )
    )
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        CodexController(config),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "timed out" in completion.text


async def test_nonzero_exit_without_terminal_event_yields_error_completion(
    tmp_path, prepend_path, clean_agent_bridge_env
):
    install_fake_cli(
        tmp_path / "bin",
        name="codex",
        lines=[
            codex_thread_started_line(THREAD_ID),
            codex_agent_message_line("partial answer"),
        ],
        exit_code=3,
    )
    prepend_path(tmp_path / "bin")
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        CodexController(_make_config(tmp_path)),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "exited with code 3" in completion.text


async def test_lost_mapping_falls_back_to_fresh_thread(bridge_setup, tmp_path):
    bridge, session_manager, config, args_log = bridge_setup

    await collect_events(bridge.handle_message("slack:C1:t1", "first"))
    assert config.session_map_path.exists()

    # Simulate a restart that lost the map file: same session store, fresh
    # controller with an empty mapping.
    config.session_map_path.unlink()
    bridge_after_restart = Bridge(session_manager, CodexController(config), max_concurrent=2)

    events = await collect_events(bridge_after_restart.handle_message("slack:C1:t1", "second"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is False

    invocations = args_log.read_text().splitlines()
    assert len(invocations) == 2
    assert "resume" not in invocations[1]  # degraded to a new thread

    # The fresh thread id was re-captured into the map
    session_id = session_manager.get("slack:C1:t1")
    assert json.loads(config.session_map_path.read_text()) == {session_id: THREAD_ID}

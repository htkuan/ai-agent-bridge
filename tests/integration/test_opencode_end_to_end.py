"""Fake opencode CLI → real OpencodeController → real Bridge/SessionManager."""

from __future__ import annotations

import json

import pytest

from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.controller import OpencodeController
from agent_bridge.bridge import Bridge
from agent_bridge.config_loader import ConfigSource
from agent_bridge.events import Completion, Processing, StatusUpdate, TextDelta
from agent_bridge.session import SessionManager
from tests.helpers import (
    install_fake_cli,
    opencode_error_line,
    opencode_step_finish_line,
    opencode_step_start_line,
    opencode_text_line,
    opencode_tool_use_line,
)
from tests.helpers.events import collect_events, event_types

pytestmark = pytest.mark.integration

SESSION_ID = "ses_494719016ffe85dkDMj0FPRbHK"


def _happy_lines(text: str = "all done") -> list[str]:
    # Two steps: a tool-using step, then the final answer — exercises usage
    # aggregation across step_finish events and final-text selection.
    return [
        opencode_step_start_line(session_id=SESSION_ID),
        opencode_text_line("Let me check...", session_id=SESSION_ID),
        opencode_tool_use_line(
            tool="bash", title="ls -la", session_id=SESSION_ID
        ),
        opencode_step_finish_line(
            cost=0.001,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=500,
            session_id=SESSION_ID,
        ),
        opencode_step_start_line(session_id=SESSION_ID),
        opencode_text_line(text, session_id=SESSION_ID),
        opencode_step_finish_line(
            cost=0.002,
            input_tokens=200,
            output_tokens=20,
            cache_read_tokens=600,
            cache_write_tokens=50,
            session_id=SESSION_ID,
        ),
    ]


def _make_config(tmp_path, **extra) -> OpencodeConfig:
    return OpencodeConfig.from_source(
        ConfigSource(
            {
                "agents": {
                    "opencode": {
                        "work_dir": str(tmp_path),
                        "session_map_path": str(tmp_path / "opencode-sessions.json"),
                        **extra,
                    }
                }
            },
            env={},
        )
    )


@pytest.fixture
def bridge_setup(tmp_path, prepend_path, clean_agent_bridge_env):
    args_log = tmp_path / "cli-args.log"
    install_fake_cli(
        tmp_path / "bin", name="opencode", lines=_happy_lines(), args_log=args_log
    )
    prepend_path(tmp_path / "bin")

    config = _make_config(tmp_path)
    session_manager = SessionManager(tmp_path / "sessions.json")
    bridge = Bridge(session_manager, OpencodeController(config), max_concurrent=2)
    return bridge, session_manager, config, args_log


async def test_first_turn_streams_full_event_sequence(bridge_setup):
    bridge, session_manager, config, args_log = bridge_setup

    events = await collect_events(
        bridge.handle_message("slack:C1:t1", "[alice]: hi", system_prompt="be brief")
    )

    assert event_types(events) == [
        Processing,
        TextDelta,
        StatusUpdate,
        TextDelta,
        Completion,
    ]
    assert events[1].text == "Let me check..."
    assert events[2].status == "Ran bash"
    assert events[3].text == "all done"
    completion = events[4]
    assert completion.is_error is False
    # Final text = the last step's text, not the intermediate narration
    assert completion.text == "all done"
    # Usage and cost are summed across both step_finish events
    assert completion.cost_usd == pytest.approx(0.003)
    assert completion.usage is not None
    assert completion.usage.input_tokens == 300
    assert completion.usage.output_tokens == 30
    assert completion.usage.cache_read_tokens == 1100
    assert completion.usage.cache_creation_tokens == 50
    assert completion.usage.num_turns == 2
    assert completion.session_usage is not None
    assert completion.session_usage.output_tokens == 30

    # bridge session persisted + mapped to the opencode-minted session id
    session_id = session_manager.get("slack:C1:t1")
    assert session_id is not None
    assert json.loads(config.session_map_path.read_text()) == {session_id: SESSION_ID}

    # The prepended system prompt contains newlines, so this single
    # invocation spans multiple lines in the log — assert on the whole text.
    invocation = args_log.read_text()
    assert invocation.startswith("run --format json")
    assert "--session" not in invocation
    assert "[alice]: hi" in invocation
    assert "<platform-directives>" in invocation
    assert "be brief" in invocation


async def test_second_turn_resumes_native_session(bridge_setup):
    bridge, session_manager, config, args_log = bridge_setup

    await collect_events(bridge.handle_message("slack:C1:t1", "first"))
    events = await collect_events(bridge.handle_message("slack:C1:t1", "second"))

    assert event_types(events)[-1] is Completion
    assert events[-1].is_error is False

    invocations = args_log.read_text().splitlines()
    assert len(invocations) == 2
    assert "--session" not in invocations[0]
    # Resume targets the opencode session id, not the bridge session id
    assert invocations[1].startswith(f"run --format json --session {SESSION_ID}")

    session_id = session_manager.get("slack:C1:t1")
    assert json.loads(config.session_map_path.read_text()) == {session_id: SESSION_ID}


async def test_timeout_yields_error_completion(
    tmp_path, prepend_path, clean_agent_bridge_env
):
    install_fake_cli(
        tmp_path / "bin", name="opencode", lines=_happy_lines(), line_delay=2.0
    )
    prepend_path(tmp_path / "bin")
    config = _make_config(tmp_path, timeout_seconds=0.5)
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        OpencodeController(config),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "timed out" in completion.text


async def test_error_event_yields_single_error_completion(
    tmp_path, prepend_path, clean_agent_bridge_env
):
    # An `error` event aborts the run and the CLI exits 1 — the reported
    # message must win over a generic exit-code error (no duplicate).
    install_fake_cli(
        tmp_path / "bin",
        name="opencode",
        lines=[
            opencode_step_start_line(session_id=SESSION_ID),
            opencode_error_line(message="rate limited", session_id=SESSION_ID),
        ],
        exit_code=1,
    )
    prepend_path(tmp_path / "bin")
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        OpencodeController(_make_config(tmp_path)),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert completions[0].is_error is True
    assert completions[0].text == "rate limited"


async def test_nonzero_exit_without_error_event_yields_error_completion(
    tmp_path, prepend_path, clean_agent_bridge_env
):
    # e.g. a stale --session id: "Session not found" on stderr, exit 1,
    # no JSON events at all.
    install_fake_cli(
        tmp_path / "bin",
        name="opencode",
        lines=[opencode_text_line("partial answer", session_id=SESSION_ID)],
        exit_code=3,
    )
    prepend_path(tmp_path / "bin")
    bridge = Bridge(
        SessionManager(tmp_path / "sessions.json"),
        OpencodeController(_make_config(tmp_path)),
        max_concurrent=2,
    )

    events = await collect_events(bridge.handle_message("slack:C1:t1", "hi"))

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "exited with code 3" in completion.text


async def test_lost_mapping_falls_back_to_fresh_session(bridge_setup, tmp_path):
    bridge, session_manager, config, args_log = bridge_setup

    await collect_events(bridge.handle_message("slack:C1:t1", "first"))
    assert config.session_map_path.exists()

    # Simulate a restart that lost the map file: same session store, fresh
    # controller with an empty mapping.
    config.session_map_path.unlink()
    bridge_after_restart = Bridge(
        session_manager, OpencodeController(config), max_concurrent=2
    )

    events = await collect_events(
        bridge_after_restart.handle_message("slack:C1:t1", "second")
    )

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is False

    invocations = args_log.read_text().splitlines()
    assert len(invocations) == 2
    assert "--session" not in invocations[1]  # degraded to a new session

    # The fresh session id was re-captured into the map
    session_id = session_manager.get("slack:C1:t1")
    assert json.loads(config.session_map_path.read_text()) == {session_id: SESSION_ID}

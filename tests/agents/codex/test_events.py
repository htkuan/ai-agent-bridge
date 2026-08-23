"""The codex ``exec --json`` line parser and its fold into bridge events."""

from __future__ import annotations

import json
from typing import Any

from agent_bridge.agents.codex.events import (
    AgentMessageEvent,
    CodexRunState,
    CommandStartEvent,
    ErrorItemEvent,
    FileChangeStartEvent,
    StreamErrorEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


# --- parse_stream_line ---


def test_parse_thread_started():
    events = parse_stream_line(
        _line({"type": "thread.started", "thread_id": "01a0-abc"})
    )
    assert events == [ThreadStartedEvent(thread_id="01a0-abc")]


def test_parse_agent_message():
    events = parse_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "hello"},
            }
        )
    )
    assert events == [AgentMessageEvent(text="hello")]


def test_parse_command_execution_started():
    events = parse_stream_line(
        _line(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'git status'",
                    "status": "in_progress",
                },
            }
        )
    )
    assert events == [CommandStartEvent(command="/bin/zsh -lc 'git status'")]


def test_parse_file_change_started():
    events = parse_stream_line(
        _line(
            {
                "type": "item.started",
                "item": {
                    "id": "item_7",
                    "type": "file_change",
                    "changes": [
                        {"path": "/abs/calc.py", "kind": "update"},
                        {"path": "/abs/test.py", "kind": "add"},
                    ],
                    "status": "in_progress",
                },
            }
        )
    )
    assert events == [FileChangeStartEvent(paths=["/abs/calc.py", "/abs/test.py"])]


def test_parse_error_item_from_either_lifecycle_edge():
    for event_type in ("item.started", "item.completed"):
        events = parse_stream_line(
            _line(
                {
                    "type": event_type,
                    "item": {"id": "item_0", "type": "error", "message": "boom"},
                }
            )
        )
        assert events == [ErrorItemEvent(message="boom")]


def test_parse_turn_completed_with_usage():
    events = parse_stream_line(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 77938,
                    "cached_input_tokens": 69504,
                    "cache_write_input_tokens": 12,
                    "output_tokens": 741,
                    "reasoning_output_tokens": 64,
                },
            }
        )
    )
    assert events == [
        TurnCompletedEvent(
            input_tokens=77938,
            cached_input_tokens=69504,
            cache_write_input_tokens=12,
            output_tokens=741,
        )
    ]


def test_parse_turn_failed_and_top_level_error():
    assert parse_stream_line(
        _line({"type": "turn.failed", "error": {"message": "bad model"}})
    ) == [TurnFailedEvent(message="bad model")]
    assert parse_stream_line(_line({"type": "error", "message": "bad model"})) == [
        StreamErrorEvent(message="bad model")
    ]


def test_parse_ignores_lifecycle_noise():
    assert parse_stream_line(_line({"type": "turn.started"})) == []
    for item_type in ("command_execution", "file_change", "reasoning"):
        line = _line({"type": "item.completed", "item": {"id": "i", "type": item_type}})
        assert parse_stream_line(line) == []
    assert parse_stream_line(_line({"type": "something.new"})) == []


def test_parse_skips_malformed_and_blank_lines():
    assert parse_stream_line("Reading additional input from stdin...") == []
    assert parse_stream_line("   ") == []


# --- to_bridge_event fold ---


def test_thread_started_records_thread_id_silently():
    state = CodexRunState()
    assert to_bridge_event(ThreadStartedEvent(thread_id="t-1"), state) is None
    assert state.thread_id == "t-1"


def test_agent_message_yields_delta_and_last_one_wins():
    state = CodexRunState()
    first = to_bridge_event(AgentMessageEvent(text="working on it"), state)
    second = to_bridge_event(AgentMessageEvent(text="the answer"), state)
    assert first == TextDelta(text="working on it")
    assert second == TextDelta(text="the answer")
    assert state.last_text == "the answer"


def test_command_start_becomes_status_update():
    event = to_bridge_event(CommandStartEvent(command="git status"), CodexRunState())
    assert event == StatusUpdate(status="Running a command...", detail="git status")


def test_file_change_becomes_status_update_with_joined_paths():
    event = to_bridge_event(
        FileChangeStartEvent(paths=["a.py", "b.py"]), CodexRunState()
    )
    assert event == StatusUpdate(status="Editing files...", detail="a.py, b.py")


def test_error_events_record_message_silently():
    state = CodexRunState()
    assert to_bridge_event(ErrorItemEvent(message="item boom"), state) is None
    assert state.last_error == "item boom"
    assert to_bridge_event(StreamErrorEvent(message="stream boom"), state) is None
    assert state.last_error == "stream boom"
    assert state.terminal is False


def test_turn_completed_builds_completion_with_mapped_usage():
    state = CodexRunState()
    to_bridge_event(AgentMessageEvent(text="final answer"), state)

    completion = to_bridge_event(
        TurnCompletedEvent(
            input_tokens=77938,
            cached_input_tokens=69504,
            cache_write_input_tokens=12,
            output_tokens=741,
        ),
        state,
    )

    assert state.terminal is True
    assert isinstance(completion, Completion)
    assert completion.text == "final answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.0
    assert completion.duration_ms >= 0
    assert completion.metadata["usage"] == {
        # cached_input_tokens is a subset of input_tokens.
        "input_tokens": 77938 - 69504,
        "output_tokens": 741,
        "cache_read_tokens": 69504,
        "cache_creation_tokens": 12,
        "num_turns": 1,
        "duration_api_ms": 0,
    }


def test_turn_failed_builds_error_completion():
    state = CodexRunState()
    completion = to_bridge_event(TurnFailedEvent(message="bad model"), state)
    assert state.terminal is True
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "bad model"


def test_turn_failed_without_message_falls_back_to_recorded_error():
    state = CodexRunState()
    to_bridge_event(ErrorItemEvent(message="detailed reason"), state)
    completion = to_bridge_event(TurnFailedEvent(message=""), state)
    assert isinstance(completion, Completion)
    assert completion.text == "detailed reason"


def test_turn_failed_with_nothing_recorded_has_generic_text():
    completion = to_bridge_event(TurnFailedEvent(message=""), CodexRunState())
    assert isinstance(completion, Completion)
    assert completion.text == "Codex turn failed"

from __future__ import annotations

import json
from typing import Any

from agent_bridge.agents.codex.events import (
    AgentMessageEvent,
    CodexRunState,
    CommandExecutionStartedEvent,
    ErrorItemEvent,
    FileChangeStartedEvent,
    ThreadStartedEvent,
    TopLevelErrorEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _item_line(event_type: str, item: dict[str, Any]) -> str:
    return _line({"type": event_type, "item": item})


def test_parse_thread_started():
    events = parse_stream_line(
        _line({"type": "thread.started", "thread_id": "thread-1"})
    )
    assert events == [ThreadStartedEvent(thread_id="thread-1")]


def test_parse_agent_message():
    events = parse_stream_line(
        _item_line(
            "item.completed",
            {"id": "item_0", "type": "agent_message", "text": "hello"},
        )
    )
    assert events == [AgentMessageEvent(text="hello")]


def test_parse_command_execution_started():
    events = parse_stream_line(
        _item_line(
            "item.started",
            {
                "id": "item_1",
                "type": "command_execution",
                "command": "git status",
            },
        )
    )
    assert events == [CommandExecutionStartedEvent(command="git status")]


def test_parse_file_change_started():
    events = parse_stream_line(
        _item_line(
            "item.started",
            {
                "id": "item_2",
                "type": "file_change",
                "changes": [
                    {"path": "/repo/a.py", "kind": "update"},
                    {"path": "/repo/b.py", "kind": "update"},
                ],
            },
        )
    )
    assert events == [FileChangeStartedEvent(paths=["/repo/a.py", "/repo/b.py"])]


def test_parse_error_item_and_top_level_error():
    assert parse_stream_line(
        _item_line(
            "item.started",
            {"id": "item_2", "type": "error", "message": "started bad"},
        )
    ) == [ErrorItemEvent(message="started bad")]
    assert parse_stream_line(
        _item_line(
            "item.completed",
            {"id": "item_3", "type": "error", "message": "bad model"},
        )
    ) == [ErrorItemEvent(message="bad model")]
    assert parse_stream_line(_line({"type": "error", "message": "http 400"})) == [
        TopLevelErrorEvent(message="http 400")
    ]


def test_parse_turn_completed_with_usage():
    events = parse_stream_line(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "cache_write_input_tokens": 5,
                    "output_tokens": 12,
                    "reasoning_output_tokens": 3,
                },
            }
        )
    )
    assert events == [
        TurnCompletedEvent(
            input_tokens=100,
            cached_input_tokens=40,
            cache_write_input_tokens=5,
            output_tokens=12,
            reasoning_output_tokens=3,
        )
    ]


def test_parse_turn_failed():
    assert parse_stream_line(
        _line({"type": "turn.failed", "error": {"message": "failed"}})
    ) == [TurnFailedEvent(message="failed")]
    assert parse_stream_line(_line({"type": "turn.failed", "error": "plain"})) == [
        TurnFailedEvent(message="plain")
    ]
    assert parse_stream_line(_line({"type": "turn.failed", "error": None})) == [
        TurnFailedEvent(message="")
    ]


def test_parse_ignores_lifecycle_noise_unknown_and_malformed():
    assert parse_stream_line(_line({"type": "turn.started"})) == []
    assert (
        parse_stream_line(
            _item_line(
                "item.completed",
                {"id": "item_4", "type": "command_execution", "status": "completed"},
            )
        )
        == []
    )
    assert parse_stream_line(_line({"type": "item.started", "item": "oops"})) == []
    assert parse_stream_line(
        _item_line(
            "item.started",
            {"id": "item_5", "type": "file_change", "changes": "oops"},
        )
    ) == [FileChangeStartedEvent(paths=[])]
    assert parse_stream_line(_line({"type": "something.new"})) == []
    assert parse_stream_line("not json") == []
    assert parse_stream_line("   ") == []


def test_thread_started_captures_thread_id():
    state = CodexRunState()
    event = to_bridge_event(ThreadStartedEvent(thread_id="thread-1"), state)
    assert event is None
    assert state.thread_id == "thread-1"


def test_agent_message_yields_delta_and_last_text_wins():
    state = CodexRunState()
    assert to_bridge_event(AgentMessageEvent(text="first"), state) == TextDelta(
        text="first"
    )
    assert to_bridge_event(AgentMessageEvent(text="final"), state) == TextDelta(
        text="final"
    )
    assert state.last_text == "final"


def test_command_and_file_items_become_status_updates():
    assert to_bridge_event(
        CommandExecutionStartedEvent(command="git status"), CodexRunState()
    ) == StatusUpdate(status="Running a command...", detail="git status")
    assert to_bridge_event(
        FileChangeStartedEvent(paths=["/repo/a.py", "/repo/b.py"]), CodexRunState()
    ) == StatusUpdate(status="Editing files...", detail="/repo/a.py, /repo/b.py")


def test_error_items_record_message_silently():
    state = CodexRunState()
    assert to_bridge_event(ErrorItemEvent(message="bad model"), state) is None
    assert to_bridge_event(TopLevelErrorEvent(message="http 400"), state) is None
    assert state.error_message == "http 400"


def test_turn_completed_builds_success_completion_with_usage():
    state = CodexRunState()
    to_bridge_event(AgentMessageEvent(text="final answer"), state)

    completion = to_bridge_event(
        TurnCompletedEvent(
            input_tokens=100,
            cached_input_tokens=40,
            cache_write_input_tokens=5,
            output_tokens=12,
            reasoning_output_tokens=3,
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
        "input_tokens": 60,
        "cache_read_tokens": 40,
        "cache_creation_tokens": 5,
        "output_tokens": 12,
        "cost_usd": 0.0,
        "duration_api_ms": 0,
        "num_turns": 1,
    }


def test_turn_failed_builds_error_completion():
    state = CodexRunState(error_message="earlier")
    completion = to_bridge_event(TurnFailedEvent(message="failed now"), state)

    assert state.terminal is True
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "failed now"

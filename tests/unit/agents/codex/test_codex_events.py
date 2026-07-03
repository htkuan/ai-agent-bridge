from __future__ import annotations

import json

from agent_bridge.agents.codex.events import (
    TERMINAL_EVENTS,
    AgentMessageEvent,
    CommandExecutionEvent,
    ErrorEvent,
    FileChangeEvent,
    McpToolCallEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnStartedEvent,
    WebSearchEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import Completion, StatusUpdate, TextDelta


def _item_line(phase: str, item: dict) -> str:
    return json.dumps({"type": f"item.{phase}", "item": item})


# --- parse_stream_line ---


def test_parse_thread_started():
    events = parse_stream_line(json.dumps({"type": "thread.started", "thread_id": "th-42"}))
    assert events == [ThreadStartedEvent(thread_id="th-42")]


def test_parse_turn_started():
    assert parse_stream_line(json.dumps({"type": "turn.started"})) == [TurnStartedEvent()]


def test_parse_agent_message_completed():
    events = parse_stream_line(
        _item_line("completed", {"id": "item_0", "type": "agent_message", "text": "hi"})
    )
    assert events == [AgentMessageEvent(text="hi")]


def test_parse_agent_message_started_and_updated_are_ignored():
    for phase in ("started", "updated"):
        line = _item_line(phase, {"id": "item_0", "type": "agent_message", "text": "par"})
        assert parse_stream_line(line) == []


def test_parse_command_execution_started():
    events = parse_stream_line(
        _item_line(
            "started",
            {"id": "item_1", "type": "command_execution", "command": "ls -la"},
        )
    )
    assert events == [CommandExecutionEvent(command="ls -la")]


def test_parse_command_execution_completed_is_internal():
    line = _item_line(
        "completed",
        {"id": "item_1", "type": "command_execution", "command": "ls", "exit_code": 0},
    )
    assert parse_stream_line(line) == []


def test_parse_mcp_tool_call_started():
    events = parse_stream_line(
        _item_line("started", {"id": "i", "type": "mcp_tool_call", "server": "gh", "tool": "pr"})
    )
    assert events == [McpToolCallEvent(server="gh", tool="pr")]


def test_parse_web_search_started():
    events = parse_stream_line(
        _item_line("started", {"id": "i", "type": "web_search", "query": "codex cli"})
    )
    assert events == [WebSearchEvent(query="codex cli")]


def test_parse_file_change_started():
    changes = [{"path": "a.py", "kind": "update"}]
    events = parse_stream_line(
        _item_line("started", {"id": "i", "type": "file_change", "changes": changes})
    )
    assert events == [FileChangeEvent(changes=changes)]


def test_parse_reasoning_and_todo_list_are_internal():
    for item_type in ("reasoning", "todo_list"):
        line = _item_line("started", {"id": "i", "type": item_type})
        assert parse_stream_line(line) == []


def test_parse_turn_completed_with_usage():
    events = parse_stream_line(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 1000,
                    "output_tokens": 300,
                    "reasoning_output_tokens": 100,
                },
            }
        )
    )
    assert events == [
        TurnCompletedEvent(input_tokens=1200, cached_input_tokens=1000, output_tokens=300)
    ]


def test_parse_turn_completed_without_usage():
    assert parse_stream_line(json.dumps({"type": "turn.completed"})) == [TurnCompletedEvent()]


def test_parse_turn_failed():
    events = parse_stream_line(
        json.dumps({"type": "turn.failed", "error": {"message": "rate limited"}})
    )
    assert events == [TurnFailedEvent(message="rate limited")]


def test_parse_error():
    assert parse_stream_line(json.dumps({"type": "error", "message": "bad auth"})) == [
        ErrorEvent(message="bad auth")
    ]


def test_parse_bad_json_skipped():
    assert parse_stream_line("{not json") == []


def test_parse_non_object_json_skipped():
    assert parse_stream_line('["a", "b"]') == []


def test_parse_empty_line_skipped():
    assert parse_stream_line("   \n") == []


def test_parse_unknown_event_type_skipped():
    assert parse_stream_line(json.dumps({"type": "session.hologram"})) == []


def test_parse_unknown_item_type_skipped():
    line = _item_line("started", {"id": "i", "type": "quantum_flux"})
    assert parse_stream_line(line) == []


def test_terminal_events_cover_all_three_endings():
    assert isinstance(TurnCompletedEvent(), TERMINAL_EVENTS)
    assert isinstance(TurnFailedEvent(), TERMINAL_EVENTS)
    assert isinstance(ErrorEvent(), TERMINAL_EVENTS)
    assert not isinstance(AgentMessageEvent(), TERMINAL_EVENTS)


# --- to_bridge_event ---


def test_agent_message_becomes_text_delta():
    assert to_bridge_event(AgentMessageEvent(text="hello")) == TextDelta(text="hello")


def test_command_execution_becomes_status_update_with_detail():
    event = to_bridge_event(CommandExecutionEvent(command="pytest -q"))
    assert isinstance(event, StatusUpdate)
    assert event.status == "Running command..."
    assert event.detail == "pytest -q"


def test_long_command_detail_is_truncated():
    event = to_bridge_event(CommandExecutionEvent(command="x" * 500))
    assert isinstance(event, StatusUpdate)
    assert len(event.detail) == 200
    assert event.detail.endswith("…")


def test_mcp_tool_call_becomes_status_update():
    event = to_bridge_event(McpToolCallEvent(server="github", tool="create_pr"))
    assert event == StatusUpdate(status="Using github.create_pr...")


def test_web_search_becomes_status_update():
    event = to_bridge_event(WebSearchEvent(query="asyncio timeout"))
    assert isinstance(event, StatusUpdate)
    assert event.detail == "asyncio timeout"


def test_file_change_becomes_status_update_with_paths():
    event = to_bridge_event(
        FileChangeEvent(
            changes=[{"path": "a.py", "kind": "update"}, {"path": "b.py", "kind": "add"}]
        )
    )
    assert isinstance(event, StatusUpdate)
    assert event.detail == "a.py, b.py"


def test_turn_completed_maps_usage_to_canonical_keys():
    completion = to_bridge_event(
        TurnCompletedEvent(input_tokens=1200, cached_input_tokens=1000, output_tokens=300)
    )
    assert isinstance(completion, Completion)
    assert completion.is_error is False
    # Codex input_tokens includes the cached prefix; canonical input excludes it.
    assert completion.metadata["usage"] == {
        "input_tokens": 200,
        "output_tokens": 300,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 0,
        "num_turns": 1,
    }
    assert completion.cost_usd == 0.0  # CLI reports no cost


def test_turn_completed_clamps_negative_input():
    completion = to_bridge_event(TurnCompletedEvent(input_tokens=10, cached_input_tokens=50))
    assert isinstance(completion, Completion)
    assert completion.metadata["usage"]["input_tokens"] == 0


def test_turn_failed_becomes_error_completion():
    completion = to_bridge_event(TurnFailedEvent(message="model overloaded"))
    assert completion == Completion(text="model overloaded", is_error=True)


def test_turn_failed_without_message_gets_fallback_text():
    completion = to_bridge_event(TurnFailedEvent())
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text


def test_error_becomes_error_completion():
    completion = to_bridge_event(ErrorEvent(message="stream disconnected"))
    assert completion == Completion(text="stream disconnected", is_error=True)


def test_internal_events_are_filtered():
    assert to_bridge_event(ThreadStartedEvent(thread_id="t")) is None
    assert to_bridge_event(TurnStartedEvent()) is None

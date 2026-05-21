from __future__ import annotations

import json

from agent_bridge.agents.codex.events import (
    AgentMessageItem,
    CodexEventTranslator,
    CommandExecutionItem,
    ErrorEvent,
    FileChangeItem,
    McpToolCallItem,
    ReasoningItem,
    ThreadStartedEvent,
    TodoListItem,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnStartedEvent,
    UnknownItem,
    WebSearchItem,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import Completion, StatusUpdate, TextDelta


# ---------------------------------------------------------------------------
# parse_stream_line
# ---------------------------------------------------------------------------


def test_parse_empty_and_whitespace_lines():
    assert parse_stream_line("") == []
    assert parse_stream_line("   ") == []
    assert parse_stream_line("\n") == []


def test_parse_invalid_json_swallowed():
    assert parse_stream_line("not json") == []
    assert parse_stream_line("{half") == []


def test_parse_unknown_top_level_type():
    assert parse_stream_line(json.dumps({"type": "unheard.of"})) == []


def test_parse_thread_started():
    line = json.dumps({"type": "thread.started", "thread_id": "abc-123"})
    events = parse_stream_line(line)
    assert events == [ThreadStartedEvent(thread_id="abc-123")]


def test_parse_thread_started_missing_id_becomes_empty_string():
    line = json.dumps({"type": "thread.started"})
    events = parse_stream_line(line)
    assert events == [ThreadStartedEvent(thread_id="")]


def test_parse_turn_started():
    assert parse_stream_line(json.dumps({"type": "turn.started"})) == [
        TurnStartedEvent()
    ]


def test_parse_turn_completed_with_usage():
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 30,
                "output_tokens": 50,
                "reasoning_output_tokens": 20,
            },
        }
    )
    events = parse_stream_line(line)
    assert events == [
        TurnCompletedEvent(
            input_tokens=100,
            cached_input_tokens=30,
            output_tokens=50,
            reasoning_output_tokens=20,
        )
    ]


def test_parse_turn_completed_missing_usage_defaults_to_zero():
    line = json.dumps({"type": "turn.completed"})
    events = parse_stream_line(line)
    assert events == [TurnCompletedEvent()]


def test_parse_turn_completed_handles_null_usage_fields():
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": None, "output_tokens": 5},
        }
    )
    events = parse_stream_line(line)
    assert events == [TurnCompletedEvent(input_tokens=0, output_tokens=5)]


def test_parse_turn_failed_with_error_object():
    line = json.dumps(
        {"type": "turn.failed", "error": {"message": "context length exceeded"}}
    )
    events = parse_stream_line(line)
    assert events == [TurnFailedEvent(message="context length exceeded")]


def test_parse_turn_failed_with_error_string():
    line = json.dumps({"type": "turn.failed", "error": "boom"})
    events = parse_stream_line(line)
    assert events == [TurnFailedEvent(message="boom")]


def test_parse_turn_failed_falls_back_to_default_message():
    line = json.dumps({"type": "turn.failed"})
    events = parse_stream_line(line)
    assert events == [TurnFailedEvent(message="turn failed")]


def test_parse_top_level_error():
    line = json.dumps({"type": "error", "message": "rate limited"})
    events = parse_stream_line(line)
    assert events == [ErrorEvent(message="rate limited")]


def test_parse_agent_message_item_completed():
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "item_3", "type": "agent_message", "text": "hi there"},
        }
    )
    events = parse_stream_line(line)
    assert events == [AgentMessageItem(item_id="item_3", text="hi there", completed=True)]


def test_parse_agent_message_item_updated_marks_not_completed():
    line = json.dumps(
        {
            "type": "item.updated",
            "item": {"id": "item_3", "type": "agent_message", "text": "hi"},
        }
    )
    events = parse_stream_line(line)
    assert events == [AgentMessageItem(item_id="item_3", text="hi", completed=False)]


def test_parse_command_execution_item():
    line = json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "cmd_1",
                "type": "command_execution",
                "command": "ls -la",
                "status": "in_progress",
            },
        }
    )
    events = parse_stream_line(line)
    assert events == [
        CommandExecutionItem(
            item_id="cmd_1",
            command="ls -la",
            status="in_progress",
            exit_code=None,
            completed=False,
        )
    ]


def test_parse_command_execution_completed_with_exit_code():
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_1",
                "type": "command_execution",
                "command": "ls",
                "status": "completed",
                "exit_code": 0,
            },
        }
    )
    events = parse_stream_line(line)
    assert events[0] == CommandExecutionItem(
        item_id="cmd_1",
        command="ls",
        status="completed",
        exit_code=0,
        completed=True,
    )


def test_parse_mcp_tool_call_item():
    line = json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": "mcp_1",
                "type": "mcp_tool_call",
                "server": "github",
                "tool": "search_code",
                "status": "in_progress",
            },
        }
    )
    events = parse_stream_line(line)
    assert events == [
        McpToolCallItem(
            item_id="mcp_1",
            server="github",
            tool="search_code",
            status="in_progress",
            completed=False,
        )
    ]


def test_parse_web_search_item():
    line = json.dumps(
        {"type": "item.started", "item": {"id": "ws_1", "type": "web_search"}}
    )
    assert parse_stream_line(line) == [WebSearchItem(item_id="ws_1", completed=False)]


def test_parse_todo_list_item_completed():
    line = json.dumps(
        {"type": "item.completed", "item": {"id": "td_1", "type": "todo_list"}}
    )
    assert parse_stream_line(line) == [TodoListItem(item_id="td_1", completed=True)]


def test_parse_file_change_item_completed():
    line = json.dumps(
        {"type": "item.completed", "item": {"id": "fc_1", "type": "file_change"}}
    )
    assert parse_stream_line(line) == [FileChangeItem(item_id="fc_1", completed=True)]


def test_parse_reasoning_item():
    line = json.dumps(
        {"type": "item.completed", "item": {"id": "r_1", "type": "reasoning"}}
    )
    assert parse_stream_line(line) == [ReasoningItem(item_id="r_1", completed=True)]


def test_parse_unknown_item_type_kept_as_unknown():
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "x_1", "type": "newfangled_thing", "foo": "bar"},
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, UnknownItem)
    assert ev.item_id == "x_1"
    assert ev.item_type == "newfangled_thing"
    assert ev.completed is True
    assert ev.raw == {"id": "x_1", "type": "newfangled_thing", "foo": "bar"}


# ---------------------------------------------------------------------------
# to_bridge_event (stateless)
# ---------------------------------------------------------------------------


def test_to_bridge_command_execution_in_progress():
    ev = CommandExecutionItem(
        item_id="c1", command="ls -la", status="in_progress",
        exit_code=None, completed=False,
    )
    result = to_bridge_event(ev)
    assert result == StatusUpdate(status="Running: ls -la")


def test_to_bridge_command_execution_completed_filtered():
    ev = CommandExecutionItem(
        item_id="c1", command="ls", status="completed",
        exit_code=0, completed=True,
    )
    assert to_bridge_event(ev) is None


def test_to_bridge_command_execution_truncates_first_line():
    long = "x" * 200
    ev = CommandExecutionItem(
        item_id="c1", command=long + "\nmore", status="in_progress",
        exit_code=None, completed=False,
    )
    result = to_bridge_event(ev)
    assert isinstance(result, StatusUpdate)
    # 80 char preview, prefixed with "Running: "
    assert result.status == "Running: " + ("x" * 80)


def test_to_bridge_mcp_tool_call_with_server():
    ev = McpToolCallItem(
        item_id="m1", server="github", tool="search",
        status="in_progress", completed=False,
    )
    assert to_bridge_event(ev) == StatusUpdate(status="Using github.search...")


def test_to_bridge_mcp_tool_call_without_server():
    ev = McpToolCallItem(
        item_id="m1", server="", tool="search",
        status="in_progress", completed=False,
    )
    assert to_bridge_event(ev) == StatusUpdate(status="Using search...")


def test_to_bridge_web_search():
    ev = WebSearchItem(item_id="w1", completed=False)
    assert to_bridge_event(ev) == StatusUpdate(status="Searching the web...")


def test_to_bridge_todo_list_completed():
    ev = TodoListItem(item_id="t1", completed=True)
    assert to_bridge_event(ev) == StatusUpdate(status="Updated todo list")


def test_to_bridge_file_change_completed():
    ev = FileChangeItem(item_id="f1", completed=True)
    assert to_bridge_event(ev) == StatusUpdate(status="Edited files")


def test_to_bridge_reasoning_filtered():
    assert to_bridge_event(ReasoningItem(item_id="r1", completed=True)) is None


def test_to_bridge_thread_and_turn_lifecycle_filtered():
    assert to_bridge_event(ThreadStartedEvent(thread_id="abc")) is None
    assert to_bridge_event(TurnStartedEvent()) is None
    # TurnCompleted/Failed/AgentMessage are translator-owned, return None here
    assert to_bridge_event(TurnCompletedEvent()) is None
    assert to_bridge_event(TurnFailedEvent(message="x")) is None
    assert to_bridge_event(AgentMessageItem("i", "hi", completed=True)) is None


# ---------------------------------------------------------------------------
# CodexEventTranslator (stateful)
# ---------------------------------------------------------------------------


def test_translator_thread_started_emits_nothing():
    t = CodexEventTranslator()
    assert t.translate(ThreadStartedEvent(thread_id="abc")) == []


def test_translator_turn_started_emits_nothing():
    t = CodexEventTranslator()
    assert t.translate(TurnStartedEvent()) == []


def test_translator_full_agent_message_via_completed_only():
    """Codex 0.128 only emits item.completed for agent_message in practice."""
    t = CodexEventTranslator()
    out = t.translate(AgentMessageItem("i1", "hello world", completed=True))
    assert out == [TextDelta(text="hello world")]
    assert t.last_assistant_text == "hello world"


def test_translator_incremental_deltas_from_item_updated():
    t = CodexEventTranslator()
    # First chunk
    assert t.translate(AgentMessageItem("i1", "hello", completed=False)) == [
        TextDelta(text="hello")
    ]
    # Cumulative text grows
    assert t.translate(AgentMessageItem("i1", "hello world", completed=False)) == [
        TextDelta(text=" world")
    ]
    # Final completion adds nothing new
    assert t.translate(AgentMessageItem("i1", "hello world", completed=True)) == []
    assert t.last_assistant_text == "hello world"


def test_translator_no_emission_when_text_unchanged():
    t = CodexEventTranslator()
    t.translate(AgentMessageItem("i1", "hi", completed=False))
    # Same text again — no delta
    assert t.translate(AgentMessageItem("i1", "hi", completed=False)) == []


def test_translator_non_monotonic_update_emits_full_text():
    t = CodexEventTranslator()
    t.translate(AgentMessageItem("i1", "hello world", completed=False))
    # Codex rewrote the message (rare). Emit the new full text rather than
    # silently dropping it.
    out = t.translate(AgentMessageItem("i1", "different text", completed=False))
    assert out == [TextDelta(text="different text")]


def test_translator_empty_text_update_emits_nothing_but_records_completion():
    t = CodexEventTranslator()
    out = t.translate(AgentMessageItem("i1", "", completed=True))
    assert out == []
    assert t.last_assistant_text == ""


def test_translator_separate_items_track_independently():
    t = CodexEventTranslator()
    t.translate(AgentMessageItem("i1", "first", completed=True))
    out = t.translate(AgentMessageItem("i2", "second", completed=False))
    assert out == [TextDelta(text="second")]
    # last_assistant_text follows the latest *completed* message
    assert t.last_assistant_text == "first"


def test_translator_turn_completed_uses_last_assistant_text():
    t = CodexEventTranslator()
    t.translate(AgentMessageItem("i1", "final answer", completed=True))
    out = t.translate(
        TurnCompletedEvent(
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=5,
            reasoning_output_tokens=3,
        )
    )
    assert len(out) == 1
    completion = out[0]
    assert isinstance(completion, Completion)
    assert completion.text == "final answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.0
    assert completion.metadata == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_output_tokens": 3,
    }


def test_translator_turn_completed_with_no_prior_message_emits_empty_text():
    t = CodexEventTranslator()
    out = t.translate(TurnCompletedEvent(output_tokens=0))
    assert out == [
        Completion(
            text="",
            is_error=False,
            cost_usd=0.0,
            duration_ms=0,
            metadata={
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
        )
    ]


def test_translator_turn_failed_emits_error_completion():
    t = CodexEventTranslator()
    out = t.translate(TurnFailedEvent(message="model went sideways"))
    assert out == [Completion(text="model went sideways", is_error=True)]


def test_translator_turn_failed_falls_back_to_default_message():
    t = CodexEventTranslator()
    out = t.translate(TurnFailedEvent(message=""))
    assert out == [Completion(text="Codex turn failed", is_error=True)]


def test_translator_top_level_error_emits_error_completion():
    t = CodexEventTranslator()
    out = t.translate(ErrorEvent(message="rate limited"))
    assert out == [Completion(text="rate limited", is_error=True)]


def test_translator_status_updates_pass_through_to_stateless_converter():
    t = CodexEventTranslator()
    out = t.translate(WebSearchItem(item_id="w1", completed=False))
    assert out == [StatusUpdate(status="Searching the web...")]


def test_translator_filtered_events_yield_nothing():
    t = CodexEventTranslator()
    assert t.translate(ReasoningItem(item_id="r1", completed=True)) == []
    assert (
        t.translate(
            CommandExecutionItem(
                item_id="c1", command="ls", status="completed",
                exit_code=0, completed=True,
            )
        )
        == []
    )

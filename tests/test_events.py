import json

from agent_bridge.claude.events import (
    AssistantTextEvent,
    InitEvent,
    ResultEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    parse_stream_line,
)


def test_parse_init_event():
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "abc-123",
            "model": "claude-opus-4-6",
            "tools": ["Bash", "Read", "Edit"],
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InitEvent)
    assert event.session_id == "abc-123"
    assert event.model == "claude-opus-4-6"
    assert event.tools == ["Bash", "Read", "Edit"]


def test_parse_assistant_text():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello world"}],
            },
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextEvent)
    assert events[0].text == "Hello world"
    assert events[0].session_id == "abc-123"


def test_parse_thinking_event():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "Let me think..."}],
            },
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].thinking == "Let me think..."


def test_parse_tool_use():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "Bash"
    assert events[0].tool_input == {"command": "ls"}


def test_parse_tool_result():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "file1.txt\nfile2.txt",
                        "is_error": False,
                    }
                ],
            },
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolResultEvent)
    assert events[0].output == "file1.txt\nfile2.txt"
    assert events[0].is_error is False


def test_parse_result_event():
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done!",
            "total_cost_usd": 0.05,
            "duration_ms": 3000,
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].result_text == "Done!"
    assert events[0].cost_usd == 0.05
    assert events[0].duration_ms == 3000
    assert events[0].is_error is False


def test_parse_error_result():
    line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Authentication failed",
            "total_cost_usd": 0,
            "duration_ms": 40,
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ResultEvent)
    assert events[0].is_error is True
    assert events[0].result_text == "Authentication failed"


def test_parse_empty_line():
    assert parse_stream_line("") == []
    assert parse_stream_line("   ") == []


def test_parse_invalid_json():
    assert parse_stream_line("not json") == []


def test_parse_unknown_event_type():
    line = json.dumps({"type": "rate_limit_event", "session_id": "abc"})
    assert parse_stream_line(line) == []


def test_parse_hook_event_ignored():
    line = json.dumps(
        {
            "type": "system",
            "subtype": "hook_started",
            "session_id": "abc-123",
        }
    )
    assert parse_stream_line(line) == []


def test_parse_multi_content_blocks():
    """A single assistant message with both thinking and text content."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me reason..."},
                    {"type": "text", "text": "Here is the answer."},
                ],
            },
            "session_id": "abc-123",
        }
    )
    events = parse_stream_line(line)
    assert len(events) == 2
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].thinking == "Let me reason..."
    assert isinstance(events[1], AssistantTextEvent)
    assert events[1].text == "Here is the answer."


def test_parse_assistant_empty_content():
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": []},
            "session_id": "abc-123",
        }
    )
    assert parse_stream_line(line) == []

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
    event = parse_stream_line(line)
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
    event = parse_stream_line(line)
    assert isinstance(event, AssistantTextEvent)
    assert event.text == "Hello world"
    assert event.session_id == "abc-123"


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
    event = parse_stream_line(line)
    assert isinstance(event, ThinkingEvent)
    assert event.thinking == "Let me think..."


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
    event = parse_stream_line(line)
    assert isinstance(event, ToolUseEvent)
    assert event.tool_name == "Bash"
    assert event.tool_input == {"command": "ls"}


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
    event = parse_stream_line(line)
    assert isinstance(event, ToolResultEvent)
    assert event.output == "file1.txt\nfile2.txt"
    assert event.is_error is False


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
    event = parse_stream_line(line)
    assert isinstance(event, ResultEvent)
    assert event.result_text == "Done!"
    assert event.cost_usd == 0.05
    assert event.duration_ms == 3000
    assert event.is_error is False


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
    event = parse_stream_line(line)
    assert isinstance(event, ResultEvent)
    assert event.is_error is True
    assert event.result_text == "Authentication failed"


def test_parse_empty_line():
    assert parse_stream_line("") is None
    assert parse_stream_line("   ") is None


def test_parse_invalid_json():
    assert parse_stream_line("not json") is None


def test_parse_unknown_event_type():
    line = json.dumps({"type": "rate_limit_event", "session_id": "abc"})
    assert parse_stream_line(line) is None


def test_parse_hook_event_ignored():
    line = json.dumps(
        {
            "type": "system",
            "subtype": "hook_started",
            "session_id": "abc-123",
        }
    )
    assert parse_stream_line(line) is None

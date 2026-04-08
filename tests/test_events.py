import json

from agent_bridge.agents.claude.events import (
    AssistantTextEvent,
    InitEvent,
    ResultEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import Completion, StatusUpdate, TextDelta, UserQuestion


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


# --- to_bridge_event tests ---


def test_bridge_event_from_text():
    event = AssistantTextEvent(session_id="s1", text="Hello")
    result = to_bridge_event(event)
    assert isinstance(result, TextDelta)
    assert result.text == "Hello"


def test_bridge_event_from_tool_use():
    event = ToolUseEvent(session_id="s1", tool_name="Bash", tool_input={"command": "ls"})
    result = to_bridge_event(event)
    assert isinstance(result, StatusUpdate)
    assert result.status == "Using Bash..."


def test_bridge_event_from_result():
    event = ResultEvent(
        session_id="s1", result_text="Done", cost_usd=0.05, duration_ms=3000
    )
    result = to_bridge_event(event)
    assert isinstance(result, Completion)
    assert result.text == "Done"
    assert result.cost_usd == 0.05
    assert result.duration_ms == 3000
    assert result.is_error is False


def test_bridge_event_from_error_result():
    event = ResultEvent(session_id="s1", result_text="Failed", is_error=True)
    result = to_bridge_event(event)
    assert isinstance(result, Completion)
    assert result.is_error is True


def test_bridge_event_from_init_returns_none():
    event = InitEvent(session_id="s1", model="claude-opus-4-6", tools=["Bash"])
    assert to_bridge_event(event) is None


def test_bridge_event_from_thinking_returns_none():
    event = ThinkingEvent(session_id="s1", thinking="Let me think...")
    assert to_bridge_event(event) is None


def test_bridge_event_from_tool_result_returns_none():
    event = ToolResultEvent(session_id="s1", output="file.txt")
    assert to_bridge_event(event) is None


def test_bridge_event_from_ask_user_question():
    questions = [{"question": "Which database?", "options": ["pg", "mysql"]}]
    event = ToolUseEvent(
        session_id="s1",
        tool_name="AskUserQuestion",
        tool_input={"questions": questions},
    )
    result = to_bridge_event(event)
    assert isinstance(result, UserQuestion)
    assert result.questions == questions


def test_bridge_event_from_ask_user_question_fallback():
    """AskUserQuestion without questions key falls back to StatusUpdate."""
    event = ToolUseEvent(
        session_id="s1",
        tool_name="AskUserQuestion",
        tool_input={},
    )
    result = to_bridge_event(event)
    assert isinstance(result, StatusUpdate)
    assert "AskUserQuestion" in result.status


def test_bridge_event_other_tool_still_status_update():
    """Non-AskUserQuestion tools remain StatusUpdate."""
    event = ToolUseEvent(
        session_id="s1", tool_name="Bash", tool_input={"command": "ls"}
    )
    result = to_bridge_event(event)
    assert isinstance(result, StatusUpdate)
    assert result.status == "Using Bash..."

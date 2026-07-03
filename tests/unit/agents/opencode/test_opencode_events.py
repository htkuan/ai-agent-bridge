from __future__ import annotations

import json

from agent_bridge.agents.opencode.events import (
    TERMINAL_EVENTS,
    SessionErrorEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextPartEvent,
    ToolUseEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import Completion, StatusUpdate, TextDelta
from tests.helpers import (
    opencode_error_line,
    opencode_step_finish_line,
    opencode_step_start_line,
    opencode_text_line,
    opencode_tool_use_line,
)

# --- parse_stream_line ---


def test_parse_step_start_carries_session_id():
    events = parse_stream_line(opencode_step_start_line(session_id="ses_42"))
    assert events == [StepStartedEvent(session_id="ses_42")]


def test_parse_text_part():
    events = parse_stream_line(opencode_text_line("hello", session_id="ses_42"))
    assert events == [TextPartEvent(text="hello", session_id="ses_42")]


def test_parse_empty_text_part_skipped():
    assert parse_stream_line(opencode_text_line("")) == []
    assert parse_stream_line(opencode_text_line("  \n ")) == []


def test_parse_tool_use_completed():
    events = parse_stream_line(
        opencode_tool_use_line(tool="bash", title="pytest -q", status="completed")
    )
    assert events == [
        ToolUseEvent(tool="bash", title="pytest -q", failed=False, session_id="ses_fake")
    ]


def test_parse_tool_use_error():
    events = parse_stream_line(
        opencode_tool_use_line(tool="edit", title="a.py", status="error")
    )
    assert events[0].failed is True


def test_parse_step_finish_extracts_cost_and_tokens():
    events = parse_stream_line(
        opencode_step_finish_line(
            cost=0.0015,
            input_tokens=671,
            output_tokens=8,
            reasoning_tokens=2,
            cache_read_tokens=21415,
            cache_write_tokens=100,
        )
    )
    assert events == [
        StepFinishedEvent(
            cost=0.0015,
            input_tokens=671,
            output_tokens=8,
            reasoning_tokens=2,
            cache_read_tokens=21415,
            cache_write_tokens=100,
            session_id="ses_fake",
        )
    ]


def test_parse_step_finish_without_tokens_defaults_to_zero():
    line = json.dumps(
        {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish"}}
    )
    assert parse_stream_line(line) == [StepFinishedEvent(session_id="ses_1")]


def test_parse_error_prefers_nested_data_message():
    events = parse_stream_line(
        opencode_error_line(message="rate limited", name="APIError")
    )
    assert events == [
        SessionErrorEvent(message="rate limited", session_id="ses_fake")
    ]


def test_parse_error_falls_back_to_flat_message_then_name():
    flat = json.dumps(
        {"type": "error", "sessionID": "s", "error": {"message": "boom"}}
    )
    assert parse_stream_line(flat)[0].message == "boom"
    name_only = json.dumps(
        {"type": "error", "sessionID": "s", "error": {"name": "UnknownError"}}
    )
    assert parse_stream_line(name_only)[0].message == "UnknownError"


def test_parse_error_without_details_gets_empty_message():
    assert parse_stream_line(json.dumps({"type": "error"})) == [SessionErrorEvent()]


def test_parse_reasoning_is_internal():
    line = json.dumps(
        {
            "type": "reasoning",
            "sessionID": "s",
            "part": {"type": "reasoning", "text": "hmm"},
        }
    )
    assert parse_stream_line(line) == []


def test_parse_bad_json_skipped():
    assert parse_stream_line("{not json") == []


def test_parse_non_object_json_skipped():
    assert parse_stream_line('["a", "b"]') == []


def test_parse_empty_line_skipped():
    assert parse_stream_line("   \n") == []


def test_parse_unknown_event_type_skipped():
    assert parse_stream_line(json.dumps({"type": "session.hologram"})) == []


def test_parse_missing_part_is_tolerated():
    # A malformed line without `part` must not raise
    assert parse_stream_line(json.dumps({"type": "text", "sessionID": "s"})) == []
    assert parse_stream_line(json.dumps({"type": "tool_use", "sessionID": "s"})) == [
        ToolUseEvent(session_id="s")
    ]


def test_only_session_error_is_terminal():
    assert isinstance(SessionErrorEvent(), TERMINAL_EVENTS)
    assert not isinstance(StepFinishedEvent(), TERMINAL_EVENTS)
    assert not isinstance(TextPartEvent(), TERMINAL_EVENTS)


# --- to_bridge_event ---


def test_text_part_becomes_text_delta():
    assert to_bridge_event(TextPartEvent(text="hello")) == TextDelta(text="hello")


def test_tool_use_becomes_status_update_with_detail():
    event = to_bridge_event(ToolUseEvent(tool="bash", title="ls -la"))
    assert event == StatusUpdate(status="Ran bash", detail="ls -la")


def test_failed_tool_use_becomes_failure_status():
    event = to_bridge_event(ToolUseEvent(tool="edit", title="a.py", failed=True))
    assert isinstance(event, StatusUpdate)
    assert event.status == "Tool edit failed"


def test_long_tool_title_is_truncated():
    event = to_bridge_event(ToolUseEvent(tool="bash", title="x" * 500))
    assert isinstance(event, StatusUpdate)
    assert len(event.detail) == 200
    assert event.detail.endswith("…")


def test_session_error_becomes_error_completion():
    completion = to_bridge_event(SessionErrorEvent(message="model overloaded"))
    assert completion == Completion(text="model overloaded", is_error=True)


def test_session_error_without_message_gets_fallback_text():
    completion = to_bridge_event(SessionErrorEvent())
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text


def test_step_boundaries_are_filtered():
    assert to_bridge_event(StepStartedEvent()) is None
    assert to_bridge_event(StepFinishedEvent(cost=0.1, input_tokens=5)) is None

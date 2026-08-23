"""The opencode ``run --format json`` line parser, its fold into bridge
events, and the EOF Completion synthesis (the stream has no terminal event)."""

from __future__ import annotations

import json
from typing import Any

from agent_bridge.agents.opencode.events import (
    OpencodeRunState,
    SessionAnnouncedEvent,
    StepFinishEvent,
    StreamErrorEvent,
    TextEvent,
    ToolUseEvent,
    completion_at_eof,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


# --- parse_stream_line ---


def test_parse_text_announces_session_and_carries_text():
    events = parse_stream_line(
        _line(
            {
                "type": "text",
                "sessionID": "ses_abc",
                "part": {"type": "text", "text": "hello"},
            }
        )
    )
    assert events == [
        SessionAnnouncedEvent(opencode_session_id="ses_abc"),
        TextEvent(text="hello"),
    ]


def test_parse_tool_use():
    events = parse_stream_line(
        _line(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "callID": "call_0",
                    "state": {"status": "completed", "input": {"filePath": "calc.py"}},
                },
            }
        )
    )
    assert events == [
        SessionAnnouncedEvent(opencode_session_id="ses_abc"),
        ToolUseEvent(tool="read"),
    ]


def test_parse_step_finish_with_usage():
    events = parse_stream_line(
        _line(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {
                    "type": "step-finish",
                    "reason": "tool-calls",
                    "tokens": {
                        "total": 5986,
                        "input": 3,
                        "output": 30,
                        "reasoning": 9,
                        "cache": {"write": 5944, "read": 7},
                    },
                    "cost": 0.0015334,
                },
            }
        )
    )
    assert events == [
        SessionAnnouncedEvent(opencode_session_id="ses_abc"),
        StepFinishEvent(
            input_tokens=3,
            output_tokens=30,
            cache_read_tokens=7,
            cache_write_tokens=5944,
            cost_usd=0.0015334,
        ),
    ]


def test_parse_error_extracts_nested_message():
    events = parse_stream_line(
        _line(
            {
                "type": "error",
                "sessionID": "ses_abc",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "Unexpected server error.", "ref": "err_1"},
                },
            }
        )
    )
    assert events == [
        SessionAnnouncedEvent(opencode_session_id="ses_abc"),
        StreamErrorEvent(message="Unexpected server error."),
    ]


def test_parse_error_falls_back_to_name_without_message():
    events = parse_stream_line(
        _line({"type": "error", "error": {"name": "UnknownError"}})
    )
    assert events == [StreamErrorEvent(message="UnknownError")]


def test_parse_ignores_lifecycle_noise_but_still_announces_session():
    assert parse_stream_line(
        _line({"type": "step_start", "sessionID": "ses_x", "part": {}})
    ) == [SessionAnnouncedEvent(opencode_session_id="ses_x")]
    assert parse_stream_line(
        _line({"type": "something.new", "sessionID": "ses_x"})
    ) == [SessionAnnouncedEvent(opencode_session_id="ses_x")]


def test_parse_skips_malformed_and_blank_lines():
    assert parse_stream_line("not json") == []
    assert parse_stream_line("   ") == []


# --- to_bridge_event fold ---


def test_session_announced_records_id_silently():
    state = OpencodeRunState()
    event = SessionAnnouncedEvent(opencode_session_id="ses_1")
    assert to_bridge_event(event, state) is None
    assert state.opencode_session_id == "ses_1"


def test_text_yields_delta_and_last_one_wins():
    state = OpencodeRunState()
    first = to_bridge_event(TextEvent(text="working on it"), state)
    second = to_bridge_event(TextEvent(text="the answer"), state)
    assert first == TextDelta(text="working on it")
    assert second == TextDelta(text="the answer")
    assert state.last_text == "the answer"


def test_tool_use_becomes_status_update():
    event = to_bridge_event(ToolUseEvent(tool="bash"), OpencodeRunState())
    assert event == StatusUpdate(status="Using bash...")


def test_step_finish_accumulates_usage_silently():
    state = OpencodeRunState()
    for step in (
        StepFinishEvent(
            input_tokens=3,
            output_tokens=30,
            cache_read_tokens=0,
            cache_write_tokens=5944,
            cost_usd=0.0015334,
        ),
        StepFinishEvent(
            input_tokens=2,
            output_tokens=40,
            cache_read_tokens=5944,
            cache_write_tokens=0,
            cost_usd=0.0001736,
        ),
    ):
        assert to_bridge_event(step, state) is None
    assert state.input_tokens == 5
    assert state.output_tokens == 70
    assert state.cache_read_tokens == 5944
    assert state.cache_creation_tokens == 5944
    assert state.cost_usd == 0.0015334 + 0.0001736
    assert state.num_turns == 2
    assert state.terminal is False  # no terminal event — EOF decides


def test_stream_error_records_message_silently():
    state = OpencodeRunState()
    assert to_bridge_event(StreamErrorEvent(message="boom"), state) is None
    assert state.error_message == "boom"
    assert state.terminal is False


# --- completion_at_eof ---


def test_eof_with_exit_zero_synthesizes_success():
    state = OpencodeRunState()
    to_bridge_event(TextEvent(text="the answer"), state)
    to_bridge_event(
        StepFinishEvent(
            input_tokens=3,
            output_tokens=30,
            cache_read_tokens=7,
            cache_write_tokens=5944,
            cost_usd=0.0015334,
        ),
        state,
    )

    completion = completion_at_eof(state, return_code=0)

    assert isinstance(completion, Completion)
    assert completion.text == "the answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.0015334
    assert completion.duration_ms >= 0
    assert completion.metadata["usage"] == {
        "input_tokens": 3,
        "output_tokens": 30,
        "cache_read_tokens": 7,
        "cache_creation_tokens": 5944,
        "num_turns": 1,
        "duration_api_ms": 0,
    }


def test_eof_with_nonzero_exit_and_recorded_error_is_an_error_completion():
    state = OpencodeRunState()
    to_bridge_event(StreamErrorEvent(message="Unexpected server error."), state)

    completion = completion_at_eof(state, return_code=1)

    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "Unexpected server error."


def test_eof_recorded_error_wins_even_on_exit_zero():
    # The CLI has been seen exiting 0 mid-run; a recorded error with no
    # reply is a failure whatever the exit code says.
    state = OpencodeRunState()
    to_bridge_event(StreamErrorEvent(message="boom"), state)

    completion = completion_at_eof(state, return_code=0)

    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "boom"


def test_eof_exit_zero_without_any_reply_is_an_error():
    # A real turn always ends in a text event — an empty exit-0 stream is a
    # silent failure, not an empty answer.
    completion = completion_at_eof(OpencodeRunState(), return_code=0)
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "without a reply" in completion.text


def test_eof_with_nonzero_exit_and_nothing_recorded_defers_to_the_engine():
    # e.g. `Error: Session not found` — stderr only, exit 1, no JSON.
    assert completion_at_eof(OpencodeRunState(), return_code=1) is None

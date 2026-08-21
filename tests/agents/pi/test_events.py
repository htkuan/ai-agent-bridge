"""The pi ``--mode json`` line parser and its fold into bridge events."""

from __future__ import annotations

import json
from typing import Any

from agent_bridge.agents.pi.events import (
    AgentEndEvent,
    AssistantMessageEvent,
    PiRunState,
    SessionEvent,
    ToolStartEvent,
    TurnEndEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _assistant_line(
    texts: list[str],
    *,
    usage: dict[str, Any] | None = None,
    stop_reason: str = "stop",
) -> str:
    return _line(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": t} for t in texts],
                "usage": usage or {},
                "stopReason": stop_reason,
            },
        }
    )


# --- parse_stream_line ---


def test_parse_session_header():
    events = parse_stream_line(_line({"type": "session", "id": "abc", "cwd": "."}))
    assert events == [SessionEvent(session_id="abc")]


def test_parse_tool_execution_start():
    events = parse_stream_line(
        _line(
            {
                "type": "tool_execution_start",
                "toolName": "read",
                "args": {"path": "x.py"},
            }
        )
    )
    assert events == [ToolStartEvent(tool_name="read", args={"path": "x.py"})]


def test_parse_assistant_message_with_usage():
    events = parse_stream_line(
        _assistant_line(
            ["hello"],
            usage={
                "input": 10,
                "output": 5,
                "cacheRead": 3,
                "cacheWrite": 2,
                "cost": {"total": 0.01},
            },
        )
    )
    assert events == [
        AssistantMessageEvent(
            texts=["hello"],
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=3,
            cache_write_tokens=2,
            cost_usd=0.01,
        )
    ]


def test_parse_ignores_non_assistant_messages():
    for role in ("user", "toolResult"):
        line = _line(
            {
                "type": "message_end",
                "message": {"role": role, "content": [{"type": "text", "text": "x"}]},
            }
        )
        assert parse_stream_line(line) == []


def test_parse_turn_end_and_agent_end():
    assert parse_stream_line(_line({"type": "turn_end"})) == [TurnEndEvent()]
    assert parse_stream_line(_line({"type": "agent_end", "willRetry": True})) == [
        AgentEndEvent(will_retry=True)
    ]


def test_parse_ignores_lifecycle_and_delta_events():
    for event_type in (
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "tool_execution_end",
        "agent_settled",
    ):
        assert parse_stream_line(_line({"type": event_type})) == []


def test_parse_skips_malformed_and_blank_lines():
    assert parse_stream_line("not json") == []
    assert parse_stream_line("   ") == []


# --- to_bridge_event fold ---


def test_assistant_text_yields_delta_and_updates_state():
    state = PiRunState()
    event = to_bridge_event(
        AssistantMessageEvent(texts=["a", "b"], cost_usd=0.5), state
    )
    assert event == TextDelta(text="a\nb")
    assert state.last_text == "a\nb"
    assert state.cost_usd == 0.5


def test_tool_call_only_message_accumulates_silently():
    state = PiRunState()
    event = to_bridge_event(
        AssistantMessageEvent(texts=[], input_tokens=7, output_tokens=3), state
    )
    assert event is None
    assert state.input_tokens == 7
    assert state.output_tokens == 3
    assert state.last_text == ""


def test_usage_accumulates_across_messages():
    state = PiRunState()
    to_bridge_event(
        AssistantMessageEvent(
            texts=["one"],
            input_tokens=10,
            output_tokens=1,
            cache_read_tokens=5,
            cache_write_tokens=4,
            cost_usd=0.1,
        ),
        state,
    )
    to_bridge_event(
        AssistantMessageEvent(
            texts=["two"],
            input_tokens=20,
            output_tokens=2,
            cache_read_tokens=6,
            cache_write_tokens=1,
            cost_usd=0.2,
        ),
        state,
    )
    assert state.input_tokens == 30
    assert state.output_tokens == 3
    assert state.cache_read_tokens == 11
    assert state.cache_creation_tokens == 5
    assert state.cost_usd == 0.30000000000000004
    assert state.last_text == "two"


def test_tool_start_becomes_status_update():
    event = to_bridge_event(ToolStartEvent(tool_name="bash"), PiRunState())
    assert event == StatusUpdate(status="Using bash...")


def test_turn_end_counts_turns_silently():
    state = PiRunState()
    assert to_bridge_event(TurnEndEvent(), state) is None
    assert to_bridge_event(TurnEndEvent(), state) is None
    assert state.num_turns == 2


def test_session_event_is_internal():
    assert to_bridge_event(SessionEvent(session_id="s"), PiRunState()) is None


def test_agent_end_with_retry_keeps_reading():
    state = PiRunState()
    assert to_bridge_event(AgentEndEvent(will_retry=True), state) is None
    assert state.terminal is False


def test_agent_end_builds_completion_from_state():
    state = PiRunState()
    to_bridge_event(
        AssistantMessageEvent(
            texts=["final answer"],
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=20,
            cache_write_tokens=10,
            cost_usd=0.25,
        ),
        state,
    )
    to_bridge_event(TurnEndEvent(), state)

    completion = to_bridge_event(AgentEndEvent(), state)

    assert state.terminal is True
    assert isinstance(completion, Completion)
    assert completion.text == "final answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.25
    assert completion.duration_ms >= 0
    assert completion.metadata["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 20,
        "cache_creation_tokens": 10,
        "num_turns": 1,
        "duration_api_ms": 0,
    }

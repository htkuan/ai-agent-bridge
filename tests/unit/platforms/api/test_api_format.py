from __future__ import annotations

import json

from agent_bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    Usage,
    UserQuestion,
)
from agent_bridge.platforms.api.adapter import (
    completion_body,
    format_sse,
    is_capacity_full,
    status_line,
    usage_to_dict,
)

# --- usage_to_dict ---


def test_usage_to_dict_none():
    assert usage_to_dict(None) is None


def test_usage_to_dict_fields_and_total():
    usage = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_creation_tokens=40,
        num_turns=2,
        duration_api_ms=500,
        duration_ms=700,
        cost_usd=0.05,
    )
    data = usage_to_dict(usage)
    assert data == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_creation_tokens": 40,
        "num_turns": 2,
        "duration_api_ms": 500,
        "duration_ms": 700,
        "cost_usd": 0.05,
        "total_tokens": 100,
    }


# --- completion_body (buffered response) ---


def test_completion_text_is_authoritative():
    body = completion_body(
        "job-1",
        Completion(text="final answer"),
        accumulated="partial deltas",
        status_updates=["Running tool"],
    )
    assert body == {
        "session": "job-1",
        "text": "final answer",
        "is_error": False,
        "usage": None,
        "session_usage": None,
        "status_updates": ["Running tool"],
    }


def test_accumulated_deltas_are_fallback_when_completion_text_empty():
    body = completion_body("job-1", Completion(text=""), accumulated="from deltas")
    assert body["text"] == "from deltas"


def test_oneshot_body_has_null_session():
    body = completion_body(None, Completion(text="ok"))
    assert body["session"] is None
    assert body["status_updates"] == []


def test_questions_included_only_when_present():
    plain = completion_body(None, Completion(text="ok"))
    assert "questions" not in plain
    asked = completion_body(
        None, Completion(text=""), questions=[{"question": "Which one?"}]
    )
    assert asked["questions"] == [{"question": "Which one?"}]


def test_usage_serialized_into_body():
    completion = Completion(text="ok")
    completion.usage = Usage(input_tokens=5, output_tokens=7)
    completion.session_usage = Usage(input_tokens=50, output_tokens=70)
    body = completion_body(None, completion)
    assert body["usage"]["total_tokens"] == 12
    assert body["session_usage"]["total_tokens"] == 120


def test_is_capacity_full_reads_metadata_error_code():
    assert is_capacity_full(
        Completion(text="busy", is_error=True, metadata={"error_code": "capacity_full"})
    )
    assert not is_capacity_full(Completion(text="ok"))
    assert not is_capacity_full(
        Completion(text="boom", is_error=True, metadata={"error_code": "other"})
    )


def test_status_line_with_and_without_detail():
    assert status_line(StatusUpdate(status="Running Bash")) == "Running Bash"
    assert (
        status_line(StatusUpdate(status="Running Bash", detail="ls -la"))
        == "Running Bash: ls -la"
    )


# --- SSE formatting ---


def _parse_sse(raw: str) -> tuple[str, dict]:
    lines = raw.split("\n")
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    assert raw.endswith("\n\n")
    return lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])


def test_sse_processing():
    name, data = _parse_sse(format_sse(Processing()))
    assert name == "processing"
    assert data == {}


def test_sse_text_delta():
    name, data = _parse_sse(format_sse(TextDelta(text="chunk one")))
    assert name == "text_delta"
    assert data == {"text": "chunk one"}


def test_sse_status():
    name, data = _parse_sse(format_sse(StatusUpdate(status="Bash", detail="ls")))
    assert name == "status"
    assert data == {"status": "Bash", "detail": "ls"}


def test_sse_question():
    questions = [{"question": "Pick one", "options": ["a", "b"]}]
    name, data = _parse_sse(format_sse(UserQuestion(questions=questions)))
    assert name == "question"
    assert data == {"questions": questions}


def test_sse_completion_includes_usage():
    completion = Completion(text="done", is_error=False)
    completion.usage = Usage(input_tokens=1, output_tokens=2)
    name, data = _parse_sse(format_sse(completion))
    assert name == "completion"
    assert data["text"] == "done"
    assert data["is_error"] is False
    assert data["usage"]["total_tokens"] == 3
    assert data["session_usage"] is None


def test_sse_multiline_text_stays_on_one_data_line():
    # json.dumps escapes newlines, so the SSE framing (blank-line delimited)
    # cannot be broken by agent output.
    raw = format_sse(TextDelta(text="line1\nline2"))
    assert raw.count("\n") == 3  # event line + data line + blank line
    _name, data = _parse_sse(raw)
    assert data["text"] == "line1\nline2"

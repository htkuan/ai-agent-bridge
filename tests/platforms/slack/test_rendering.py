"""Event rendering: placeholder lifecycle, throttling, and completion paths.

Time-sensitive behaviour is exercised by swapping the adapter module's
``time`` / ``asyncio`` references for scripted stand-ins (the agreed
convention — no clock seam in src).
"""

from __future__ import annotations

import pytest

import agent_bridge.platforms.slack.adapter as slack_adapter
from agent_bridge.bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    Usage,
    UserQuestion,
)
from agent_bridge.platforms.slack.adapter import _RenderState
from agent_bridge.platforms.slack.config import SlackConfig
from tests.fakes import FakeBridge
from tests.platforms.slack.harness import build_harness

QUEUED_REJECTION = (
    ":x: Your queued message could not be processed — please try again shortly."
)
CAPACITY_REJECTION = (
    ":no_entry: Too many requests being processed, please try again later."
)


class _ScriptClock:
    """time.monotonic stand-in replaying scripted values (last one repeats)."""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def monotonic(self) -> float:
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _StepClock:
    """time.monotonic stand-in that jumps past the throttle window each call."""

    def __init__(self) -> None:
        self._now = 100.0

    def monotonic(self) -> float:
        self._now += 10.0
        return self._now


class _SleepRecorder:
    """asyncio stand-in capturing sleep() durations instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def _render_state() -> _RenderState:
    return _RenderState(
        channel="C1",
        thread_ts="1.0",
        session_key="slack:C1:1.0",
        say=None,
        existing_message_ts="5.0",
        message_ts="5.0",
    )


# --- placeholder lifecycle ---


async def test_processing_resets_existing_placeholder(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(slack_adapter, "time", _StepClock())
    harness = build_harness(events=[Processing(), Completion(text="hi")])

    await harness.adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "prompt", {}, say=None, existing_message_ts="5.0"
    )

    texts = [c.kwargs["text"] for c in harness.client.calls_to("chat_update")]
    assert texts[0] == ":hourglass_flowing_sand: Processing..."
    assert texts[-1] == "hi"


async def test_deltas_and_status_accumulate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(slack_adapter, "time", _StepClock())
    harness = build_harness(
        events=[
            Processing(),
            TextDelta(text="a"),
            StatusUpdate(status="Running tests"),
            TextDelta(text="b"),
            Completion(text="ignored"),
        ]
    )

    await harness.adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "prompt", {}, say=None, existing_message_ts="5.0"
    )

    texts = [c.kwargs["text"] for c in harness.client.calls_to("chat_update")]
    assert texts == [
        ":hourglass_flowing_sand: Processing...",
        "a",
        "a\n\n_Running tests_",
        "a\n\nb\n\n_Running tests_",
        # Accumulated stream text wins over the Completion payload; the
        # residual tool status is dropped from the final message.
        "a\n\nb",
    ]


async def test_status_without_text_renders_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(slack_adapter, "time", _StepClock())
    harness = build_harness()
    st = _render_state()

    await harness.adapter._render_status(st, "Reading files")

    assert harness.client.messages[("C1", "5.0")] == "\n\n_Reading files_"


# --- throttling ---


async def test_throttle_skips_updates_inside_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(slack_adapter, "time", _ScriptClock(100.0, 100.5, 102.5))
    harness = build_harness()
    st = _render_state()

    await harness.adapter._render_text_delta(st, "one")  # 100.0 → update
    await harness.adapter._render_text_delta(st, "two")  # 100.5 → in window
    await harness.adapter._render_text_delta(st, "three")  # 102.5 → update

    texts = [c.kwargs["text"] for c in harness.client.calls_to("chat_update")]
    assert texts == ["one", "one\n\ntwo\n\nthree"]


async def test_completion_waits_out_throttle_window(monkeypatch: pytest.MonkeyPatch):
    sleeper = _SleepRecorder()
    monkeypatch.setattr(slack_adapter, "time", _ScriptClock(100.5))
    monkeypatch.setattr(slack_adapter, "asyncio", sleeper)
    harness = build_harness()
    st = _render_state()
    st.last_update_time = 100.0

    await harness.adapter._render_completion(st, Completion(text="done"))

    assert sleeper.calls == [pytest.approx(1.0)]
    assert harness.client.messages[("C1", "5.0")] == "done"


async def test_completion_waits_out_the_configured_window(
    monkeypatch: pytest.MonkeyPatch,
):
    """Same as above but with a non-default throttle — pins that the wait comes
    from the config field, not the constant it was extracted from."""
    sleeper = _SleepRecorder()
    monkeypatch.setattr(slack_adapter, "time", _ScriptClock(100.5))
    monkeypatch.setattr(slack_adapter, "asyncio", sleeper)
    harness = build_harness(
        config=SlackConfig(
            bot_token="xoxb-x", app_token="xapp-x", update_throttle_seconds=3.0
        )
    )
    st = _render_state()
    st.last_update_time = 100.0

    await harness.adapter._render_completion(st, Completion(text="done"))

    assert sleeper.calls == [pytest.approx(2.5)]


# --- completion variants ---


async def test_capacity_rejection_of_queued_message():
    harness = build_harness()
    st = _render_state()

    await harness.adapter._render_completion(
        st,
        Completion(
            text="Too many requests being processed, please try again later.",
            is_error=True,
            metadata={"error_code": "capacity_full"},
        ),
    )

    assert harness.client.messages[("C1", "5.0")] == QUEUED_REJECTION


async def test_agent_error_shows_the_reported_reason():
    """A timeout (or any non-capacity failure) must not be dressed up as a
    capacity rejection — regression guard for the misleading blanket notice."""
    harness = build_harness()
    st = _render_state()

    await harness.adapter._render_completion(
        st,
        Completion(text="Claude process timed out after 20.0s", is_error=True),
    )

    assert (
        harness.client.messages[("C1", "5.0")]
        == ":warning: Claude process timed out after 20.0s"
    )


async def test_agent_error_keeps_partial_output_below_the_reason():
    harness = build_harness()
    st = _render_state()
    st.accumulated_text = "got this far"

    await harness.adapter._render_completion(
        st,
        Completion(text="Claude process timed out after 20.0s", is_error=True),
    )

    assert harness.client.messages[("C1", "5.0")] == (
        ":warning: Claude process timed out after 20.0s\n\ngot this far"
    )


async def test_agent_error_without_text_falls_back_to_a_generic_notice():
    harness = build_harness()
    st = _render_state()

    await harness.adapter._render_completion(st, Completion(text="", is_error=True))

    assert harness.client.messages[("C1", "5.0")] == slack_adapter.UNKNOWN_ERROR


async def test_error_completion_omits_the_usage_footer():
    harness = build_harness(
        config=SlackConfig(
            bot_token="xoxb-x", app_token="xapp-x", usage_report_enabled=True
        )
    )
    st = _render_state()

    await harness.adapter._render_completion(
        st,
        Completion(
            text="Claude process timed out after 20.0s",
            is_error=True,
            usage=Usage(input_tokens=10, output_tokens=20),
        ),
    )

    assert (
        harness.client.messages[("C1", "5.0")]
        == ":warning: Claude process timed out after 20.0s"
    )


async def test_capacity_rejection_posted_via_say():
    harness = build_harness(capacity_full=True)

    status = await harness.adapter._stream_response(
        "C123",
        "1.0",
        "slack:C123:1.0",
        "prompt",
        {},
        say=harness.client.say_for("C123"),
    )

    assert status is None
    assert list(harness.client.messages.values()) == [CAPACITY_REJECTION]


async def test_empty_completion_posts_fallback_text():
    harness = build_harness(events=[Processing(), Completion(text="")])

    await harness.adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "prompt", {}, say=None, existing_message_ts="5.0"
    )

    assert harness.client.messages[("C1", "5.0")] == "_No response from agent._"


# --- questions ---


async def test_questions_rewrite_placeholder():
    questions = [{"question": "Which env?", "options": ["dev", "prod"]}]
    harness = build_harness(
        events=[
            Processing(),
            UserQuestion(questions=questions),
            Completion(text=""),
        ]
    )

    status = await harness.adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "prompt", {}, say=None, existing_message_ts="5.0"
    )

    assert status == "waiting_for_answer"
    posted = harness.client.messages[("C1", "5.0")]
    assert "Claude needs your input" in posted
    assert "Which env?" in posted


async def test_questions_posted_via_say_when_no_placeholder():
    questions = [{"question": "Proceed?"}]
    harness = build_harness(
        events=[UserQuestion(questions=questions), Completion(text="")]
    )

    status = await harness.adapter._stream_response(
        "C123",
        "1.0",
        "slack:C123:1.0",
        "prompt",
        {},
        say=harness.client.say_for("C123"),
    )

    assert status == "waiting_for_answer"
    posted = "\n".join(harness.client.messages.values())
    assert "Claude needs your input" in posted


# --- incomplete streams ---


async def test_incomplete_stream_strips_tool_status(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(slack_adapter, "time", _StepClock())
    harness = build_harness(
        events=[Processing(), TextDelta(text="partial"), StatusUpdate(status="Running")]
    )

    status = await harness.adapter._stream_response(
        "C1", "1.0", "slack:C1:1.0", "prompt", {}, say=None, existing_message_ts="5.0"
    )

    assert status is None
    assert harness.client.messages[("C1", "5.0")] == "partial"


# --- turns that raise instead of completing ---


async def test_raised_turn_replaces_the_placeholder():
    harness = build_harness()
    harness.adapter._bridge = FakeBridge([Processing()], raises=True)

    with pytest.raises(RuntimeError, match="exploded"):
        await harness.adapter._stream_response(
            "C1",
            "1.0",
            "slack:C1:1.0",
            "prompt",
            {},
            say=None,
            existing_message_ts="5.0",
        )

    # A terminal notice, not the Processing placeholder left up forever —
    # and the error still reaches the caller's error envelope.
    assert harness.client.messages[("C1", "5.0")] == (
        ":warning: Something went wrong — please try again."
    )


async def test_raised_turn_reporting_never_masks_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = build_harness()
    harness.adapter._bridge = FakeBridge([Processing()], raises=True)

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("slack is down")

    monkeypatch.setattr(harness.adapter, "_post_final", explode)

    with pytest.raises(RuntimeError, match="exploded"):
        await harness.adapter._stream_response(
            "C1",
            "1.0",
            "slack:C1:1.0",
            "prompt",
            {},
            say=None,
            existing_message_ts="5.0",
        )

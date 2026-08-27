"""HeartbeatAdapter: state file, the fire flow, scheduling, and log output."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from tests.platforms.heartbeat.harness import heartbeat_harness

# --- State file I/O ---


async def test_read_last_run_returns_none_when_missing(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        assert h.adapter._read_last_run() is None


async def test_state_file_round_trip(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        when = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        h.adapter._write_last_run(when)
        assert h.config.state_path.exists()
        assert h.adapter._read_last_run() == when


async def test_read_last_run_returns_none_on_corrupt_file(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        h.config.state_path.write_text("{not json")
        assert h.adapter._read_last_run() is None


# --- Fire flow ---


async def test_fire_once_calls_bridge_with_prompt_and_writes_state(tmp_path: Path):
    async with heartbeat_harness(tmp_path, prompt="check tasks") as h:
        await h.deliver()

        (call,) = h.requests()
        assert call.text == "check tasks"
        assert call.session_key.startswith("heartbeat:tick:")
        assert call.context is not None
        assert call.context["source"] == "heartbeat"
        assert "fired_at" in call.context
        assert h.config.state_path.exists()


async def test_fire_once_marks_session_non_resumable(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        await h.deliver()
        # Heartbeat ticks are one-shot — the same key must never resume.
        assert h.requests()[0].resumable is False


async def test_fire_once_routes_to_configured_agent(tmp_path: Path):
    async with heartbeat_harness(tmp_path, agent="night-shift") as h:
        await h.deliver()
        assert h.requests()[0].agent == "night-shift"


async def test_fire_once_defaults_to_no_agent(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        await h.deliver()
        assert h.requests()[0].agent is None


async def test_fire_once_passes_heartbeat_flavored_system_prompt(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        await h.deliver()

        call = h.requests()[0]
        assert call.system_prompt is not None
        assert call.context is not None
        # The adapter — not the agent — owns this phrasing. Two things must be
        # present: the mechanism name and the fire time.
        assert "heartbeat" in call.system_prompt.lower()
        assert call.context["fired_at"] in call.system_prompt


async def test_fire_once_writes_state_even_on_bridge_error(tmp_path: Path):
    async with heartbeat_harness(tmp_path, events=[], raises=True) as h:
        # Must not raise — the error is caught and logged.
        await h.deliver()
        assert h.config.state_path.exists()


async def test_each_tick_uses_unique_session_key(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        await h.deliver()
        # isoformat() includes microseconds, but force a clear gap to be safe
        await asyncio.sleep(0.005)
        await h.deliver()

        keys = {call.session_key for call in h.requests()}
        assert len(keys) == 2


# --- Loop / restart catch-up ---


async def test_loop_fires_immediately_when_state_missing(tmp_path: Path):
    async with heartbeat_harness(tmp_path, interval_minutes=60) as h:
        await h.adapter.start()
        await asyncio.sleep(0.05)
        await h.adapter.stop()
        assert len(h.requests()) >= 1


async def test_loop_skips_initial_fire_when_state_recent(tmp_path: Path):
    async with heartbeat_harness(tmp_path, interval_minutes=60) as h:
        h.config.state_path.write_text(
            json.dumps({"last_run": datetime.now(UTC).isoformat()})
        )
        await h.adapter.start()
        await asyncio.sleep(0.05)
        await h.adapter.stop()
        assert h.requests() == []


async def test_loop_fires_immediately_when_state_stale(tmp_path: Path):
    async with heartbeat_harness(tmp_path, interval_minutes=1) as h:
        stale = datetime.now(UTC) - timedelta(minutes=10)
        h.config.state_path.write_text(json.dumps({"last_run": stale.isoformat()}))
        await h.adapter.start()
        await asyncio.sleep(0.05)
        await h.adapter.stop()
        assert len(h.requests()) >= 1


async def test_stop_during_sleep_returns_promptly(tmp_path: Path):
    async with heartbeat_harness(tmp_path, interval_minutes=60) as h:
        # Recent state → the loop will sleep ~60min before the next fire.
        h.config.state_path.write_text(
            json.dumps({"last_run": datetime.now(UTC).isoformat()})
        )
        await h.adapter.start()

        # Stop must not block on the 60min sleep.
        await asyncio.wait_for(h.adapter.stop(), timeout=1.0)


# --- Rendering spec: what each event becomes in the log ---


async def _render(tmp_path: Path, event: BridgeEvent) -> list[logging.LogRecord]:
    """Drive one turn carrying just ``event`` and return what it logged."""
    async with heartbeat_harness(tmp_path, events=[event]) as h:
        await h.deliver()
        return list(h.recorder.records)


async def test_renders_processing_at_info(tmp_path: Path):
    records = await _render(tmp_path, Processing())
    assert any(
        r.levelno == logging.INFO and "processing" in r.getMessage() for r in records
    )


async def test_renders_status_update_at_info(tmp_path: Path):
    records = await _render(tmp_path, StatusUpdate(status="thinking", detail="d"))
    assert any(
        r.levelno == logging.INFO and "thinking" in r.getMessage() for r in records
    )


async def test_renders_text_delta_at_debug_only(tmp_path: Path):
    records = await _render(tmp_path, TextDelta(text="hello"))
    deltas = [r for r in records if "text +" in r.getMessage()]
    assert deltas
    # An operator watching at INFO must not be drowned in token-level noise.
    assert all(r.levelno == logging.DEBUG for r in deltas)


async def test_renders_user_question_as_a_warning(tmp_path: Path):
    records = await _render(tmp_path, UserQuestion(questions=[{"question": "ok?"}]))
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert any("no human can answer" in r.getMessage() for r in warnings)


async def test_renders_error_completion_at_error(tmp_path: Path):
    records = await _render(tmp_path, Completion(text="oops", is_error=True))
    assert any(r.levelno == logging.ERROR for r in records)


async def test_renders_successful_completion_with_the_final_reply(tmp_path: Path):
    records = await _render(
        tmp_path, Completion(text="all done", cost_usd=0.01, duration_ms=42)
    )
    assert any("all done" in r.getMessage() for r in records)


async def test_renders_a_whole_turn_without_losing_events(tmp_path: Path):
    async with heartbeat_harness(tmp_path) as h:
        await h.deliver()
        # The tick itself is announced before anything the agent sends back.
        assert any("Heartbeat tick" in line for line in h.output())

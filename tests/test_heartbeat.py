from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig


# --- Stubs ---


class _StubBridge:
    def __init__(self, events: list[BridgeEvent] | None = None) -> None:
        self.calls: list[dict] = []
        self._events: list[BridgeEvent] = (
            events if events is not None else [Completion(text="ok")]
        )

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(
            {
                "session_key": session_key,
                "text": text,
                "context": context,
                "system_prompt": system_prompt,
                "resumable": resumable,
            }
        )
        for event in self._events:
            yield event


class _BoomBridge:
    async def handle_message(
        self, *args, **kwargs
    ) -> AsyncIterator[BridgeEvent]:
        raise RuntimeError("boom")
        yield  # noqa: unreachable, makes this an async generator


# --- Fixtures ---


@pytest.fixture()
def make_adapter(tmp_path: Path):
    def _make(
        interval_minutes: int = 60,
        prompt: str = "ping",
        events: list[BridgeEvent] | None = None,
    ) -> tuple[HeartbeatAdapter, _StubBridge, HeartbeatConfig]:
        config = HeartbeatConfig(
            enabled=True,
            interval_minutes=interval_minutes,
            prompt=prompt,
            state_path=tmp_path / "heartbeat.json",
        )
        bridge = _StubBridge(events=events)
        adapter = HeartbeatAdapter(config, bridge)  # type: ignore[arg-type]
        return adapter, bridge, config

    return _make


# --- Config validation ---


def test_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", raising=False)
    config = HeartbeatConfig.from_env()
    assert not config.enabled


def test_config_enabled_requires_prompt(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", "60")
    monkeypatch.delenv("AGENT_BRIDGE_HEARTBEAT_PROMPT", raising=False)
    with pytest.raises(ValueError, match="PROMPT"):
        HeartbeatConfig.from_env()


def test_config_enabled_requires_positive_interval(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_PROMPT", "go")
    with pytest.raises(ValueError, match="INTERVAL_MINUTES"):
        HeartbeatConfig.from_env()


def test_config_valid(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", "15")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_PROMPT", "go")
    monkeypatch.setenv("AGENT_BRIDGE_HEARTBEAT_STATE_PATH", str(tmp_path / "h.json"))
    config = HeartbeatConfig.from_env()
    assert config.enabled
    assert config.interval_minutes == 15
    assert config.prompt == "go"
    assert config.state_path == tmp_path / "h.json"


# --- State file I/O ---


def test_read_last_run_returns_none_when_missing(make_adapter):
    adapter, _, _ = make_adapter()
    assert adapter._read_last_run() is None


def test_state_file_round_trip(make_adapter):
    adapter, _, config = make_adapter()
    when = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    adapter._write_last_run(when)
    assert config.state_path.exists()
    assert adapter._read_last_run() == when


def test_read_last_run_returns_none_on_corrupt_file(make_adapter):
    adapter, _, config = make_adapter()
    config.state_path.write_text("{not json")
    assert adapter._read_last_run() is None


# --- Fire flow ---


async def test_fire_once_calls_bridge_with_prompt_and_writes_state(make_adapter):
    adapter, bridge, config = make_adapter(prompt="check tasks")
    await adapter._fire_once()

    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    assert call["text"] == "check tasks"
    assert call["session_key"].startswith("heartbeat:tick:")
    assert call["context"]["source"] == "heartbeat"
    assert "fired_at" in call["context"]
    assert config.state_path.exists()


async def test_fire_once_marks_session_non_resumable(make_adapter):
    adapter, bridge, _ = make_adapter()
    await adapter._fire_once()

    # Heartbeat ticks are one-shot — same key must never resume the same session
    assert bridge.calls[0]["resumable"] is False


async def test_fire_once_passes_heartbeat_flavored_system_prompt(make_adapter):
    adapter, bridge, _ = make_adapter()
    await adapter._fire_once()

    sp = bridge.calls[0]["system_prompt"]
    assert sp is not None
    # Adapter — not the agent — owns this phrasing. Two things must be present:
    # the mechanism name and the fire time.
    assert "heartbeat" in sp.lower()
    assert bridge.calls[0]["context"]["fired_at"] in sp


async def test_fire_once_writes_state_even_on_bridge_error(tmp_path: Path):
    config = HeartbeatConfig(
        enabled=True,
        interval_minutes=60,
        prompt="x",
        state_path=tmp_path / "h.json",
    )
    adapter = HeartbeatAdapter(config, _BoomBridge())  # type: ignore[arg-type]

    # Should not raise — error is caught and logged
    await adapter._fire_once()
    assert config.state_path.exists()


async def test_each_tick_uses_unique_session_key(make_adapter):
    adapter, bridge, _ = make_adapter()
    await adapter._fire_once()
    # isoformat() includes microseconds, but force a clear gap to be safe
    await asyncio.sleep(0.005)
    await adapter._fire_once()

    keys = [c["session_key"] for c in bridge.calls]
    assert len(set(keys)) == 2


# --- Loop / restart catch-up ---


async def test_loop_fires_immediately_when_state_missing(make_adapter):
    adapter, bridge, _ = make_adapter(interval_minutes=60)
    await adapter.start()
    await asyncio.sleep(0.05)
    await adapter.stop()
    assert len(bridge.calls) >= 1


async def test_loop_skips_initial_fire_when_state_recent(make_adapter):
    adapter, bridge, config = make_adapter(interval_minutes=60)
    config.state_path.write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()})
    )
    await adapter.start()
    await asyncio.sleep(0.05)
    await adapter.stop()
    assert bridge.calls == []


async def test_loop_fires_immediately_when_state_stale(make_adapter):
    adapter, bridge, config = make_adapter(interval_minutes=1)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    config.state_path.write_text(json.dumps({"last_run": stale.isoformat()}))
    await adapter.start()
    await asyncio.sleep(0.05)
    await adapter.stop()
    assert len(bridge.calls) >= 1


async def test_stop_during_sleep_returns_promptly(make_adapter):
    adapter, _, config = make_adapter(interval_minutes=60)
    # Recent state → loop will sleep ~60min before next fire
    config.state_path.write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()})
    )
    await adapter.start()

    # Stop must not block on the 60min sleep.
    await asyncio.wait_for(adapter.stop(), timeout=1.0)


# --- Event log dispatch ---


def test_log_event_processing(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    caplog.set_level("INFO", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event("k", Processing())
    assert any("processing" in r.message for r in caplog.records)


def test_log_event_status_update(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    caplog.set_level("INFO", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event("k", StatusUpdate(status="thinking", detail="d"))
    assert any("thinking" in r.message for r in caplog.records)


def test_log_event_text_delta_is_debug_only(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    # INFO threshold → DEBUG-level TextDelta should NOT appear
    caplog.set_level("INFO", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event("k", TextDelta(text="hello"))
    assert not any("text +" in r.message for r in caplog.records)


def test_log_event_user_question_is_warning(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    caplog.set_level("WARNING", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event(
        "k", UserQuestion(questions=[{"question": "ok?"}])
    )
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert any("no human can answer" in r.message for r in caplog.records)


def test_log_event_completion_error_is_error_level(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    caplog.set_level("ERROR", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event("k", Completion(text="oops", is_error=True))
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_log_event_completion_success_logs_final_reply(make_adapter, caplog):
    adapter, _, _ = make_adapter()
    caplog.set_level("INFO", logger="agent_bridge.platforms.heartbeat.adapter")
    adapter._log_event("k", Completion(text="all done", cost_usd=0.01, duration_ms=42))
    assert any("all done" in r.message for r in caplog.records)

"""Per-session state bookkeeping: _get_state and cleanup_stale_sessions."""

from __future__ import annotations

from pathlib import Path

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.slack.adapter import _PendingMessage
from tests.platforms.slack.harness import build_harness


def _pending() -> _PendingMessage:
    return _PendingMessage(
        text="queued", context={}, message_ts="1.1", channel="C1", thread_ts="1.0"
    )


def test_get_state_reuses_existing_entry():
    adapter = build_harness().adapter
    assert adapter._get_state("slack:C1:1.0") is adapter._get_state("slack:C1:1.0")


def test_cleanup_without_session_manager_is_noop():
    adapter = build_harness().adapter
    adapter._get_state("slack:C1:1.0")
    assert adapter.cleanup_stale_sessions() == 0
    assert "slack:C1:1.0" in adapter._sessions


async def test_cleanup_removes_only_idle_expired_sessions(tmp_path: Path):
    manager = SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=1.0)
    )
    adapter = build_harness(session_manager=manager).adapter

    manager.get_or_create("slack:live:1.0")
    adapter._get_state("slack:live:1.0")

    adapter._get_state("slack:stale:1.0")

    adapter._get_state("slack:processing:1.0").processing = True
    adapter._get_state("slack:waiting:1.0").waiting_for_answer = True
    adapter._get_state("slack:pending:1.0").pending = _pending()
    locked = adapter._get_state("slack:locked:1.0")

    async with locked.lock:
        assert adapter.cleanup_stale_sessions() == 1

    assert "slack:stale:1.0" not in adapter._sessions
    assert set(adapter._sessions) == {
        "slack:live:1.0",
        "slack:processing:1.0",
        "slack:waiting:1.0",
        "slack:pending:1.0",
        "slack:locked:1.0",
    }

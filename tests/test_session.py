import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_bridge.session import SessionManager


def test_get_or_create_new(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    session_id, is_new = mgr.get_or_create("slack:C123:ts1")
    assert is_new is True
    assert len(session_id) == 36  # UUID format


def test_get_or_create_existing(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    sid1, new1 = mgr.get_or_create("slack:C123:ts1")
    sid2, new2 = mgr.get_or_create("slack:C123:ts1")

    assert new1 is True
    assert new2 is False
    assert sid1 == sid2


def test_different_keys_different_sessions(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    sid1, _ = mgr.get_or_create("slack:C123:ts1")
    sid2, _ = mgr.get_or_create("slack:C123:ts2")

    assert sid1 != sid2


def test_persistence(tmp_path: Path):
    store = tmp_path / "sessions.json"

    mgr1 = SessionManager(store)
    sid1, _ = mgr1.get_or_create("slack:C123:ts1")

    # New manager loads from file
    mgr2 = SessionManager(store)
    sid2, is_new = mgr2.get_or_create("slack:C123:ts1")

    assert sid1 == sid2
    assert is_new is False


def test_get_nonexistent(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    assert mgr.get("nonexistent") is None


def test_get_existing(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    sid, _ = mgr.get_or_create("slack:C123:ts1")
    assert mgr.get("slack:C123:ts1") == sid


def test_delete(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    mgr.get_or_create("slack:C123:ts1")
    assert mgr.delete("slack:C123:ts1") is True
    assert mgr.get("slack:C123:ts1") is None
    assert mgr.delete("slack:C123:ts1") is False


def test_list_sessions(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    mgr.get_or_create("slack:C1:ts1")
    mgr.get_or_create("slack:C2:ts2")

    sessions = mgr.list_sessions()
    assert len(sessions) == 2
    assert "slack:C1:ts1" in sessions
    assert "slack:C2:ts2" in sessions


def test_store_file_format(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store)

    mgr.get_or_create("slack:C123:ts1")

    data = json.loads(store.read_text())
    entry = data["slack:C123:ts1"]
    assert "session_id" in entry
    assert "created_at" in entry
    assert "last_used" in entry


# --- TTL tests ---


def test_expired_session_treated_as_new(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=1.0)

    sid1, _ = mgr.get_or_create("slack:C123:ts1")

    # Simulate time passing beyond TTL
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    mgr._sessions["slack:C123:ts1"]["last_used"] = past.isoformat()
    mgr._save()

    sid2, is_new = mgr.get_or_create("slack:C123:ts1")
    assert is_new is True
    assert sid1 != sid2


def test_get_returns_none_for_expired(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=1.0)

    mgr.get_or_create("slack:C123:ts1")

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    mgr._sessions["slack:C123:ts1"]["last_used"] = past.isoformat()

    assert mgr.get("slack:C123:ts1") is None


def test_list_sessions_excludes_expired(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=1.0)

    mgr.get_or_create("slack:C1:ts1")
    mgr.get_or_create("slack:C2:ts2")

    # Expire only one session
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    mgr._sessions["slack:C1:ts1"]["last_used"] = past.isoformat()

    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert "slack:C2:ts2" in sessions


def test_purge_expired(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=1.0)

    mgr.get_or_create("slack:C1:ts1")
    mgr.get_or_create("slack:C2:ts2")

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    mgr._sessions["slack:C1:ts1"]["last_used"] = past.isoformat()
    mgr._save()

    purged = mgr.purge_expired()
    assert purged == 1

    # Verify it's gone from the store file too
    data = json.loads(store.read_text())
    assert "slack:C1:ts1" not in data
    assert "slack:C2:ts2" in data


def test_purge_on_load(tmp_path: Path):
    store = tmp_path / "sessions.json"
    past = datetime.now(timezone.utc) - timedelta(hours=100)
    store.write_text(json.dumps({
        "slack:old:ts1": {
            "session_id": "old-uuid",
            "created_at": past.isoformat(),
            "last_used": past.isoformat(),
        }
    }))

    mgr = SessionManager(store, ttl_hours=72.0)
    # Expired session should have been purged on load
    assert mgr.get("slack:old:ts1") is None


def test_active_session_not_expired(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=72.0)

    sid, _ = mgr.get_or_create("slack:C123:ts1")

    # Just created, should be active
    assert mgr.get("slack:C123:ts1") == sid
    assert mgr.purge_expired() == 0


def test_ttl_resets_on_use(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = SessionManager(store, ttl_hours=2.0)

    mgr.get_or_create("slack:C123:ts1")

    # Set last_used to 1.5 hours ago (within TTL)
    almost_expired = datetime.now(timezone.utc) - timedelta(hours=1, minutes=30)
    mgr._sessions["slack:C123:ts1"]["last_used"] = almost_expired.isoformat()

    # Accessing it should refresh last_used
    sid, is_new = mgr.get_or_create("slack:C123:ts1")
    assert is_new is False

    # Now it should be fresh again
    assert mgr.purge_expired() == 0

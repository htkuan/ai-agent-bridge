import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.protocols import SessionStoreError
from agent_bridge.bridge.session import SessionManager


def _mgr(tmp_path: Path, ttl_hours: float = 72.0) -> SessionManager:
    return SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=ttl_hours)
    )


def _age(store: Path, key: str, *, hours: float) -> None:
    """Backdate an entry's last_used directly in the store file — the JSON
    store is stateless, so the manager sees the edit on its next read."""
    data = json.loads(store.read_text())
    past = datetime.now(UTC) - timedelta(hours=hours)
    data[key]["last_used"] = past.isoformat()
    store.write_text(json.dumps(data))


async def test_get_or_create_new(tmp_path: Path):
    mgr = _mgr(tmp_path)
    session_id, is_new = await mgr.get_or_create("slack:C123:ts1")
    assert is_new is True
    assert len(session_id) == 36  # UUID format


async def test_get_or_create_existing(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, new1 = await mgr.get_or_create("slack:C123:ts1")
    sid2, new2 = await mgr.get_or_create("slack:C123:ts1")
    assert new1 is True
    assert new2 is False
    assert sid1 == sid2


async def test_different_keys_different_sessions(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, _ = await mgr.get_or_create("slack:C123:ts1")
    sid2, _ = await mgr.get_or_create("slack:C123:ts2")
    assert sid1 != sid2


async def test_persistence(tmp_path: Path):
    mgr1 = _mgr(tmp_path)
    sid1, _ = await mgr1.get_or_create("slack:C123:ts1")

    # New manager reads the same file
    mgr2 = _mgr(tmp_path)
    sid2, is_new = await mgr2.get_or_create("slack:C123:ts1")

    assert sid1 == sid2
    assert is_new is False


async def test_get_nonexistent(tmp_path: Path):
    assert await _mgr(tmp_path).get("nonexistent") is None


async def test_get_existing(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid, _ = await mgr.get_or_create("slack:C123:ts1")
    assert await mgr.get("slack:C123:ts1") == sid


async def test_delete(tmp_path: Path):
    mgr = _mgr(tmp_path)
    await mgr.get_or_create("slack:C123:ts1")
    assert await mgr.delete("slack:C123:ts1") is True
    assert await mgr.get("slack:C123:ts1") is None
    assert await mgr.delete("slack:C123:ts1") is False


async def test_list_sessions(tmp_path: Path):
    mgr = _mgr(tmp_path)
    await mgr.get_or_create("slack:C1:ts1")
    await mgr.get_or_create("slack:C2:ts2")

    sessions = await mgr.list_sessions()
    assert len(sessions) == 2
    assert "slack:C1:ts1" in sessions
    assert "slack:C2:ts2" in sessions


async def test_store_file_format(tmp_path: Path):
    mgr = _mgr(tmp_path)
    await mgr.get_or_create("slack:C123:ts1")

    data = json.loads((tmp_path / "sessions.json").read_text())
    entry = data["slack:C123:ts1"]
    assert "session_id" in entry
    assert "created_at" in entry
    assert "last_used" in entry


# --- TTL tests ---


async def test_expired_session_treated_as_new(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=1.0)
    sid1, _ = await mgr.get_or_create("slack:C123:ts1")
    _age(tmp_path / "sessions.json", "slack:C123:ts1", hours=2)

    sid2, is_new = await mgr.get_or_create("slack:C123:ts1")
    assert is_new is True
    assert sid1 != sid2


async def test_get_returns_none_for_expired(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=1.0)
    await mgr.get_or_create("slack:C123:ts1")
    _age(tmp_path / "sessions.json", "slack:C123:ts1", hours=2)

    assert await mgr.get("slack:C123:ts1") is None


async def test_list_sessions_excludes_expired(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=1.0)
    await mgr.get_or_create("slack:C1:ts1")
    await mgr.get_or_create("slack:C2:ts2")
    _age(tmp_path / "sessions.json", "slack:C1:ts1", hours=2)

    sessions = await mgr.list_sessions()
    assert len(sessions) == 1
    assert "slack:C2:ts2" in sessions


async def test_purge_expired(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=1.0)
    sid1, _ = await mgr.get_or_create("slack:C1:ts1")
    await mgr.get_or_create("slack:C2:ts2")
    _age(tmp_path / "sessions.json", "slack:C1:ts1", hours=2)

    purged = await mgr.purge_expired()
    assert purged == [sid1]

    # Verify it's gone from the store file too
    data = json.loads((tmp_path / "sessions.json").read_text())
    assert "slack:C1:ts1" not in data
    assert "slack:C2:ts2" in data


async def test_expired_entry_invisible_after_restart(tmp_path: Path):
    store = tmp_path / "sessions.json"
    past = datetime.now(UTC) - timedelta(hours=100)
    store.write_text(
        json.dumps(
            {
                "slack:old:ts1": {
                    "session_id": "old-uuid",
                    "created_at": past.isoformat(),
                    "last_used": past.isoformat(),
                }
            }
        )
    )

    mgr = _mgr(tmp_path, ttl_hours=72.0)
    # Expired on disk → never visible; the periodic purge drains the file
    # (and reports the id for agent-side cleanup).
    assert await mgr.get("slack:old:ts1") is None
    assert await mgr.purge_expired() == ["old-uuid"]


async def test_active_session_not_expired(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=72.0)
    sid, _ = await mgr.get_or_create("slack:C123:ts1")

    assert await mgr.get("slack:C123:ts1") == sid
    assert await mgr.purge_expired() == []


async def test_ttl_resets_on_use(tmp_path: Path):
    mgr = _mgr(tmp_path, ttl_hours=2.0)
    await mgr.get_or_create("slack:C123:ts1")
    # 1.5 hours ago — within TTL
    _age(tmp_path / "sessions.json", "slack:C123:ts1", hours=1.5)

    # Accessing it refreshes last_used
    _sid, is_new = await mgr.get_or_create("slack:C123:ts1")
    assert is_new is False

    assert await mgr.purge_expired() == []


# --- Failure paths: corrupt store, unwritable disk, unparsable timestamps ---


async def test_corrupt_store_file_starts_empty(tmp_path: Path):
    (tmp_path / "sessions.json").write_text("{not valid json!!")
    mgr = _mgr(tmp_path)
    assert await mgr.list_sessions() == {}
    assert await mgr.get("slack:C123:ts1") is None


async def test_unparsable_last_used_treated_as_expired(tmp_path: Path):
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "slack:C1:t1": {
                    "session_id": "sid-garbage",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "last_used": "not-a-timestamp",
                },
                "slack:C1:t2": {
                    "session_id": "sid-missing",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            }
        )
    )
    mgr = _mgr(tmp_path)
    # Both entries are unparsable → expired → invisible.
    assert await mgr.get("slack:C1:t1") is None
    assert await mgr.get("slack:C1:t2") is None
    assert await mgr.list_sessions() == {}


async def test_new_session_persist_failure_raises_and_rolls_back(tmp_path: Path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    mgr = SessionManager(SessionConfig(store_path=store_dir / "sessions.json"))
    store_dir.chmod(0o555)  # store file can no longer be created
    try:
        with pytest.raises(SessionStoreError, match="Failed to persist"):
            await mgr.get_or_create("slack:C123:ts1")
        assert await mgr.get("slack:C123:ts1") is None
        assert await mgr.list_sessions() == {}
    finally:
        store_dir.chmod(0o755)


async def test_store_error_is_an_oserror(tmp_path: Path):
    # Callers that guarded on OSError before the port existed keep working.
    assert issubclass(SessionStoreError, OSError)


async def test_touch_failure_keeps_old_last_used(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = _mgr(tmp_path)
    sid, _ = await mgr.get_or_create("slack:C123:ts1")
    before = (await mgr.list_sessions())["slack:C123:ts1"].last_used
    store.chmod(0o444)  # subsequent saves fail
    try:
        sid_again, is_new = await mgr.get_or_create("slack:C123:ts1")
        assert sid_again == sid
        assert is_new is False
        after = (await mgr.list_sessions())["slack:C123:ts1"].last_used
        assert after == before
    finally:
        store.chmod(0o644)


async def test_delete_failure_restores_entry(tmp_path: Path):
    store = tmp_path / "sessions.json"
    mgr = _mgr(tmp_path)
    sid, _ = await mgr.get_or_create("slack:C123:ts1")
    store.chmod(0o444)
    try:
        assert await mgr.delete("slack:C123:ts1") is False
        assert await mgr.get("slack:C123:ts1") == sid
    finally:
        store.chmod(0o644)


# --- Agent affinity: a remapped key must not resume the old session ---


async def test_same_agent_resumes(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, new1 = await mgr.get_or_create("slack:C1:ts1", agent="research")
    sid2, new2 = await mgr.get_or_create("slack:C1:ts1", agent="research")
    assert (new1, new2) == (True, False)
    assert sid1 == sid2


async def test_agent_mismatch_mints_fresh_session(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, _ = await mgr.get_or_create("slack:C1:ts1", agent="research")
    sid2, new2 = await mgr.get_or_create("slack:C1:ts1", agent="ops")
    assert new2 is True
    assert sid1 != sid2


async def test_agent_to_default_remap_mints_fresh_session(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, _ = await mgr.get_or_create("slack:C1:ts1", agent="research")
    sid2, new2 = await mgr.get_or_create("slack:C1:ts1")
    assert new2 is True
    assert sid1 != sid2


async def test_orphaned_session_drained_by_purge_expired(tmp_path: Path):
    mgr = _mgr(tmp_path)
    sid1, _ = await mgr.get_or_create("slack:C1:ts1")
    await mgr.get_or_create("slack:C1:ts1", agent="research")

    # The remapped-away session surfaces exactly once, like a TTL purge.
    assert await mgr.purge_expired() == [sid1]
    assert await mgr.purge_expired() == []


async def test_pre_upgrade_entry_without_agent_field_resumes_as_default(
    tmp_path: Path,
):
    """Old sessions.json entries lack "agent" — they belong to the default
    agent and must keep resuming for unmapped keys."""
    store = tmp_path / "sessions.json"
    mgr1 = _mgr(tmp_path)
    sid1, _ = await mgr1.get_or_create("slack:C1:ts1")
    # Simulate a pre-upgrade store: strip any agent key from the entry.
    data = json.loads(store.read_text())
    data["slack:C1:ts1"].pop("agent", None)
    store.write_text(json.dumps(data))

    mgr2 = _mgr(tmp_path)
    sid2, is_new = await mgr2.get_or_create("slack:C1:ts1")

    assert is_new is False
    assert sid1 == sid2


async def test_agent_persisted_across_restart(tmp_path: Path):
    mgr1 = _mgr(tmp_path)
    sid1, _ = await mgr1.get_or_create("slack:C1:ts1", agent="research")

    mgr2 = _mgr(tmp_path)
    sid_same, new_same = await mgr2.get_or_create("slack:C1:ts1", agent="research")
    assert (sid_same, new_same) == (sid1, False)

    mgr3 = _mgr(tmp_path)
    sid_other, new_other = await mgr3.get_or_create("slack:C1:ts1", agent="ops")
    assert new_other is True
    assert sid_other != sid1

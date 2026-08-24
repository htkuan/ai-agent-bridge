"""Contract suite for the ``SessionStore`` port.

Every implementation — the real ``JsonSessionStore`` and the in-memory
fake — must round-trip entries identically, treat deletes of missing keys
as no-ops, and hand out snapshots (not live views) from ``list_all``.
A future RDBMS store joins by adding one fixture param.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.bridge.protocols import SessionEntry, SessionStore
from agent_bridge.bridge.stores import JsonSessionStore
from tests.fakes import InMemorySessionStore


@pytest.fixture(params=["json", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SessionStore:
    if request.param == "json":
        return JsonSessionStore(tmp_path / "sessions.json")
    return InMemorySessionStore()


def _entry(sid: str = "sid-1", agent: str | None = None) -> SessionEntry:
    return SessionEntry(
        session_id=sid,
        created_at="2026-01-01T00:00:00+00:00",
        last_used="2026-01-02T00:00:00+00:00",
        agent=agent,
    )


async def test_get_missing_returns_none(store: SessionStore):
    assert await store.get("nope") is None


async def test_put_get_roundtrip(store: SessionStore):
    entry = _entry(agent="research")
    await store.put("slack:C1:t1", entry)
    assert await store.get("slack:C1:t1") == entry


async def test_roundtrip_preserves_none_agent(store: SessionStore):
    await store.put("slack:C1:t1", _entry(agent=None))
    got = await store.get("slack:C1:t1")
    assert got is not None
    assert got.agent is None


async def test_put_overwrites(store: SessionStore):
    await store.put("k", _entry("sid-1"))
    await store.put("k", _entry("sid-2"))
    got = await store.get("k")
    assert got is not None
    assert got.session_id == "sid-2"


async def test_delete_removes(store: SessionStore):
    await store.put("k", _entry())
    await store.delete("k")
    assert await store.get("k") is None


async def test_delete_missing_is_noop(store: SessionStore):
    await store.delete("never-existed")  # must not raise


async def test_list_all_returns_every_entry(store: SessionStore):
    await store.put("a", _entry("sid-a"))
    await store.put("b", _entry("sid-b", agent="ops"))
    entries = await store.list_all()
    assert set(entries) == {"a", "b"}
    assert entries["b"].agent == "ops"


async def test_list_all_is_a_snapshot(store: SessionStore):
    await store.put("a", _entry())
    snapshot = await store.list_all()
    snapshot.pop("a")
    assert await store.get("a") is not None


# --- JSON-specific: the on-disk format is a compatibility surface ---


async def test_json_file_omits_agent_when_none(tmp_path: Path):
    """Pre-profile stores never had an "agent" key; writing None as an
    explicit key would make downgrades and hand-edits noisier."""
    path = tmp_path / "sessions.json"
    store = JsonSessionStore(path)
    await store.put("k", _entry(agent=None))
    assert "agent" not in json.loads(path.read_text())["k"]

    await store.put("k", _entry(agent="research"))
    assert json.loads(path.read_text())["k"]["agent"] == "research"


async def test_json_survives_process_restart(tmp_path: Path):
    path = tmp_path / "sessions.json"
    await JsonSessionStore(path).put("k", _entry("sid-x"))
    got = await JsonSessionStore(path).get("k")
    assert got is not None
    assert got.session_id == "sid-x"

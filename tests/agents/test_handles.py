"""SessionHandleStore: the persistent bridge-session → agent-handle map."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.agents.handles import SessionHandleStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "handles.json"


def test_round_trip(store_path: Path):
    store = SessionHandleStore(store_path)
    assert store.get("s1") is None
    store.put("s1", "thread-1")
    assert store.get("s1") == "thread-1"


def test_put_is_an_idempotent_upsert(store_path: Path):
    store = SessionHandleStore(store_path)
    store.put("s1", "thread-1")
    store.put("s1", "thread-1")
    store.put("s1", "thread-2")
    assert store.get("s1") == "thread-2"
    assert json.loads(store_path.read_text()) == {"s1": "thread-2"}


def test_persists_across_instances(store_path: Path):
    SessionHandleStore(store_path).put("s1", "thread-1")
    assert SessionHandleStore(store_path).get("s1") == "thread-1"


def test_two_instances_on_one_file_do_not_lose_entries(store_path: Path):
    # Two controller instances (profiles sharing a work_dir) share the file;
    # read-merge-write keeps each other's entries intact.
    a = SessionHandleStore(store_path)
    b = SessionHandleStore(store_path)
    a.put("s1", "thread-1")
    b.put("s2", "thread-2")
    assert json.loads(store_path.read_text()) == {
        "s1": "thread-1",
        "s2": "thread-2",
    }
    a.discard("s1")
    assert json.loads(store_path.read_text()) == {"s2": "thread-2"}


def test_discard_removes_and_persists(store_path: Path):
    store = SessionHandleStore(store_path)
    store.put("s1", "thread-1")
    store.discard("s1")
    assert store.get("s1") is None
    assert json.loads(store_path.read_text()) == {}


def test_discard_unknown_id_is_a_noop(store_path: Path):
    store = SessionHandleStore(store_path)
    store.discard("never-seen")
    # No entry was removed, so nothing was written either.
    assert not store_path.exists()


def test_missing_file_starts_empty(store_path: Path):
    assert SessionHandleStore(store_path).get("s1") is None


def test_corrupt_file_starts_empty_with_warning(
    store_path: Path, caplog: pytest.LogCaptureFixture
):
    store_path.write_text("not json{")
    with caplog.at_level("WARNING"):
        store = SessionHandleStore(store_path)
    assert store.get("s1") is None
    assert "Failed to load session handle store" in caplog.text


def test_unexpected_shape_starts_empty_with_warning(
    store_path: Path, caplog: pytest.LogCaptureFixture
):
    store_path.write_text(json.dumps({"s1": 42}))
    with caplog.at_level("WARNING"):
        store = SessionHandleStore(store_path)
    assert store.get("s1") is None
    assert "unexpected shape" in caplog.text


def test_save_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    # The parent "directory" is a file, so mkdir/write must fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    store = SessionHandleStore(blocker / "handles.json")
    with caplog.at_level("ERROR"):
        store.put("s1", "thread-1")
    assert "Failed to save session handle store" in caplog.text
    # The in-memory map still serves this process.
    assert store.get("s1") == "thread-1"


def test_creates_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "handles.json"
    SessionHandleStore(path).put("s1", "thread-1")
    assert json.loads(path.read_text()) == {"s1": "thread-1"}

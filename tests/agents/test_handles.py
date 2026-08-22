from __future__ import annotations

import json
from pathlib import Path

from agent_bridge.agents.handles import SessionHandleStore


def test_store_round_trips_handle(tmp_path: Path):
    store = SessionHandleStore(tmp_path / "handles.json")

    store.put("bridge-1", "native-1")

    assert store.get("bridge-1") == "native-1"


def test_store_persists_across_instances(tmp_path: Path):
    path = tmp_path / "handles.json"
    SessionHandleStore(path).put("bridge-1", "native-1")

    assert SessionHandleStore(path).get("bridge-1") == "native-1"


def test_mutations_read_merge_write(tmp_path: Path):
    path = tmp_path / "handles.json"
    one = SessionHandleStore(path)
    two = SessionHandleStore(path)

    one.put("bridge-1", "native-1")
    two.put("bridge-2", "native-2")

    assert json.loads(path.read_text()) == {
        "bridge-1": "native-1",
        "bridge-2": "native-2",
    }


def test_corrupt_file_starts_empty_with_warning(tmp_path: Path, caplog):
    path = tmp_path / "handles.json"
    path.write_text("not json")

    store = SessionHandleStore(path)

    assert store.get("missing") is None
    assert "Failed to load session handle store" in caplog.text


def test_non_object_file_starts_empty_with_warning(tmp_path: Path, caplog):
    path = tmp_path / "handles.json"
    path.write_text("[]")

    store = SessionHandleStore(path)

    assert store.get("missing") is None
    assert "Invalid session handle store shape" in caplog.text


def test_non_string_values_start_empty_with_warning(tmp_path: Path, caplog):
    path = tmp_path / "handles.json"
    path.write_text(json.dumps({"bridge-1": 123}))

    store = SessionHandleStore(path)

    assert store.get("bridge-1") is None
    assert "Invalid session handle store shape" in caplog.text


def test_discard_unknown_is_noop(tmp_path: Path):
    path = tmp_path / "handles.json"
    store = SessionHandleStore(path)

    store.discard("missing")

    assert json.loads(path.read_text()) == {}


def test_save_failure_is_logged(tmp_path: Path, caplog):
    parent_file = tmp_path / "not-a-dir"
    parent_file.write_text("x")
    store = SessionHandleStore(parent_file / "handles.json")

    store.put("bridge-1", "native-1")

    assert "Failed to save session handle store" in caplog.text

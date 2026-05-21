from __future__ import annotations

import json
from pathlib import Path

from agent_bridge.agents.codex.thread_map import ThreadMap


def test_get_returns_none_when_unknown(tmp_path: Path):
    tm = ThreadMap(tmp_path / "threads.json")
    assert tm.get("anything") is None


def test_set_and_get_round_trip(tmp_path: Path):
    tm = ThreadMap(tmp_path / "threads.json")
    tm.set("bridge-1", "codex-1")
    assert tm.get("bridge-1") == "codex-1"


def test_set_persists_across_instances(tmp_path: Path):
    store = tmp_path / "threads.json"
    ThreadMap(store).set("bridge-1", "codex-abc")
    assert ThreadMap(store).get("bridge-1") == "codex-abc"


def test_set_overwrites_existing_mapping(tmp_path: Path):
    store = tmp_path / "threads.json"
    tm = ThreadMap(store)
    tm.set("bridge-1", "codex-1")
    tm.set("bridge-1", "codex-2")
    assert tm.get("bridge-1") == "codex-2"
    assert ThreadMap(store).get("bridge-1") == "codex-2"


def test_set_same_value_is_noop(tmp_path: Path):
    store = tmp_path / "threads.json"
    tm = ThreadMap(store)
    tm.set("bridge-1", "codex-1")
    mtime_before = store.stat().st_mtime_ns
    # Re-set with the same value should not rewrite the file.
    tm.set("bridge-1", "codex-1")
    assert store.stat().st_mtime_ns == mtime_before


def test_delete_returns_true_when_present(tmp_path: Path):
    tm = ThreadMap(tmp_path / "threads.json")
    tm.set("bridge-1", "codex-1")
    assert tm.delete("bridge-1") is True
    assert tm.get("bridge-1") is None


def test_delete_returns_false_when_absent(tmp_path: Path):
    tm = ThreadMap(tmp_path / "threads.json")
    assert tm.delete("never-set") is False


def test_delete_persists_to_disk(tmp_path: Path):
    store = tmp_path / "threads.json"
    tm = ThreadMap(store)
    tm.set("bridge-1", "codex-1")
    tm.delete("bridge-1")
    # New instance reads disk fresh
    assert ThreadMap(store).get("bridge-1") is None


def test_load_handles_missing_file(tmp_path: Path):
    # File never created — constructor must not raise
    tm = ThreadMap(tmp_path / "does-not-exist.json")
    assert tm.get("anything") is None


def test_load_handles_corrupted_json(tmp_path: Path):
    store = tmp_path / "threads.json"
    store.write_text("not json at all{{{")
    tm = ThreadMap(store)
    # Corrupted file becomes empty map; we can still write to it.
    assert tm.get("anything") is None
    tm.set("bridge-1", "codex-1")
    assert tm.get("bridge-1") == "codex-1"


def test_load_handles_non_dict_json(tmp_path: Path):
    store = tmp_path / "threads.json"
    store.write_text(json.dumps(["not", "a", "dict"]))
    tm = ThreadMap(store)
    assert tm.get("anything") is None


def test_load_coerces_values_to_strings(tmp_path: Path):
    store = tmp_path / "threads.json"
    # Hand-write a file with numeric value to verify defensive str() coercion.
    store.write_text(json.dumps({"bridge-1": 12345}))
    tm = ThreadMap(store)
    assert tm.get("bridge-1") == "12345"


def test_save_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "nested" / "dir" / "threads.json"
    tm = ThreadMap(nested)
    tm.set("bridge-1", "codex-1")
    assert nested.exists()
    assert json.loads(nested.read_text()) == {"bridge-1": "codex-1"}

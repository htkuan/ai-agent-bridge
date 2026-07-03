from __future__ import annotations

import json
from pathlib import Path

from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.controller import (
    OpencodeController,
    OpencodeSessionMap,
)

PREFIX = "<platform-directives>"


def _config(tmp_path: Path, **overrides) -> OpencodeConfig:
    kwargs = {
        "work_dir": tmp_path,
        "session_map_path": tmp_path / "opencode-sessions.json",
    }
    kwargs.update(overrides)
    return OpencodeConfig(**kwargs)


# --- Session map ---


def test_session_map_set_get_roundtrip_persists(tmp_path: Path):
    path = tmp_path / "map.json"
    OpencodeSessionMap(path).set("bridge-1", "ses_a")

    reloaded = OpencodeSessionMap(path)
    assert reloaded.get("bridge-1") == "ses_a"
    assert json.loads(path.read_text()) == {"bridge-1": "ses_a"}


def test_session_map_get_missing_returns_none(tmp_path: Path):
    assert OpencodeSessionMap(tmp_path / "map.json").get("nope") is None


def test_session_map_remove_persists(tmp_path: Path):
    path = tmp_path / "map.json"
    session_map = OpencodeSessionMap(path)
    session_map.set("bridge-1", "ses_a")
    session_map.set("bridge-2", "ses_b")

    session_map.remove("bridge-1")

    assert session_map.get("bridge-1") is None
    assert json.loads(path.read_text()) == {"bridge-2": "ses_b"}


def test_session_map_remove_missing_is_noop(tmp_path: Path):
    path = tmp_path / "map.json"
    OpencodeSessionMap(path).remove("ghost")
    assert not path.exists()  # no gratuitous write


def test_session_map_corrupt_file_starts_empty(tmp_path: Path):
    path = tmp_path / "map.json"
    path.write_text("{corrupt")

    session_map = OpencodeSessionMap(path)
    assert session_map.get("bridge-1") is None
    session_map.set("bridge-1", "ses_a")  # still functional
    assert OpencodeSessionMap(path).get("bridge-1") == "ses_a"


def test_session_map_non_object_file_starts_empty(tmp_path: Path):
    path = tmp_path / "map.json"
    path.write_text('["not", "a", "dict"]')
    assert OpencodeSessionMap(path).get("bridge-1") is None


def test_session_map_write_failure_keeps_memory_state(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not dir")  # parent mkdir will fail with OSError

    session_map = OpencodeSessionMap(blocker / "map.json")
    session_map.set("bridge-1", "ses_a")  # must not raise

    assert session_map.get("bridge-1") == "ses_a"


# --- Command assembly ---


def test_build_command_new_session(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    cmd = controller._build_command("hello", native_id=None)
    assert cmd[:4] == ["opencode", "run", "--format", "json"]
    assert "--session" not in cmd
    assert cmd[-1] == "hello"


def test_build_command_resume_uses_native_id(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    cmd = controller._build_command("again", native_id="ses_a")
    assert cmd[:4] == ["opencode", "run", "--format", "json"]
    assert cmd[cmd.index("--session") + 1] == "ses_a"
    assert cmd[-1] == "again"


def test_build_command_includes_model_when_set(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path, model="openai/gpt-5"))
    cmd = controller._build_command("hi", native_id=None)
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5"


def test_build_command_omits_model_when_unset(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    assert "--model" not in controller._build_command("hi", native_id=None)


def test_build_command_prepends_system_prompt_with_delimiter(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    cmd = controller._build_command("hi", native_id=None, system_prompt="be brief")
    assert cmd[-1] == "<platform-directives>\nbe brief\n</platform-directives>\n\nhi"


def test_build_command_passes_prompt_verbatim_without_system_prompt(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    for system_prompt in (None, ""):
        cmd = controller._build_command(
            "[alice]: hi", native_id=None, system_prompt=system_prompt
        )
        assert cmd[-1] == "[alice]: hi"
        assert PREFIX not in cmd[-1]


# --- cleanup_session ---


async def test_cleanup_session_removes_mapping(tmp_path: Path):
    config = _config(tmp_path)
    controller = OpencodeController(config)
    controller._session_map.set("bridge-1", "ses_a")

    await controller.cleanup_session("bridge-1")

    assert controller._session_map.get("bridge-1") is None
    assert json.loads(config.session_map_path.read_text()) == {}


async def test_cleanup_session_unknown_never_raises(tmp_path: Path):
    controller = OpencodeController(_config(tmp_path))
    await controller.cleanup_session("never-seen")

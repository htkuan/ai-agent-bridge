from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.config_loader import ConfigSource


def test_defaults(tmp_path: Path):
    source = ConfigSource({"agents": {"codex": {"work_dir": str(tmp_path)}}}, env={})
    cfg = CodexConfig.from_source(source)
    assert cfg.work_dir == tmp_path.resolve()
    assert cfg.model is None
    assert cfg.sandbox == "workspace-write"
    assert cfg.timeout_seconds == 600.0
    assert cfg.session_map_path == Path("./codex-sessions.json")


def test_from_source_reads_yaml_keys(tmp_path: Path):
    source = ConfigSource(
        {
            "agents": {
                "codex": {
                    "work_dir": str(tmp_path),
                    "model": "o4-mini",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                    "session_map_path": str(tmp_path / "map.json"),
                }
            }
        },
        env={},
    )
    cfg = CodexConfig.from_source(source)
    assert cfg.model == "o4-mini"
    assert cfg.sandbox == "read-only"
    assert cfg.timeout_seconds == 30.0
    assert cfg.session_map_path == tmp_path / "map.json"


def test_env_overrides_yaml(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path), "sandbox": "read-only"}}},
        env={"AGENT_BRIDGE_CODEX_SANDBOX": "danger-full-access"},
    )
    cfg = CodexConfig.from_source(source)
    assert cfg.sandbox == "danger-full-access"


def test_from_env_reads_environ(tmp_path: Path, monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_MODEL", "gpt-5-codex")
    cfg = CodexConfig.from_env()
    assert cfg.work_dir == tmp_path.resolve()
    assert cfg.model == "gpt-5-codex"


def test_blank_model_is_none(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path), "model": "  "}}}, env={}
    )
    assert CodexConfig.from_source(source).model is None


@pytest.mark.parametrize(
    "sandbox", ["read-only", "workspace-write", "danger-full-access"]
)
def test_valid_sandbox_modes(tmp_path: Path, sandbox: str):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path), "sandbox": sandbox}}}, env={}
    )
    assert CodexConfig.from_source(source).sandbox == sandbox


def test_invalid_sandbox_rejected(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path), "sandbox": "yolo"}}}, env={}
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_SANDBOX"):
        CodexConfig.from_source(source)


def test_nonpositive_timeout_rejected(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path), "timeout_seconds": 0}}},
        env={},
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS"):
        CodexConfig.from_source(source)


def test_missing_work_dir_rejected(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"codex": {"work_dir": str(tmp_path / "nope")}}}, env={}
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_WORK_DIR"):
        CodexConfig.from_source(source)

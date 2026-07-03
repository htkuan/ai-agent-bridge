from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.config_loader import ConfigSource


def test_defaults(tmp_path: Path):
    source = ConfigSource({"agents": {"opencode": {"work_dir": str(tmp_path)}}}, env={})
    cfg = OpencodeConfig.from_source(source)
    assert cfg.work_dir == tmp_path.resolve()
    assert cfg.model is None
    assert cfg.timeout_seconds == 600.0
    assert cfg.session_map_path == Path("./opencode-sessions.json")


def test_from_source_reads_yaml_keys(tmp_path: Path):
    source = ConfigSource(
        {
            "agents": {
                "opencode": {
                    "work_dir": str(tmp_path),
                    "model": "anthropic/claude-sonnet-4-5",
                    "timeout_seconds": 30,
                    "session_map_path": str(tmp_path / "map.json"),
                }
            }
        },
        env={},
    )
    cfg = OpencodeConfig.from_source(source)
    assert cfg.model == "anthropic/claude-sonnet-4-5"
    assert cfg.timeout_seconds == 30.0
    assert cfg.session_map_path == tmp_path / "map.json"


def test_env_overrides_yaml(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"opencode": {"work_dir": str(tmp_path), "model": "openai/gpt-5"}}},
        env={"AGENT_BRIDGE_OPENCODE_MODEL": "anthropic/claude-opus-4-5"},
    )
    cfg = OpencodeConfig.from_source(source)
    assert cfg.model == "anthropic/claude-opus-4-5"


def test_from_env_reads_environ(tmp_path: Path, monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("AGENT_BRIDGE_OPENCODE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_OPENCODE_MODEL", "openai/gpt-5")
    cfg = OpencodeConfig.from_env()
    assert cfg.work_dir == tmp_path.resolve()
    assert cfg.model == "openai/gpt-5"


def test_blank_model_is_none(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"opencode": {"work_dir": str(tmp_path), "model": "  "}}}, env={}
    )
    assert OpencodeConfig.from_source(source).model is None


def test_model_without_provider_rejected(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"opencode": {"work_dir": str(tmp_path), "model": "gpt-5"}}},
        env={},
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_MODEL"):
        OpencodeConfig.from_source(source)


def test_nonpositive_timeout_rejected(tmp_path: Path):
    source = ConfigSource(
        {"agents": {"opencode": {"work_dir": str(tmp_path), "timeout_seconds": 0}}},
        env={},
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS"):
        OpencodeConfig.from_source(source)


def test_missing_work_dir_rejected(tmp_path: Path):
    source = ConfigSource({"agents": {"opencode": {"work_dir": str(tmp_path / "nope")}}}, env={})
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_WORK_DIR"):
        OpencodeConfig.from_source(source)

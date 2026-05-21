from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.codex.config import CodexConfig


def _set_required(monkeypatch, work_dir: Path):
    """Set the only env var that has no safe default for testing."""
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_WORK_DIR", str(work_dir))


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_when_only_work_dir_set(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SANDBOX", "workspace-write")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_APPROVAL", "never")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_MODEL", "")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK", "true")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "")
    monkeypatch.setenv(
        "AGENT_BRIDGE_CODEX_THREAD_MAP_PATH", str(tmp_path / "threads.json")
    )

    cfg = CodexConfig.from_env()
    assert cfg.work_dir == tmp_path.resolve()
    assert cfg.sandbox == "workspace-write"
    assert cfg.approval == "never"
    assert cfg.model == ""
    assert cfg.timeout_seconds == 600.0
    assert cfg.skip_git_repo_check is True
    assert cfg.extra_config == ()
    assert cfg.thread_map_path == tmp_path / "threads.json"


# ---------------------------------------------------------------------------
# work_dir validation
# ---------------------------------------------------------------------------


def test_work_dir_must_exist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "AGENT_BRIDGE_CODEX_WORK_DIR", str(tmp_path / "does-not-exist")
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_WORK_DIR"):
        CodexConfig.from_env()


def test_work_dir_must_be_a_directory(tmp_path: Path, monkeypatch):
    file_path = tmp_path / "a-file.txt"
    file_path.write_text("not a dir")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_WORK_DIR", str(file_path))
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_WORK_DIR"):
        CodexConfig.from_env()


# ---------------------------------------------------------------------------
# sandbox / approval validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
def test_valid_sandbox_modes_accepted(tmp_path: Path, monkeypatch, mode: str):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SANDBOX", mode)
    cfg = CodexConfig.from_env()
    assert cfg.sandbox == mode


def test_invalid_sandbox_rejected(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SANDBOX", "yolo")
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_SANDBOX"):
        CodexConfig.from_env()


@pytest.mark.parametrize("mode", ["untrusted", "on-request", "never"])
def test_valid_approval_modes_accepted(tmp_path: Path, monkeypatch, mode: str):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_APPROVAL", mode)
    cfg = CodexConfig.from_env()
    assert cfg.approval == mode


def test_invalid_approval_rejected(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_APPROVAL", "always")
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_APPROVAL"):
        CodexConfig.from_env()


# ---------------------------------------------------------------------------
# timeout validation
# ---------------------------------------------------------------------------


def test_negative_timeout_rejected(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS", "-1")
    with pytest.raises(ValueError, match="TIMEOUT"):
        CodexConfig.from_env()


def test_zero_timeout_rejected(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="TIMEOUT"):
        CodexConfig.from_env()


# ---------------------------------------------------------------------------
# extra_config parsing
# ---------------------------------------------------------------------------


def test_extra_config_empty_yields_empty_tuple(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "")
    cfg = CodexConfig.from_env()
    assert cfg.extra_config == ()


def test_extra_config_single_entry(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "model_reasoning_effort=high"
    )
    cfg = CodexConfig.from_env()
    assert cfg.extra_config == ("model_reasoning_effort=high",)


def test_extra_config_multiple_entries_with_whitespace(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "AGENT_BRIDGE_CODEX_EXTRA_CONFIG",
        " a=1 ,  b=2,c=3 ",
    )
    cfg = CodexConfig.from_env()
    assert cfg.extra_config == ("a=1", "b=2", "c=3")


def test_extra_config_blanks_dropped(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "a=1,,b=2,")
    cfg = CodexConfig.from_env()
    assert cfg.extra_config == ("a=1", "b=2")


def test_extra_config_without_equals_rejected(tmp_path: Path, monkeypatch):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "a=1,bogus,b=2")
    with pytest.raises(ValueError, match="EXTRA_CONFIG"):
        CodexConfig.from_env()


# ---------------------------------------------------------------------------
# skip_git_repo_check parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on"])
def test_skip_git_repo_check_truthy(tmp_path: Path, monkeypatch, raw: str):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK", raw)
    cfg = CodexConfig.from_env()
    assert cfg.skip_git_repo_check is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off", ""])
def test_skip_git_repo_check_falsy(tmp_path: Path, monkeypatch, raw: str):
    _set_required(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK", raw)
    cfg = CodexConfig.from_env()
    assert cfg.skip_git_repo_check is False

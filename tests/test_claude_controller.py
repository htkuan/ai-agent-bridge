from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController


def _config(work_dir: Path, worktree_enabled: bool = False) -> ClaudeConfig:
    # Bypass _validate so tests don't need a real git repo unless they want one.
    cfg = ClaudeConfig.__new__(ClaudeConfig)
    object.__setattr__(cfg, "work_dir", work_dir)
    object.__setattr__(cfg, "permission_mode", "acceptEdits")
    object.__setattr__(cfg, "timeout_seconds", 600.0)
    object.__setattr__(cfg, "worktree_enabled", worktree_enabled)
    return cfg


# --- Command builder ---


def test_build_command_no_worktree(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=False))
    cmd = controller._build_command("abc-123", "hello", is_new=True)
    assert "-w" not in cmd
    assert "--session-id" in cmd
    assert "abc-123" in cmd


def test_build_command_with_worktree_new_session(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    cmd = controller._build_command("abc-123", "hello", is_new=True)
    # -w <session_id> appears before --session-id
    w_idx = cmd.index("-w")
    assert cmd[w_idx + 1] == "abc-123"
    assert cmd.index("-w") < cmd.index("--session-id")


def test_build_command_with_worktree_resume(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    cmd = controller._build_command("abc-123", "hi again", is_new=False)
    # -w still present on resume (Claude reuses the existing worktree)
    assert "-w" in cmd
    assert cmd[cmd.index("-w") + 1] == "abc-123"
    assert "--resume" in cmd
    assert "--session-id" not in cmd


# --- system_prompt pass-through ---
#
# After the platform/agent split, the controller is platform-agnostic:
# it never inspects context, never prefixes the prompt — it just appends
# whatever system_prompt the platform supplied.


def _system_prompt(cmd: list[str]) -> str | None:
    if "--append-system-prompt" not in cmd:
        return None
    return cmd[cmd.index("--append-system-prompt") + 1]


def test_build_command_passes_prompt_verbatim(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "[alice]: hi there", is_new=True)
    # Whatever the caller passed is what -p sees
    assert cmd[cmd.index("-p") + 1] == "[alice]: hi there"


def test_build_command_omits_system_prompt_when_none(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True, system_prompt=None)
    assert "--append-system-prompt" not in cmd


def test_build_command_omits_system_prompt_when_empty(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True, system_prompt="")
    assert "--append-system-prompt" not in cmd


def test_build_command_appends_system_prompt_verbatim(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    sp = "platform-built directives that the agent must not parse"
    cmd = controller._build_command("s1", "hi", is_new=True, system_prompt=sp)
    assert _system_prompt(cmd) == sp


# --- Config validation ---


def test_worktree_validation_fails_without_git_repo(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", "true")
    with pytest.raises(ValueError, match="not a git repository"):
        ClaudeConfig.from_env()


def test_worktree_validation_fails_without_origin(tmp_path: Path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", "true")
    with pytest.raises(ValueError, match="origin"):
        ClaudeConfig.from_env()


def test_worktree_validation_passes_with_origin_head(tmp_path: Path, monkeypatch):
    # Build a repo with a working origin/HEAD
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(repo), str(origin)], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True
    )
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
        check=True,
    )

    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(repo))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", "true")
    cfg = ClaudeConfig.from_env()
    assert cfg.worktree_enabled is True
    assert cfg.work_dir == repo.resolve()


def test_worktree_disabled_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", raising=False)
    cfg = ClaudeConfig.from_env()
    assert cfg.worktree_enabled is False


# --- cleanup_session ---


async def test_cleanup_session_noop_when_disabled(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=False))
    # Should not raise even though no git repo exists
    await controller.cleanup_session("nonexistent-session")

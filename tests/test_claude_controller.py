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


# --- Context handling ---


def _system_prompt(cmd: list[str]) -> str | None:
    if "--append-system-prompt" not in cmd:
        return None
    return cmd[cmd.index("--append-system-prompt") + 1]


def test_build_command_no_context_omits_system_prompt(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True, context=None)
    # Prompt is passed verbatim, no [tag]: prefix
    assert cmd[cmd.index("-p") + 1] == "hi"
    assert "--append-system-prompt" not in cmd


def test_build_command_empty_context_omits_system_prompt(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True, context={})
    assert cmd[cmd.index("-p") + 1] == "hi"
    assert "--append-system-prompt" not in cmd


def test_build_command_chat_context_prefixes_prompt_and_emits_system_prompt(
    tmp_path: Path,
):
    controller = ClaudeController(_config(tmp_path))
    context = {
        "platform": "slack",
        "workspace": "acme",
        "channel_id": "C123",
        "channel_name": "general",
        "thread_ts": "1700000000.000100",
        "user_id": "U999",
        "user_name": "alice",
    }
    cmd = controller._build_command("s1", "do something", is_new=True, context=context)

    # Prompt prefixed with [user_name (user_id)]:
    assert cmd[cmd.index("-p") + 1] == "[alice (U999)]: do something"

    sp = _system_prompt(cmd)
    assert sp is not None
    assert "chat platform" in sp
    assert "[user_name (user_id)]" in sp
    assert "Platform: slack" in sp
    assert "Workspace: acme" in sp
    assert "Channel: #general (C123)" in sp
    assert "Thread: 1700000000.000100" in sp


def test_build_command_heartbeat_context_no_prompt_prefix(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    context = {"source": "heartbeat", "fired_at": "2026-04-26T10:00:00+00:00"}
    cmd = controller._build_command("s1", "check tasks", is_new=True, context=context)

    # Prompt passed through verbatim — no [tag]: prefix
    assert cmd[cmd.index("-p") + 1] == "check tasks"


def test_build_command_heartbeat_system_prompt_has_heartbeat_phrasing(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    context = {"source": "heartbeat", "fired_at": "2026-04-26T10:00:00+00:00"}
    cmd = controller._build_command("s1", "check tasks", is_new=True, context=context)

    sp = _system_prompt(cmd)
    assert sp is not None
    lowered = sp.lower()
    # Key phrases — partial match, not exact string
    assert "scheduled" in lowered
    assert "no user" in lowered
    assert "audit" in lowered
    # Should explicitly tell agent not to ask questions / expect a reply
    assert "question" in lowered
    assert "reply" in lowered
    # Should NOT use the chat-platform framing
    assert "chat platform" not in lowered


def test_build_command_heartbeat_includes_fired_at(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    fired_at = "2026-04-26T10:00:00+00:00"
    context = {"source": "heartbeat", "fired_at": fired_at}
    cmd = controller._build_command("s1", "check tasks", is_new=True, context=context)

    sp = _system_prompt(cmd)
    assert sp is not None
    assert fired_at in sp


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

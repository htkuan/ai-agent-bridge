from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.events import Completion
from tests.helpers import claude_result_line, install_fake_cli


def _config(
    work_dir: Path,
    worktree_enabled: bool = False,
    effort: str = "xhigh",
    timeout_seconds: float = 600.0,
) -> ClaudeConfig:
    # Bypass _validate so tests don't need a real git repo unless they want one.
    cfg = ClaudeConfig.__new__(ClaudeConfig)
    object.__setattr__(cfg, "work_dir", work_dir)
    object.__setattr__(cfg, "permission_mode", "acceptEdits")
    object.__setattr__(cfg, "timeout_seconds", timeout_seconds)
    object.__setattr__(cfg, "worktree_enabled", worktree_enabled)
    object.__setattr__(cfg, "effort", effort)
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


# --- Effort flag ---


def test_build_command_includes_default_effort(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True)
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_build_command_includes_custom_effort(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, effort="low"))
    cmd = controller._build_command("s1", "hi", is_new=True)
    assert cmd[cmd.index("--effort") + 1] == "low"


def test_effort_validation_rejects_invalid(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_EFFORT", "ultra")
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_EFFORT"):
        ClaudeConfig.from_env()


def test_effort_defaults_to_xhigh_when_unset(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_BRIDGE_CLAUDE_EFFORT", raising=False)
    cfg = ClaudeConfig.from_env()
    assert cfg.effort == "xhigh"


def test_effort_empty_string_falls_back_to_xhigh(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_EFFORT", "")
    cfg = ClaudeConfig.from_env()
    assert cfg.effort == "xhigh"


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
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
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


def _init_repo_with_worktree(repo: Path, session_id: str) -> Path:
    """Init a git repo with one commit and create a worktree for the session."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    worktree_path = repo / ".claude" / "worktrees" / session_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            f"worktree-{session_id}",
            str(worktree_path),
        ],
        check=True,
    )
    return worktree_path


async def test_cleanup_session_force_removes_dirty_worktree(tmp_path: Path):
    session_id = "dirty-session"
    worktree_path = _init_repo_with_worktree(tmp_path, session_id)
    # Leave an untracked file behind so plain `git worktree remove` refuses
    (worktree_path / "untracked.txt").write_text("leftover")

    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    await controller.cleanup_session(session_id)

    assert not worktree_path.exists()
    branches = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--list", f"worktree-{session_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""


async def test_cleanup_session_removes_clean_worktree(tmp_path: Path):
    session_id = "clean-session"
    worktree_path = _init_repo_with_worktree(tmp_path, session_id)

    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    await controller.cleanup_session(session_id)

    assert not worktree_path.exists()


# --- run() stream handling: backgrounded grandchild holding stdout open ---


async def test_run_breaks_on_result_despite_orphan_holding_stdout(tmp_path: Path, prepend_path):
    # A fake `claude` that emits a result line, then leaves a backgrounded
    # child holding the stdout pipe open (mimicking a nested `claude -p`
    # spawned by a skill). Without breaking on `result`, the controller's
    # readline()/wait() would block until the overall timeout fires.
    pidfile = tmp_path / "orphan.pid"
    install_fake_cli(
        tmp_path / "bin",
        lines=[claude_result_line("done", session_id="s1", duration_ms=1000)],
        orphan_pidfile=pidfile,
    )
    prepend_path(tmp_path / "bin")

    # Small timeout: if the loop waited for EOF it would fail here.
    controller = ClaudeController(_config(tmp_path, timeout_seconds=5.0))

    start = asyncio.get_event_loop().time()
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    elapsed = asyncio.get_event_loop().time() - start

    # Completed promptly instead of hanging to the 5s timeout.
    assert elapsed < 3.0
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert completions[0].is_error is False
    assert completions[0].text == "done"

    # The orphaned grandchild was reaped along with the process group.
    orphan_pid = int(pidfile.read_text().strip())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

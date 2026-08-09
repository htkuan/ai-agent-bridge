from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.events import Completion, StatusUpdate, TextDelta
from tests.conftest import FakeClaudeFactory
from tests.fakes import claude_cli


def _config(
    work_dir: Path,
    worktree_enabled: bool = False,
    effort: str = "xhigh",
    timeout_seconds: float = 600.0,
    cli_path: str = "claude",
    permission_mode: str = "acceptEdits",
) -> ClaudeConfig:
    # Bypass _validate so tests don't need a real git repo unless they want one.
    cfg = ClaudeConfig.__new__(ClaudeConfig)
    object.__setattr__(cfg, "work_dir", work_dir)
    object.__setattr__(cfg, "permission_mode", permission_mode)
    object.__setattr__(cfg, "timeout_seconds", timeout_seconds)
    object.__setattr__(cfg, "worktree_enabled", worktree_enabled)
    object.__setattr__(cfg, "effort", effort)
    object.__setattr__(cfg, "cli_path", cli_path)
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


# --- CLI path ---


def test_build_command_uses_default_cli_path(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path))
    cmd = controller._build_command("s1", "hi", is_new=True)
    assert cmd[0] == "claude"


def test_build_command_uses_custom_cli_path(tmp_path: Path):
    controller = ClaudeController(_config(tmp_path, cli_path="/opt/bin/claude"))
    cmd = controller._build_command("s1", "hi", is_new=True)
    assert cmd[0] == "/opt/bin/claude"


def test_cli_path_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_CLI_PATH", "/opt/bin/claude")
    cfg = ClaudeConfig.from_env()
    assert cfg.cli_path == "/opt/bin/claude"


def test_cli_path_defaults_to_claude_when_unset(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_BRIDGE_CLAUDE_CLI_PATH", raising=False)
    cfg = ClaudeConfig.from_env()
    assert cfg.cli_path == "claude"


def test_cli_path_blank_env_falls_back_to_claude(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_CLI_PATH", "   ")
    cfg = ClaudeConfig.from_env()
    assert cfg.cli_path == "claude"


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


def _fake_claude_with_orphan(bin_dir: Path, pidfile: Path) -> None:
    """Install a fake `claude` that emits a result line, then leaves a
    backgrounded child holding the stdout pipe open (mimicking a nested
    `claude -p` spawned by a skill). Without breaking on `result`, the bridge's
    readline()/wait() would block until the overall timeout fires.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        # Background child inherits stdout; keeps the pipe write-end open.
        "sleep 30 &\n"
        f'echo $! > "{pidfile}"\n'
        '{ echo \'{"type":"result","subtype":"success","session_id":"s1",'
        '"result":"done","total_cost_usd":0.01,"duration_ms":1000,'
        '"is_error":false}\'; }\n'
        "exit 0\n"
    )
    script.chmod(0o755)


async def test_run_breaks_on_result_despite_orphan_holding_stdout(
    tmp_path: Path, monkeypatch
):
    pidfile = tmp_path / "orphan.pid"
    bin_dir = tmp_path / "bin"
    _fake_claude_with_orphan(bin_dir, pidfile)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

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


# --- Permission-mode flag variants ---


def test_build_command_dangerously_skip_permissions(tmp_path: Path):
    controller = ClaudeController(
        _config(tmp_path, permission_mode="dangerously-skip-permissions")
    )
    cmd = controller._build_command("s1", "hi", is_new=True)
    assert "--dangerously-skip-permissions" in cmd
    assert "--permission-mode" not in cmd


# --- run(): failure and timeout paths, driven by the scripted fake CLI ---


async def test_run_yields_error_completion_on_nonzero_exit_before_result(
    fake_claude: FakeClaudeFactory,
):
    cli = fake_claude([claude_cli.stderr_line("kaboom"), claude_cli.exit_code(3)])
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    assert len(events) == 1
    completion = events[0]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "exited with code 3" in completion.text


async def test_run_skips_malformed_json_lines(fake_claude: FakeClaudeFactory):
    cli = fake_claude(
        [claude_cli.raw_line("not json at all"), *claude_cli.reply_steps("fine")]
    )
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert deltas == "fine"
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False


async def test_run_filters_agent_internal_events(fake_claude: FakeClaudeFactory):
    cli = fake_claude([claude_cli.thinking("pondering"), claude_cli.result("done")])
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    assert not any(isinstance(e, TextDelta | StatusUpdate) for e in events)
    assert isinstance(events[-1], Completion)


async def test_run_times_out_and_reports_error(fake_claude: FakeClaudeFactory):
    cli = fake_claude([claude_cli.hang(30.0)], timeout_seconds=0.5)
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    assert len(events) == 1
    completion = events[0]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "timed out" in completion.text


async def test_run_zero_timeout_expires_before_first_read(
    fake_claude: FakeClaudeFactory,
):
    cli = fake_claude(claude_cli.reply_steps("never"), timeout_seconds=0.0)
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "timed out" in completion.text


async def test_run_sigkills_process_that_ignores_sigterm(
    fake_claude: FakeClaudeFactory,
):
    # SIGTERM is ignored by the scenario, so the graceful kill stalls and the
    # controller must escalate to SIGKILL (~5s wait_for budget) to finish.
    # The marker text is only emitted after the handler is installed, so a
    # 2s read budget guarantees SIGTERM immunity is armed before the kill.
    cli = fake_claude(
        [
            claude_cli.ignore_sigterm(),
            claude_cli.assistant_text("armed"),
            claude_cli.hang(30.0),
        ],
        timeout_seconds=2.0,
    )
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "timed out" in completion.text


async def test_run_survives_grandchild_holding_stderr_open(
    fake_claude: FakeClaudeFactory,
):
    # The TERM-immune grandchild inherits stderr and keeps it open after the
    # main process exits; the stderr drain must give up (~5s) instead of
    # wedging, and the successful result must not gain a spurious error.
    cli = fake_claude(
        [claude_cli.hold_stderr_grandchild(8.0), *claude_cli.reply_steps("done")]
    )
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert completions[0].is_error is False


# --- _kill_process_tree fallbacks ---


async def test_kill_process_tree_ignores_already_exited_group(
    monkeypatch: pytest.MonkeyPatch,
):
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()

    def raise_lookup(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", raise_lookup)
    ClaudeController._kill_process_tree(proc, graceful=True)  # must not raise


@pytest.mark.parametrize("graceful", [True, False])
async def test_kill_process_tree_falls_back_to_direct_kill(
    monkeypatch: pytest.MonkeyPatch, graceful: bool
):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        start_new_session=True,
    )

    def deny(pid: int, sig: int) -> None:
        raise PermissionError("killpg denied")

    monkeypatch.setattr(os, "killpg", deny)
    ClaudeController._kill_process_tree(proc, graceful=graceful)
    await asyncio.wait_for(proc.wait(), timeout=5.0)


# --- cleanup_session: git failure paths ---


async def test_cleanup_session_leaves_worktree_when_force_remove_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    worktree = tmp_path / ".claude" / "worktrees" / "s1"
    worktree.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    async def failing_git(cwd: Path, *args: str) -> tuple[int, str]:
        calls.append(args)
        return 1, "worktree locked"

    monkeypatch.setattr(ClaudeController, "_run_git", staticmethod(failing_git))
    await controller.cleanup_session("s1")  # never raises

    assert worktree.exists()
    assert calls == [
        ("worktree", "remove", str(worktree)),
        ("worktree", "remove", "--force", str(worktree)),
    ]  # gives up after the force retry: no branch -D


async def test_cleanup_session_prunes_when_worktree_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    controller = ClaudeController(_config(tmp_path, worktree_enabled=True))
    calls: list[tuple[str, ...]] = []

    async def recording_git(cwd: Path, *args: str) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(ClaudeController, "_run_git", staticmethod(recording_git))
    await controller.cleanup_session("s1")

    assert calls == [("worktree", "prune"), ("branch", "-D", "worktree-s1")]


async def test_run_git_timeout_kills_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Same manual-deadline pattern as the code under test.
    async def instant_timeout(awaitable: object, timeout: float) -> None:  # noqa: ASYNC109
        del timeout
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(
        "agent_bridge.agents.claude.controller.asyncio.wait_for", instant_timeout
    )
    rc, err = await ClaudeController._run_git(tmp_path, "status")
    assert (rc, err) == (-1, "timeout")


def test_work_dir_validation_rejects_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="does not exist"):
        ClaudeConfig.from_env()


def test_permission_mode_validation_rejects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_PERMISSION_MODE", "yolo")
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_PERMISSION_MODE"):
        ClaudeConfig.from_env()


def test_timeout_validation_rejects_non_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="must be positive"):
        ClaudeConfig.from_env()


def test_cli_path_validation_rejects_empty(tmp_path: Path):
    config = ClaudeConfig(work_dir=tmp_path, cli_path="")
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_CLI_PATH"):
        config._validate()

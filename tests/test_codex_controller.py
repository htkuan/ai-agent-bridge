from __future__ import annotations

from pathlib import Path

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController, _compose_prompt


def _config(
    tmp_path: Path,
    *,
    sandbox: str = "workspace-write",
    approval: str = "never",
    model: str = "",
    skip_git_repo_check: bool = True,
    extra_config: tuple[str, ...] = (),
    thread_map_path: Path | None = None,
) -> CodexConfig:
    """Build a CodexConfig bypassing _validate so tests don't need a real
    work_dir for argv-shape assertions."""
    cfg = CodexConfig.__new__(CodexConfig)
    object.__setattr__(cfg, "work_dir", tmp_path)
    object.__setattr__(cfg, "sandbox", sandbox)
    object.__setattr__(cfg, "approval", approval)
    object.__setattr__(cfg, "model", model)
    object.__setattr__(cfg, "timeout_seconds", 600.0)
    object.__setattr__(
        cfg, "thread_map_path", thread_map_path or (tmp_path / "threads.json"),
    )
    object.__setattr__(cfg, "skip_git_repo_check", skip_git_repo_check)
    object.__setattr__(cfg, "extra_config", extra_config)
    return cfg


# ---------------------------------------------------------------------------
# _build_command — flag ordering and composition
# ---------------------------------------------------------------------------


def test_build_command_new_session_has_correct_top_level_flags(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path))
    cmd = ctrl._build_command(None)

    # codex 0.128+: -s/-a/-C must come BEFORE the `exec` subcommand.
    assert cmd[0] == "codex"
    exec_idx = cmd.index("exec")
    assert cmd.index("-s") < exec_idx
    assert cmd.index("-a") < exec_idx
    assert cmd.index("-C") < exec_idx

    # Sandbox + approval values land in the right slot
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd[cmd.index("-a") + 1] == "never"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)


def test_build_command_new_session_has_no_resume_arg(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path))
    cmd = ctrl._build_command(None)
    assert "resume" not in cmd
    # exec is the last subcommand-level token before exec-scoped flags
    exec_idx = cmd.index("exec")
    assert cmd[exec_idx + 1] == "--json"


def test_build_command_resume_inserts_thread_id_after_exec(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path))
    cmd = ctrl._build_command("thread-abc-123")
    exec_idx = cmd.index("exec")
    # `exec resume <thread_id>` are consecutive
    assert cmd[exec_idx : exec_idx + 3] == ["exec", "resume", "thread-abc-123"]


def test_build_command_includes_json_and_skip_git_check(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path, skip_git_repo_check=True))
    cmd = ctrl._build_command(None)
    assert "--json" in cmd
    assert "--skip-git-repo-check" in cmd
    # Both belong to the exec subcommand — must come AFTER `exec`
    assert cmd.index("--json") > cmd.index("exec")
    assert cmd.index("--skip-git-repo-check") > cmd.index("exec")


def test_build_command_omits_skip_git_check_when_disabled(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path, skip_git_repo_check=False))
    cmd = ctrl._build_command(None)
    assert "--skip-git-repo-check" not in cmd


def test_build_command_includes_model_when_set(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path, model="gpt-5-codex"))
    cmd = ctrl._build_command(None)
    assert cmd[cmd.index("-m") + 1] == "gpt-5-codex"
    # Model is top-level, before exec
    assert cmd.index("-m") < cmd.index("exec")


def test_build_command_omits_model_when_empty(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path, model=""))
    cmd = ctrl._build_command(None)
    assert "-m" not in cmd


def test_build_command_includes_extra_config(tmp_path: Path):
    ctrl = CodexController(
        _config(
            tmp_path,
            extra_config=("model_reasoning_effort=high", "disable_response_storage=true"),
        )
    )
    cmd = ctrl._build_command(None)
    # Each entry passed as `-c key=value`, before exec
    c_indices = [i for i, tok in enumerate(cmd) if tok == "-c"]
    assert len(c_indices) == 2
    for idx in c_indices:
        assert idx < cmd.index("exec")
    assert cmd[c_indices[0] + 1] == "model_reasoning_effort=high"
    assert cmd[c_indices[1] + 1] == "disable_response_storage=true"


def test_build_command_ends_with_stdin_marker(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path))
    assert ctrl._build_command(None)[-1] == "-"
    assert ctrl._build_command("thread-1")[-1] == "-"


def test_build_command_is_pure_no_argv_mutation(tmp_path: Path):
    ctrl = CodexController(_config(tmp_path))
    a = ctrl._build_command(None)
    b = ctrl._build_command(None)
    assert a == b
    # Different calls produce different lists, not aliases
    a.append("MUTATED")
    assert "MUTATED" not in ctrl._build_command(None)


# ---------------------------------------------------------------------------
# _compose_prompt — stdin payload composition
# ---------------------------------------------------------------------------


def test_compose_prompt_without_system_returns_user_text_verbatim():
    assert _compose_prompt(None, "hello") == "hello"
    assert _compose_prompt("", "hello") == "hello"


def test_compose_prompt_with_system_wraps_in_tagged_blocks():
    payload = _compose_prompt("be terse", "what time is it")
    assert "<system>\nbe terse\n</system>" in payload
    assert "<user>\nwhat time is it\n</user>" in payload
    # System block must come before user block.
    assert payload.index("<system>") < payload.index("<user>")


def test_compose_prompt_preserves_multiline_content():
    sp = "line1\nline2"
    p = "msg-line-1\nmsg-line-2"
    payload = _compose_prompt(sp, p)
    assert sp in payload
    assert p in payload


# ---------------------------------------------------------------------------
# cleanup_session — thread-map side effect only
# ---------------------------------------------------------------------------


async def test_cleanup_session_removes_mapping(tmp_path: Path):
    cfg = _config(tmp_path, thread_map_path=tmp_path / "threads.json")
    ctrl = CodexController(cfg)
    ctrl._thread_map.set("bridge-1", "codex-1")
    await ctrl.cleanup_session("bridge-1")
    assert ctrl._thread_map.get("bridge-1") is None


async def test_cleanup_session_noop_when_unknown(tmp_path: Path):
    cfg = _config(tmp_path, thread_map_path=tmp_path / "threads.json")
    ctrl = CodexController(cfg)
    # Must not raise even though the session was never registered.
    await ctrl.cleanup_session("never-seen")

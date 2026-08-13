"""ClaudeConfig: env parsing, value validation, and the prerequisite probes.

``from_env`` takes an explicit mapping, so these never touch the process
environment (and a developer's local ``.env`` can't reach them).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bridge.agents.claude.config import ClaudeConfig


def _repo_with_origin_head(tmp_path: Path) -> Path:
    """A git repo whose origin/HEAD resolves — what worktree mode requires."""
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
    return repo


# --- from_env ---


def test_from_env_defaults(tmp_path: Path):
    config = ClaudeConfig.from_env({"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path)})
    assert config.work_dir == tmp_path.resolve()
    assert config.permission_mode == "acceptEdits"
    assert config.timeout_seconds == 600.0
    assert config.worktree_enabled is False
    assert config.effort == "xhigh"
    assert config.cli_path == "claude"


def test_from_env_reads_all_variables(tmp_path: Path):
    config = ClaudeConfig.from_env(
        {
            "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_CLAUDE_PERMISSION_MODE": "plan",
            "AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS": "30",
            "AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED": "false",
            "AGENT_BRIDGE_CLAUDE_EFFORT": "low",
            "AGENT_BRIDGE_CLAUDE_CLI_PATH": "/opt/bin/claude",
        }
    )
    assert config.permission_mode == "plan"
    assert config.timeout_seconds == 30.0
    assert config.effort == "low"
    assert config.cli_path == "/opt/bin/claude"


@pytest.mark.parametrize(
    "var", ["AGENT_BRIDGE_CLAUDE_EFFORT", "AGENT_BRIDGE_CLAUDE_CLI_PATH"]
)
def test_blank_env_falls_back_to_default(tmp_path: Path, var: str):
    config = ClaudeConfig.from_env(
        {"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path), var: "   "}
    )
    assert config.effort == "xhigh"
    assert config.cli_path == "claude"


# --- _validate: value checks, run on every construction ---


def test_permission_mode_rejects_unknown(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_PERMISSION_MODE"):
        ClaudeConfig(work_dir=tmp_path, permission_mode="yolo")


def test_timeout_rejects_non_positive(tmp_path: Path):
    with pytest.raises(ValueError, match="must be positive"):
        ClaudeConfig(work_dir=tmp_path, timeout_seconds=0)


def test_cli_path_rejects_empty(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_CLI_PATH"):
        ClaudeConfig(work_dir=tmp_path, cli_path="")


def test_effort_rejects_invalid(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_EFFORT"):
        ClaudeConfig(work_dir=tmp_path, effort="ultra")


def test_validation_also_fires_through_from_env(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_EFFORT"):
        ClaudeConfig.from_env(
            {
                "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path),
                "AGENT_BRIDGE_CLAUDE_EFFORT": "ultra",
            }
        )


# --- check_prerequisites: probes the world, only from_env runs it ---


def test_construction_does_not_probe_the_filesystem(tmp_path: Path):
    # A missing work_dir is fine to hold in memory; only startup rejects it.
    config = ClaudeConfig(work_dir=tmp_path / "nope", worktree_enabled=True)
    assert config.worktree_enabled is True


def test_missing_work_dir_rejected_at_startup(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        ClaudeConfig.from_env({"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path / "nope")})


@pytest.mark.integration
def test_worktree_prereqs_fail_without_git_repo(tmp_path: Path):
    with pytest.raises(ValueError, match="not a git repository"):
        ClaudeConfig(work_dir=tmp_path, worktree_enabled=True).check_prerequisites()


@pytest.mark.integration
def test_worktree_prereqs_fail_without_origin(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(ValueError, match="origin"):
        ClaudeConfig(work_dir=tmp_path, worktree_enabled=True).check_prerequisites()


@pytest.mark.integration
def test_worktree_prereqs_pass_with_origin_head(tmp_path: Path):
    repo = _repo_with_origin_head(tmp_path)
    config = ClaudeConfig.from_env(
        {
            "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(repo),
            "AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED": "true",
        }
    )
    assert config.worktree_enabled is True
    assert config.work_dir == repo.resolve()

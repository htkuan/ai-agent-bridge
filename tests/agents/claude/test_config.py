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


def test_work_dir_is_required(tmp_path: Path):
    # No default: forgetting it must not silently fall back to the cwd.
    with pytest.raises(TypeError, match="work_dir"):
        ClaudeConfig()  # pyright: ignore[reportCallIssue]


def test_validation_also_fires_through_from_env(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_EFFORT"):
        ClaudeConfig.from_env(
            {
                "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path),
                "AGENT_BRIDGE_CLAUDE_EFFORT": "ultra",
            }
        )


# --- check_prerequisites: probes the world, run once by app.run() ---


def test_construction_does_not_probe_the_filesystem(tmp_path: Path):
    # A missing work_dir is fine to hold in memory; only startup rejects it.
    config = ClaudeConfig(work_dir=tmp_path / "nope", worktree_enabled=True)
    assert config.worktree_enabled is True


def test_parsing_does_not_probe_the_filesystem(tmp_path: Path):
    # from_env parses; app.run() probes. A missing dir survives the parse.
    config = ClaudeConfig.from_env(
        {"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path / "nope")}
    )
    assert config.work_dir == (tmp_path / "nope").resolve()


def test_missing_work_dir_rejected_by_the_probe(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        ClaudeConfig(work_dir=tmp_path / "nope").check_prerequisites()


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
    config.check_prerequisites()  # must not raise
    assert config.worktree_enabled is True
    assert config.work_dir == repo.resolve()


# --- model: opaque pass-through for the CLI's --model flag ---


def test_model_defaults_to_none(tmp_path: Path):
    config = ClaudeConfig.from_env({"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path)})
    assert config.model is None


def test_model_read_from_env_and_trimmed(tmp_path: Path):
    config = ClaudeConfig.from_env(
        {
            "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_CLAUDE_MODEL": "  claude-opus-5  ",
        }
    )
    assert config.model == "claude-opus-5"


def test_model_blank_env_is_none(tmp_path: Path):
    config = ClaudeConfig.from_env(
        {
            "AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_CLAUDE_MODEL": "   ",
        }
    )
    assert config.model is None


def test_model_rejects_blank_on_construction(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_MODEL"):
        ClaudeConfig(work_dir=tmp_path, model="   ")


# --- profiles_from_data: the [claude.profiles] section of the profiles file ---


def _base(tmp_path: Path) -> ClaudeConfig:
    return ClaudeConfig(work_dir=tmp_path, effort="high", model="base-model")


def test_profiles_empty_data_gives_no_profiles(tmp_path: Path):
    assert ClaudeConfig.profiles_from_data({}, _base(tmp_path)) == {}


def test_profile_inherits_unset_fields_from_base(tmp_path: Path):
    base = _base(tmp_path)
    profiles = ClaudeConfig.profiles_from_data({"backend": {}}, base)
    assert profiles == {"backend": base}


def test_profile_overrides_every_field(tmp_path: Path):
    other = tmp_path / "other"
    profiles = ClaudeConfig.profiles_from_data(
        {
            "backend": {
                "work_dir": str(other),
                "permission_mode": "plan",
                "timeout_seconds": 30,
                "worktree_enabled": True,
                "effort": "low",
                "model": "claude-sonnet-5",
                "cli_path": "/opt/claude",
            }
        },
        _base(tmp_path),
    )
    profile = profiles["backend"]
    assert profile.work_dir == other.resolve()
    assert profile.permission_mode == "plan"
    assert profile.timeout_seconds == 30.0
    assert profile.worktree_enabled is True
    assert profile.effort == "low"
    assert profile.model == "claude-sonnet-5"
    assert profile.cli_path == "/opt/claude"


def test_profile_partial_override_keeps_base_for_the_rest(tmp_path: Path):
    base = _base(tmp_path)
    profiles = ClaudeConfig.profiles_from_data(
        {"docs": {"permission_mode": "plan"}}, base
    )
    profile = profiles["docs"]
    assert profile.permission_mode == "plan"
    assert profile.work_dir == base.work_dir
    assert profile.effort == "high"
    assert profile.model == "base-model"


def test_profile_name_with_hyphen_and_underscore_ok(tmp_path: Path):
    profiles = ClaudeConfig.profiles_from_data({"team-a_2": {}}, _base(tmp_path))
    assert set(profiles) == {"team-a_2"}


@pytest.mark.parametrize("name", ["Backend", "team a", "", "team.a"])
def test_profile_invalid_name_rejected(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match="Invalid profile name"):
        ClaudeConfig.profiles_from_data({name: {}}, _base(tmp_path))


def test_profile_reserved_default_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        ClaudeConfig.profiles_from_data({"default": {}}, _base(tmp_path))


def test_profile_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match="must be a table"):
        ClaudeConfig.profiles_from_data({"backend": "oops"}, _base(tmp_path))


def test_profile_unknown_field_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown field.*workdir"):
        ClaudeConfig.profiles_from_data(
            {"backend": {"workdir": "somewhere"}}, _base(tmp_path)
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("work_dir", 123, "non-empty string"),
        ("work_dir", "", "non-empty string"),
        ("permission_mode", "", "non-empty string"),
        ("timeout_seconds", "fast", "must be a number"),
        ("timeout_seconds", True, "must be a number"),
        ("timeout_seconds", float("inf"), "finite"),
        ("worktree_enabled", "yes", "must be a boolean"),
        ("effort", 3, "non-empty string"),
        ("model", 5, "non-empty string"),
        ("model", "  ", "non-empty string"),
        ("cli_path", "", "non-empty string"),
    ],
)
def test_profile_field_type_errors(
    tmp_path: Path, field: str, value: object, match: str
):
    with pytest.raises(ValueError, match=match):
        ClaudeConfig.profiles_from_data({"backend": {field: value}}, _base(tmp_path))


def test_profile_runs_the_same_value_validation_as_base(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CLAUDE_EFFORT"):
        ClaudeConfig.profiles_from_data(
            {"backend": {"effort": "ultra"}}, _base(tmp_path)
        )

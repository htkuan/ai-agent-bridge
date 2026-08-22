"""CodexConfig: env parsing, value validation, profiles, and the prerequisite
probe.

``from_env`` takes an explicit mapping, so these never touch the process
environment (and a developer's local ``.env`` can't reach them).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.codex.config import CodexConfig

# --- from_env ---


def test_from_env_defaults(tmp_path: Path):
    config = CodexConfig.from_env({"AGENT_BRIDGE_CODEX_WORK_DIR": str(tmp_path)})
    assert config.work_dir == tmp_path.resolve()
    assert config.sandbox_mode == "workspace-write"
    assert config.model is None
    assert config.effort is None
    assert config.timeout_seconds == 600.0
    assert config.cli_path == "codex"
    assert config.skip_git_repo_check is False
    assert config.session_map_path is None


def test_from_env_matches_dataclass_defaults(tmp_path: Path):
    # Guards drift between from_env's reader defaults and the dataclass.
    assert CodexConfig.from_env(
        {"AGENT_BRIDGE_CODEX_WORK_DIR": str(tmp_path)}
    ) == CodexConfig(work_dir=tmp_path.resolve())


def test_from_env_reads_all_variables(tmp_path: Path):
    map_path = tmp_path / "map.json"
    config = CodexConfig.from_env(
        {
            "AGENT_BRIDGE_CODEX_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_CODEX_SANDBOX_MODE": "read-only",
            "AGENT_BRIDGE_CODEX_MODEL": "gpt-5.3-codex",
            "AGENT_BRIDGE_CODEX_EFFORT": "high",
            "AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS": "120",
            "AGENT_BRIDGE_CODEX_CLI_PATH": "/opt/bin/codex",
            "AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK": "true",
            "AGENT_BRIDGE_CODEX_SESSION_MAP_PATH": str(map_path),
        }
    )
    assert config.sandbox_mode == "read-only"
    assert config.model == "gpt-5.3-codex"
    assert config.effort == "high"
    assert config.timeout_seconds == 120.0
    assert config.cli_path == "/opt/bin/codex"
    assert config.skip_git_repo_check is True
    assert config.session_map_path == map_path.resolve()


def test_from_env_blank_optionals_mean_unset(tmp_path: Path):
    config = CodexConfig.from_env(
        {
            "AGENT_BRIDGE_CODEX_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_CODEX_MODEL": "",
            "AGENT_BRIDGE_CODEX_EFFORT": "",
            "AGENT_BRIDGE_CODEX_SESSION_MAP_PATH": "",
        }
    )
    assert config.model is None
    assert config.effort is None
    assert config.session_map_path is None


# --- resolved_session_map_path ---


def test_session_map_path_derives_under_work_dir(tmp_path: Path):
    config = CodexConfig(work_dir=tmp_path)
    assert (
        config.resolved_session_map_path
        == tmp_path / ".agent-bridge" / "codex-sessions.json"
    )


def test_explicit_session_map_path_wins(tmp_path: Path):
    explicit = tmp_path / "elsewhere.json"
    config = CodexConfig(work_dir=tmp_path, session_map_path=explicit)
    assert config.resolved_session_map_path == explicit


# --- _validate ---


def test_invalid_sandbox_mode_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_SANDBOX_MODE"):
        CodexConfig(work_dir=tmp_path, sandbox_mode="yolo")


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
def test_valid_sandbox_modes_accepted(tmp_path: Path, mode: str):
    assert CodexConfig(work_dir=tmp_path, sandbox_mode=mode).sandbox_mode == mode


def test_nonpositive_timeout_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS must be positive"):
        CodexConfig(work_dir=tmp_path, timeout_seconds=0)


def test_empty_cli_path_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="CLI_PATH must not be empty"):
        CodexConfig(work_dir=tmp_path, cli_path="")


def test_blank_model_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_MODEL"):
        CodexConfig(work_dir=tmp_path, model="  ")


def test_blank_effort_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_EFFORT"):
        CodexConfig(work_dir=tmp_path, effort=" ")


# --- check_prerequisites ---


def test_check_prerequisites_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_WORK_DIR"):
        CodexConfig(work_dir=tmp_path / "nope").check_prerequisites()


def test_check_prerequisites_rejects_non_git_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK"):
        CodexConfig(work_dir=tmp_path).check_prerequisites()


def test_check_prerequisites_passes_for_git_repo(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    CodexConfig(work_dir=tmp_path).check_prerequisites()


def test_check_prerequisites_passes_with_skip_flag(tmp_path: Path):
    CodexConfig(work_dir=tmp_path, skip_git_repo_check=True).check_prerequisites()


# --- profiles_from_data ---


def test_profiles_inherit_unset_fields_from_base(tmp_path: Path):
    base = CodexConfig(work_dir=tmp_path, sandbox_mode="read-only", effort="low")
    profiles = CodexConfig.profiles_from_data(
        {"fast": {"model": "gpt-5.3-codex"}}, base
    )
    fast = profiles["fast"]
    assert fast.model == "gpt-5.3-codex"
    assert fast.sandbox_mode == "read-only"
    assert fast.effort == "low"
    assert fast.work_dir == tmp_path


def test_profile_sets_every_field(tmp_path: Path):
    other = tmp_path / "other"
    map_path = tmp_path / "map.json"
    profiles = CodexConfig.profiles_from_data(
        {
            "full": {
                "work_dir": str(other),
                "sandbox_mode": "danger-full-access",
                "model": "gpt-5.3-codex",
                "effort": "xhigh",
                "timeout_seconds": 30,
                "cli_path": "/opt/codex",
                "skip_git_repo_check": True,
                "session_map_path": str(map_path),
            }
        },
        CodexConfig(work_dir=tmp_path),
    )
    full = profiles["full"]
    assert full.work_dir == other.resolve()
    assert full.sandbox_mode == "danger-full-access"
    assert full.model == "gpt-5.3-codex"
    assert full.effort == "xhigh"
    assert full.timeout_seconds == 30.0
    assert full.cli_path == "/opt/codex"
    assert full.skip_git_repo_check is True
    assert full.session_map_path == map_path.resolve()


def test_profile_unknown_field_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown field.*codex\.profiles\.x"):
        CodexConfig.profiles_from_data(
            {"x": {"sandbox": "read-only"}}, CodexConfig(work_dir=tmp_path)
        )


def test_profile_invalid_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid profile name"):
        CodexConfig.profiles_from_data({"Bad Name": {}}, CodexConfig(work_dir=tmp_path))


def test_profile_reserved_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        CodexConfig.profiles_from_data({"default": {}}, CodexConfig(work_dir=tmp_path))


def test_profile_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match=r"codex\.profiles\.x must be a table"):
        CodexConfig.profiles_from_data({"x": "oops"}, CodexConfig(work_dir=tmp_path))


def test_profile_skip_flag_must_be_boolean(tmp_path: Path):
    with pytest.raises(
        ValueError, match=r"codex\.profiles\.x\.skip_git_repo_check must be a boolean"
    ):
        CodexConfig.profiles_from_data(
            {"x": {"skip_git_repo_check": "yes"}}, CodexConfig(work_dir=tmp_path)
        )


def test_profile_session_map_path_inherits_when_absent(tmp_path: Path):
    explicit = tmp_path / "map.json"
    base = CodexConfig(work_dir=tmp_path, session_map_path=explicit)
    profiles = CodexConfig.profiles_from_data({"x": {}}, base)
    assert profiles["x"].session_map_path == explicit


def test_profile_validation_runs_on_set_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_CODEX_SANDBOX_MODE"):
        CodexConfig.profiles_from_data(
            {"x": {"sandbox_mode": "yolo"}}, CodexConfig(work_dir=tmp_path)
        )

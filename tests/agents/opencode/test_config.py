"""OpencodeConfig: env parsing, value validation, profiles, and the
prerequisite probe.

``from_env`` takes an explicit mapping, so these never touch the process
environment (and a developer's local ``.env`` can't reach them).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.opencode.config import OpencodeConfig

# --- from_env ---


def test_from_env_defaults(tmp_path: Path):
    config = OpencodeConfig.from_env({"AGENT_BRIDGE_OPENCODE_WORK_DIR": str(tmp_path)})
    assert config.work_dir == tmp_path.resolve()
    assert config.model is None
    assert config.variant is None
    assert config.timeout_seconds == 600.0
    assert config.cli_path == "opencode"
    assert config.session_map_path is None


def test_from_env_matches_dataclass_defaults(tmp_path: Path):
    # Guards drift between from_env's reader defaults and the dataclass.
    assert OpencodeConfig.from_env(
        {"AGENT_BRIDGE_OPENCODE_WORK_DIR": str(tmp_path)}
    ) == OpencodeConfig(work_dir=tmp_path.resolve())


def test_from_env_reads_all_variables(tmp_path: Path):
    map_path = tmp_path / "map.json"
    config = OpencodeConfig.from_env(
        {
            "AGENT_BRIDGE_OPENCODE_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_OPENCODE_MODEL": "anthropic/claude-opus-5",
            "AGENT_BRIDGE_OPENCODE_VARIANT": "max",
            "AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS": "120",
            "AGENT_BRIDGE_OPENCODE_CLI_PATH": "/opt/bin/opencode",
            "AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH": str(map_path),
        }
    )
    assert config.model == "anthropic/claude-opus-5"
    assert config.variant == "max"
    assert config.timeout_seconds == 120.0
    assert config.cli_path == "/opt/bin/opencode"
    assert config.session_map_path == map_path.resolve()


def test_from_env_blank_optionals_mean_unset(tmp_path: Path):
    config = OpencodeConfig.from_env(
        {
            "AGENT_BRIDGE_OPENCODE_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_OPENCODE_MODEL": "",
            "AGENT_BRIDGE_OPENCODE_VARIANT": "",
            "AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH": "",
        }
    )
    assert config.model is None
    assert config.variant is None
    assert config.session_map_path is None


# --- resolved_session_map_path ---


def test_session_map_path_derives_under_work_dir(tmp_path: Path):
    config = OpencodeConfig(work_dir=tmp_path)
    assert (
        config.resolved_session_map_path
        == tmp_path / ".agent-bridge" / "opencode-sessions.json"
    )


def test_explicit_session_map_path_wins(tmp_path: Path):
    explicit = tmp_path / "elsewhere.json"
    config = OpencodeConfig(work_dir=tmp_path, session_map_path=explicit)
    assert config.resolved_session_map_path == explicit


# --- _validate ---


def test_nonpositive_timeout_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS must be positive"):
        OpencodeConfig(work_dir=tmp_path, timeout_seconds=0)


def test_empty_cli_path_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="CLI_PATH must not be empty"):
        OpencodeConfig(work_dir=tmp_path, cli_path="")


def test_blank_model_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_MODEL"):
        OpencodeConfig(work_dir=tmp_path, model="  ")


def test_blank_variant_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_VARIANT"):
        OpencodeConfig(work_dir=tmp_path, variant=" ")


# --- check_prerequisites ---


def test_check_prerequisites_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_OPENCODE_WORK_DIR"):
        OpencodeConfig(work_dir=tmp_path / "nope").check_prerequisites()


def test_check_prerequisites_passes_for_plain_dir(tmp_path: Path):
    # Unlike codex, opencode has no git-repository requirement.
    OpencodeConfig(work_dir=tmp_path).check_prerequisites()


# --- profiles_from_data ---


def test_profiles_inherit_unset_fields_from_base(tmp_path: Path):
    base = OpencodeConfig(work_dir=tmp_path, variant="low")
    profiles = OpencodeConfig.profiles_from_data(
        {"fast": {"model": "anthropic/claude-sonnet-5"}}, base
    )
    fast = profiles["fast"]
    assert fast.model == "anthropic/claude-sonnet-5"
    assert fast.variant == "low"
    assert fast.work_dir == tmp_path


def test_profile_sets_every_field(tmp_path: Path):
    other = tmp_path / "other"
    map_path = tmp_path / "map.json"
    profiles = OpencodeConfig.profiles_from_data(
        {
            "full": {
                "work_dir": str(other),
                "model": "anthropic/claude-opus-5",
                "variant": "max",
                "timeout_seconds": 30,
                "cli_path": "/opt/opencode",
                "session_map_path": str(map_path),
            }
        },
        OpencodeConfig(work_dir=tmp_path),
    )
    full = profiles["full"]
    assert full.work_dir == other.resolve()
    assert full.model == "anthropic/claude-opus-5"
    assert full.variant == "max"
    assert full.timeout_seconds == 30.0
    assert full.cli_path == "/opt/opencode"
    assert full.session_map_path == map_path.resolve()


def test_profile_unknown_field_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown field.*opencode\.profiles\.x"):
        OpencodeConfig.profiles_from_data(
            {"x": {"sandbox_mode": "read-only"}}, OpencodeConfig(work_dir=tmp_path)
        )


def test_profile_invalid_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid profile name"):
        OpencodeConfig.profiles_from_data(
            {"Bad Name": {}}, OpencodeConfig(work_dir=tmp_path)
        )


def test_profile_reserved_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        OpencodeConfig.profiles_from_data(
            {"default": {}}, OpencodeConfig(work_dir=tmp_path)
        )


def test_profile_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match=r"opencode\.profiles\.x must be a table"):
        OpencodeConfig.profiles_from_data(
            {"x": "oops"}, OpencodeConfig(work_dir=tmp_path)
        )


def test_profile_session_map_path_inherits_when_absent(tmp_path: Path):
    explicit = tmp_path / "map.json"
    base = OpencodeConfig(work_dir=tmp_path, session_map_path=explicit)
    profiles = OpencodeConfig.profiles_from_data({"x": {}}, base)
    assert profiles["x"].session_map_path == explicit


def test_profile_validation_runs_on_set_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS must be positive"):
        OpencodeConfig.profiles_from_data(
            {"x": {"timeout_seconds": -1}}, OpencodeConfig(work_dir=tmp_path)
        )

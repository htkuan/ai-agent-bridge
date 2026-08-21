"""PiConfig: env parsing, value validation, profiles, and the prerequisite probe.

``from_env`` takes an explicit mapping, so these never touch the process
environment (and a developer's local ``.env`` can't reach them).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.pi.config import PiConfig

# --- from_env ---


def test_from_env_defaults(tmp_path: Path):
    config = PiConfig.from_env({"AGENT_BRIDGE_PI_WORK_DIR": str(tmp_path)})
    assert config.work_dir == tmp_path.resolve()
    assert config.provider is None
    assert config.model is None
    assert config.thinking is None
    assert config.timeout_seconds == 600.0
    assert config.cli_path == "pi"
    assert config.tools == ()
    assert config.exclude_tools == ()


def test_from_env_matches_dataclass_defaults(tmp_path: Path):
    # Guards drift between from_env's reader defaults and the dataclass.
    assert PiConfig.from_env({"AGENT_BRIDGE_PI_WORK_DIR": str(tmp_path)}) == PiConfig(
        work_dir=tmp_path.resolve()
    )


def test_from_env_reads_all_variables(tmp_path: Path):
    config = PiConfig.from_env(
        {
            "AGENT_BRIDGE_PI_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_PI_PROVIDER": "openai-codex",
            "AGENT_BRIDGE_PI_MODEL": "gpt-5.6-luna",
            "AGENT_BRIDGE_PI_THINKING": "high",
            "AGENT_BRIDGE_PI_TIMEOUT_SECONDS": "120",
            "AGENT_BRIDGE_PI_CLI_PATH": "/opt/bin/pi",
            "AGENT_BRIDGE_PI_TOOLS": "read, grep,ls",
            "AGENT_BRIDGE_PI_EXCLUDE_TOOLS": "bash",
        }
    )
    assert config.provider == "openai-codex"
    assert config.model == "gpt-5.6-luna"
    assert config.thinking == "high"
    assert config.timeout_seconds == 120.0
    assert config.cli_path == "/opt/bin/pi"
    assert config.tools == ("read", "grep", "ls")
    assert config.exclude_tools == ("bash",)


def test_from_env_blank_optionals_mean_unset(tmp_path: Path):
    config = PiConfig.from_env(
        {
            "AGENT_BRIDGE_PI_WORK_DIR": str(tmp_path),
            "AGENT_BRIDGE_PI_PROVIDER": "",
            "AGENT_BRIDGE_PI_MODEL": "",
            "AGENT_BRIDGE_PI_THINKING": "",
        }
    )
    assert config.provider is None
    assert config.model is None
    assert config.thinking is None


# --- _validate ---


def test_invalid_thinking_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_THINKING"):
        PiConfig(work_dir=tmp_path, thinking="turbo")


@pytest.mark.parametrize(
    "level", ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_valid_thinking_levels_accepted(tmp_path: Path, level: str):
    assert PiConfig(work_dir=tmp_path, thinking=level).thinking == level


def test_nonpositive_timeout_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS must be positive"):
        PiConfig(work_dir=tmp_path, timeout_seconds=0)


def test_empty_cli_path_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="CLI_PATH must not be empty"):
        PiConfig(work_dir=tmp_path, cli_path="")


def test_blank_model_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_MODEL"):
        PiConfig(work_dir=tmp_path, model="  ")


def test_blank_provider_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_PROVIDER"):
        PiConfig(work_dir=tmp_path, provider=" ")


def test_blank_tool_entry_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_TOOLS entries"):
        PiConfig(work_dir=tmp_path, tools=("read", " "))


def test_blank_exclude_tool_entry_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_EXCLUDE_TOOLS entries"):
        PiConfig(work_dir=tmp_path, exclude_tools=("",))


# --- check_prerequisites ---


def test_check_prerequisites_passes_for_existing_dir(tmp_path: Path):
    PiConfig(work_dir=tmp_path).check_prerequisites()


def test_check_prerequisites_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_WORK_DIR"):
        PiConfig(work_dir=tmp_path / "nope").check_prerequisites()


# --- profiles_from_data ---


def test_profiles_inherit_unset_fields_from_base(tmp_path: Path):
    base = PiConfig(work_dir=tmp_path, provider="openai-codex", thinking="low")
    profiles = PiConfig.profiles_from_data({"fast": {"model": "gpt-5.6-luna"}}, base)
    fast = profiles["fast"]
    assert fast.model == "gpt-5.6-luna"
    assert fast.provider == "openai-codex"
    assert fast.thinking == "low"
    assert fast.work_dir == tmp_path


def test_profile_sets_every_field(tmp_path: Path):
    other = tmp_path / "other"
    profiles = PiConfig.profiles_from_data(
        {
            "full": {
                "work_dir": str(other),
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "thinking": "max",
                "timeout_seconds": 30,
                "cli_path": "/opt/pi",
                "tools": ["read", "grep"],
                "exclude_tools": ["bash"],
            }
        },
        PiConfig(work_dir=tmp_path),
    )
    full = profiles["full"]
    assert full.work_dir == other.resolve()
    assert full.provider == "anthropic"
    assert full.model == "claude-sonnet-5"
    assert full.thinking == "max"
    assert full.timeout_seconds == 30.0
    assert full.cli_path == "/opt/pi"
    assert full.tools == ("read", "grep")
    assert full.exclude_tools == ("bash",)


def test_profile_unknown_field_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown field.*pi\.profiles\.x"):
        PiConfig.profiles_from_data(
            {"x": {"sandbox": "read-only"}}, PiConfig(work_dir=tmp_path)
        )


def test_profile_invalid_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid profile name"):
        PiConfig.profiles_from_data({"Bad Name": {}}, PiConfig(work_dir=tmp_path))


def test_profile_reserved_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        PiConfig.profiles_from_data({"default": {}}, PiConfig(work_dir=tmp_path))


def test_profile_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match=r"pi\.profiles\.x must be a table"):
        PiConfig.profiles_from_data({"x": "oops"}, PiConfig(work_dir=tmp_path))


def test_profile_tools_must_be_string_array(tmp_path: Path):
    with pytest.raises(ValueError, match=r"pi\.profiles\.x\.tools must be an array"):
        PiConfig.profiles_from_data(
            {"x": {"tools": "read,grep"}}, PiConfig(work_dir=tmp_path)
        )


def test_profile_tools_entries_must_be_non_empty(tmp_path: Path):
    with pytest.raises(ValueError, match="non-empty strings"):
        PiConfig.profiles_from_data(
            {"x": {"tools": ["read", " "]}}, PiConfig(work_dir=tmp_path)
        )


def test_profile_validation_runs_on_inherited_and_set_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PI_THINKING"):
        PiConfig.profiles_from_data(
            {"x": {"thinking": "warp"}}, PiConfig(work_dir=tmp_path)
        )

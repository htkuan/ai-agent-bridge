"""HeartbeatConfig: ``from_env_optional`` decides on/off, ``from_env`` parses.

A constructed HeartbeatConfig is always a runnable one — "disabled" is
``None``, not a config with an ``enabled=False`` flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.platforms.heartbeat.config import HeartbeatConfig

_ENABLED = {
    "AGENT_BRIDGE_HEARTBEAT_ENABLED": "true",
    "AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES": "15",
    "AGENT_BRIDGE_HEARTBEAT_PROMPT": "go",
}


def test_disabled_by_default():
    assert HeartbeatConfig.from_env_optional({}) is None


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_explicitly_disabled(value: str):
    assert (
        HeartbeatConfig.from_env_optional(
            {**_ENABLED, "AGENT_BRIDGE_HEARTBEAT_ENABLED": value}
        )
        is None
    )


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_truthy_spellings_all_enable(value: str):
    config = HeartbeatConfig.from_env_optional(
        {**_ENABLED, "AGENT_BRIDGE_HEARTBEAT_ENABLED": value}
    )
    assert config is not None


def test_unparseable_enabled_flag_is_rejected():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HEARTBEAT_ENABLED"):
        HeartbeatConfig.from_env_optional(
            {**_ENABLED, "AGENT_BRIDGE_HEARTBEAT_ENABLED": "maybe"}
        )


def test_from_env_reads_all_variables(tmp_path: Path):
    config = HeartbeatConfig.from_env(
        {
            **_ENABLED,
            "AGENT_BRIDGE_HEARTBEAT_STATE_PATH": str(tmp_path / "h.json"),
            "AGENT_BRIDGE_HEARTBEAT_AGENT": "night-shift",
        }
    )
    assert config.interval_minutes == 15
    assert config.prompt == "go"
    assert config.state_path == tmp_path / "h.json"
    assert config.agent == "night-shift"


def test_agent_defaults_to_none():
    assert HeartbeatConfig.from_env(_ENABLED).agent is None


def test_blank_agent_means_unset():
    config = HeartbeatConfig.from_env({**_ENABLED, "AGENT_BRIDGE_HEARTBEAT_AGENT": ""})
    assert config.agent is None


def test_state_path_defaults():
    assert HeartbeatConfig.from_env(_ENABLED).state_path == Path("./heartbeat.json")


def test_enabled_requires_prompt():
    env = {k: v for k, v in _ENABLED.items() if not k.endswith("PROMPT")}
    with pytest.raises(ValueError, match="PROMPT"):
        HeartbeatConfig.from_env_optional(env)


def test_enabled_requires_positive_interval():
    with pytest.raises(ValueError, match="INTERVAL_MINUTES"):
        HeartbeatConfig.from_env_optional(
            {**_ENABLED, "AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES": "0"}
        )


def test_direct_construction_is_validated_too():
    with pytest.raises(ValueError, match="INTERVAL_MINUTES"):
        HeartbeatConfig(interval_minutes=-1, prompt="go")

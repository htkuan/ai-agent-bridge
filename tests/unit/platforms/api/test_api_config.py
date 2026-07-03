from __future__ import annotations

import pytest

from agent_bridge.config_loader import ConfigSource
from agent_bridge.platforms.api.config import DEFAULT_HOST, DEFAULT_PORT, ApiConfig


def test_disabled_by_default():
    config = ApiConfig.from_source(ConfigSource({}, env={}))
    assert config.enabled is False


def test_disabled_ignores_other_keys():
    # Not enabled → other keys are not read or validated (invalid port here).
    source = ConfigSource({"platforms": {"api": {"port": 99999, "host": ""}}}, env={})
    config = ApiConfig.from_source(source)
    assert config.enabled is False
    assert config.port == DEFAULT_PORT


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_enabled_truthy_values(value):
    source = ConfigSource({}, env={"AGENT_BRIDGE_API_ENABLED": value})
    assert ApiConfig.from_source(source).enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "nope"])
def test_enabled_falsy_values(value):
    source = ConfigSource({}, env={"AGENT_BRIDGE_API_ENABLED": value})
    assert ApiConfig.from_source(source).enabled is False


def test_defaults_when_enabled():
    source = ConfigSource({"platforms": {"api": {"enabled": True}}}, env={})
    config = ApiConfig.from_source(source)
    assert config.host == DEFAULT_HOST == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 8081
    assert config.auth_token == ""


def test_from_source_yaml_keys():
    source = ConfigSource(
        {
            "platforms": {
                "api": {
                    "enabled": True,
                    "host": "0.0.0.0",
                    "port": 9000,
                    "auth_token": "s3cret",
                }
            }
        },
        env={},
    )
    config = ApiConfig.from_source(source)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.auth_token == "s3cret"


def test_env_overrides_yaml():
    source = ConfigSource(
        {"platforms": {"api": {"enabled": True, "port": 9000}}},
        env={"AGENT_BRIDGE_API_PORT": "7070", "AGENT_BRIDGE_API_AUTH_TOKEN": "tok"},
    )
    config = ApiConfig.from_source(source)
    assert config.port == 7070
    assert config.auth_token == "tok"


def test_port_out_of_range_raises():
    source = ConfigSource(
        {},
        env={"AGENT_BRIDGE_API_ENABLED": "true", "AGENT_BRIDGE_API_PORT": "65536"},
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_API_PORT"):
        ApiConfig.from_source(source)


def test_port_zero_allowed_for_ephemeral_binding():
    source = ConfigSource(
        {},
        env={"AGENT_BRIDGE_API_ENABLED": "true", "AGENT_BRIDGE_API_PORT": "0"},
    )
    assert ApiConfig.from_source(source).port == 0


def test_empty_host_raises():
    source = ConfigSource(
        {"platforms": {"api": {"enabled": True, "host": ""}}},
        env={},
    )
    with pytest.raises(ValueError, match="AGENT_BRIDGE_API_HOST"):
        ApiConfig.from_source(source)


def test_from_env_reads_process_environment(monkeypatch, clean_agent_bridge_env):
    monkeypatch.setenv("AGENT_BRIDGE_API_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRIDGE_API_AUTH_TOKEN", "env-tok")
    config = ApiConfig.from_env()
    assert config.enabled is True
    assert config.auth_token == "env-tok"

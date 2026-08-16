"""HttpConfig: env parsing and value checks."""

from __future__ import annotations

import pytest

from agent_bridge.server.config import DEFAULT_HOST, DEFAULT_PORT, HttpConfig

# --- from_env / from_env_optional ---


def test_optional_is_none_when_not_enabled():
    assert HttpConfig.from_env_optional({}) is None
    assert HttpConfig.from_env_optional({"AGENT_BRIDGE_HTTP_ENABLED": "false"}) is None


def test_optional_enabled_returns_config():
    config = HttpConfig.from_env_optional({"AGENT_BRIDGE_HTTP_ENABLED": "true"})
    assert config == HttpConfig()


def test_env_defaults_match_dataclass_defaults():
    assert HttpConfig.from_env({}) == HttpConfig()
    assert HttpConfig() == HttpConfig(host=DEFAULT_HOST, port=DEFAULT_PORT)


def test_from_env_reads_host_and_port():
    config = HttpConfig.from_env(
        {"AGENT_BRIDGE_HTTP_HOST": "0.0.0.0", "AGENT_BRIDGE_HTTP_PORT": "9000"}  # noqa: S104
    )
    assert config == HttpConfig(host="0.0.0.0", port=9000)  # noqa: S104


def test_unparseable_port_raises():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_PORT"):
        HttpConfig.from_env({"AGENT_BRIDGE_HTTP_PORT": "eighty"})


# --- validation ---


def test_port_out_of_range_raises():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_PORT"):
        HttpConfig(port=65536)
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_PORT"):
        HttpConfig(port=-1)


def test_port_zero_is_allowed():
    assert HttpConfig(port=0).port == 0


def test_empty_host_raises():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_HOST"):
        HttpConfig(host="")

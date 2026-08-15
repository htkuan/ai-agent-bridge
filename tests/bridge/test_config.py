"""Bridge-layer configs: one per component, aggregated by BridgeConfig."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.bridge.config import (
    BridgeConfig,
    DedupeConfig,
    RouterConfig,
    SessionConfig,
)

_ALL = {
    "AGENT_BRIDGE_SESSION_STORE_PATH": "./data/store.json",
    "AGENT_BRIDGE_SESSION_TTL_HOURS": "1.5",
    "AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS": "3",
    "AGENT_BRIDGE_DEDUPE_TTL_SECONDS": "600",
    "AGENT_BRIDGE_DEDUPE_MAX_ENTRIES": "64",
    "AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD": "5",
}


# --- from_env ---


def test_from_env_defaults():
    config = BridgeConfig.from_env({})
    assert config.session.store_path == Path("./sessions.json")
    assert config.session.ttl_hours == 72.0
    assert config.router.max_concurrent_sessions == 5
    assert config.dedupe.ttl_seconds == 0.0
    assert config.dedupe.max_entries == 512
    assert config.dedupe.simhash_threshold == 0


def test_from_env_reads_all_variables():
    config = BridgeConfig.from_env(_ALL)
    assert config.session.store_path == Path("./data/store.json")
    assert config.session.ttl_hours == 1.5
    assert config.router.max_concurrent_sessions == 3
    assert config.dedupe.ttl_seconds == 600.0
    assert config.dedupe.max_entries == 64
    assert config.dedupe.simhash_threshold == 5


def test_env_defaults_match_dataclass_defaults():
    # Guards the drift that let max_concurrent_sessions default to 10 in code
    # and 5 in the environment.
    assert BridgeConfig.from_env({}) == BridgeConfig()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AGENT_BRIDGE_SESSION_TTL_HOURS", "0"),
        ("AGENT_BRIDGE_SESSION_TTL_HOURS", "-1"),
        ("AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", "0"),
        ("AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", "-2"),
        ("AGENT_BRIDGE_DEDUPE_TTL_SECONDS", "-1"),
        ("AGENT_BRIDGE_DEDUPE_MAX_ENTRIES", "0"),
        ("AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD", "-3"),
    ],
)
def test_validate_rejects_out_of_range(var: str, value: str):
    with pytest.raises(ValueError, match=var):
        BridgeConfig.from_env({var: value})


# --- validation runs on direct construction too ---


def test_session_ttl_must_be_positive():
    with pytest.raises(ValueError, match="TTL_HOURS"):
        SessionConfig(ttl_hours=0)


def test_router_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="MAX_CONCURRENT_SESSIONS"):
        RouterConfig(max_concurrent_sessions=0)


def test_dedupe_max_entries_must_be_positive():
    with pytest.raises(ValueError, match="DEDUPE_MAX_ENTRIES"):
        DedupeConfig(ttl_seconds=10.0, max_entries=0)


# --- DedupeConfig.enabled: the one place "off" is spelled ---


def test_dedupe_zero_ttl_is_valid_but_disabled():
    assert DedupeConfig(ttl_seconds=0.0).enabled is False


def test_dedupe_positive_ttl_is_enabled():
    assert DedupeConfig(ttl_seconds=0.5).enabled is True

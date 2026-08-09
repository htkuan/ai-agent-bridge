from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.config import BridgeConfig

_ENV_VARS = [
    "AGENT_BRIDGE_SESSION_STORE_PATH",
    "AGENT_BRIDGE_SESSION_TTL_HOURS",
    "AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS",
    "AGENT_BRIDGE_DEDUPE_TTL_SECONDS",
    "AGENT_BRIDGE_DEDUPE_MAX_ENTRIES",
    "AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_from_env_defaults():
    config = BridgeConfig.from_env()
    assert config.session_store_path == Path("./sessions.json")
    assert config.session_ttl_hours == 72.0
    assert config.max_concurrent_sessions == 5
    assert config.dedupe_ttl_seconds == 0.0
    assert config.dedupe_max_entries == 512
    assert config.dedupe_simhash_threshold == 0


def test_from_env_reads_all_variables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_BRIDGE_SESSION_STORE_PATH", "./data/store.json")
    monkeypatch.setenv("AGENT_BRIDGE_SESSION_TTL_HOURS", "1.5")
    monkeypatch.setenv("AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", "3")
    monkeypatch.setenv("AGENT_BRIDGE_DEDUPE_TTL_SECONDS", "600")
    monkeypatch.setenv("AGENT_BRIDGE_DEDUPE_MAX_ENTRIES", "64")
    monkeypatch.setenv("AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD", "5")
    config = BridgeConfig.from_env()
    assert config.session_store_path == Path("./data/store.json")
    assert config.session_ttl_hours == 1.5
    assert config.max_concurrent_sessions == 3
    assert config.dedupe_ttl_seconds == 600.0
    assert config.dedupe_max_entries == 64
    assert config.dedupe_simhash_threshold == 5


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
def test_validate_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        BridgeConfig.from_env()


def test_dedupe_zero_means_disabled_and_is_valid():
    config = BridgeConfig(dedupe_ttl_seconds=0.0, dedupe_simhash_threshold=0)
    config._validate()  # must not raise

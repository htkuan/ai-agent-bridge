from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_float,
    env_int,
    env_path,
)


@dataclass(frozen=True)
class SessionConfig:
    """Settings for ``SessionManager``."""

    store_path: Path = field(default_factory=lambda: Path("./sessions.json"))
    ttl_hours: float = 72.0

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> SessionConfig:
        return cls(
            store_path=env_path(
                env, "AGENT_BRIDGE_SESSION_STORE_PATH", "./sessions.json"
            ),
            ttl_hours=env_float(env, "AGENT_BRIDGE_SESSION_TTL_HOURS", 72.0),
        )

    def _validate(self) -> None:
        if self.ttl_hours <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_SESSION_TTL_HOURS must be positive, got {self.ttl_hours}"
            )


@dataclass(frozen=True)
class RouterConfig:
    """Settings for ``Bridge`` — the routing/concurrency layer itself."""

    max_concurrent_sessions: int = 5

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> RouterConfig:
        return cls(
            max_concurrent_sessions=env_int(
                env, "AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", 5
            ),
        )

    def _validate(self) -> None:
        if self.max_concurrent_sessions <= 0:
            raise ValueError(
                "AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS must be positive, "
                f"got {self.max_concurrent_sessions}"
            )


@dataclass(frozen=True)
class DedupeConfig:
    """Settings for ``PromptDedupeCache``. ``ttl_seconds`` of 0 disables the
    feature — ``enabled`` is the single place that spelling is decided."""

    ttl_seconds: float = 0.0
    max_entries: int = 512
    # SimHash Hamming-distance threshold. 0 ⇒ exact canonical match only.
    # Positive values enable fuzzy match (typical range 3-10).
    simhash_threshold: int = 0

    def __post_init__(self) -> None:
        self._validate()

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> DedupeConfig:
        return cls(
            ttl_seconds=env_float(env, "AGENT_BRIDGE_DEDUPE_TTL_SECONDS", 0.0),
            max_entries=env_int(env, "AGENT_BRIDGE_DEDUPE_MAX_ENTRIES", 512),
            simhash_threshold=env_int(env, "AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD", 0),
        )

    def _validate(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError(
                "AGENT_BRIDGE_DEDUPE_TTL_SECONDS must be >= 0 "
                f"(0 disables the feature), got {self.ttl_seconds}"
            )
        if self.max_entries <= 0:
            raise ValueError(
                "AGENT_BRIDGE_DEDUPE_MAX_ENTRIES must be positive, "
                f"got {self.max_entries}"
            )
        if self.simhash_threshold < 0:
            raise ValueError(
                "AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD must be >= 0 "
                f"(0 disables fuzzy match), got {self.simhash_threshold}"
            )


@dataclass(frozen=True)
class BridgeConfig:
    """Aggregate of every bridge-layer component config (platform-agnostic,
    agent-agnostic)."""

    session: SessionConfig = field(default_factory=SessionConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> BridgeConfig:
        return cls(
            session=SessionConfig.from_env(env),
            router=RouterConfig.from_env(env),
            dedupe=DedupeConfig.from_env(env),
        )

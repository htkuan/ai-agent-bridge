from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_bool,
    env_int,
    env_path,
    env_str,
)


@dataclass(frozen=True)
class HeartbeatConfig:
    """Present only when the heartbeat platform is on — ``enabled`` lives in
    ``AppConfig.heartbeat is None``, not in a field here, so every constructed
    config is a runnable one."""

    interval_minutes: int
    prompt: str
    state_path: Path = field(default_factory=lambda: Path("./heartbeat.json"))
    # Named agent profile the ticks route to; None = the bridge's default.
    # AppConfig validates the name against the profile registry at boot.
    agent: str | None = None

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> HeartbeatConfig:
        return cls(
            interval_minutes=env_int(env, "AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", 0),
            prompt=env_str(env, "AGENT_BRIDGE_HEARTBEAT_PROMPT", ""),
            state_path=env_path(
                env, "AGENT_BRIDGE_HEARTBEAT_STATE_PATH", "./heartbeat.json"
            ),
            agent=env_str(env, "AGENT_BRIDGE_HEARTBEAT_AGENT", "") or None,
        )

    @classmethod
    def from_env_optional(cls, env: Env = PROCESS_ENV) -> HeartbeatConfig | None:
        if not env_bool(env, "AGENT_BRIDGE_HEARTBEAT_ENABLED", False):
            return None
        return cls.from_env(env)

    def _validate(self) -> None:
        if self.interval_minutes <= 0:
            raise ValueError(
                "AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES must be positive, "
                f"got {self.interval_minutes}"
            )
        if not self.prompt:
            raise ValueError(
                "AGENT_BRIDGE_HEARTBEAT_PROMPT is required when heartbeat is enabled"
            )

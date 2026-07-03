from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_bridge.config_loader import ConfigSource

_TRUTHY = {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    interval_minutes: int = 0
    prompt: str = ""
    state_path: Path = Path("./heartbeat.json")

    @classmethod
    def from_env(cls) -> HeartbeatConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> HeartbeatConfig:
        enabled = (
            source.get(
                "AGENT_BRIDGE_HEARTBEAT_ENABLED", "platforms.heartbeat.enabled", "false"
            ).lower()
            in _TRUTHY
        )
        if not enabled:
            return cls()

        config = cls(
            enabled=True,
            interval_minutes=int(
                source.get(
                    "AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES",
                    "platforms.heartbeat.interval_minutes",
                    "0",
                )
            ),
            prompt=source.get(
                "AGENT_BRIDGE_HEARTBEAT_PROMPT", "platforms.heartbeat.prompt", ""
            ),
            state_path=Path(
                source.get(
                    "AGENT_BRIDGE_HEARTBEAT_STATE_PATH",
                    "platforms.heartbeat.state_path",
                    "./heartbeat.json",
                )
            ),
        )
        config._validate()
        return config

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

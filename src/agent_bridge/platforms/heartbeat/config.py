from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    interval_minutes: int = 0
    prompt: str = ""
    state_path: Path = Path("./heartbeat.json")

    @classmethod
    def from_env(cls) -> HeartbeatConfig:
        load_dotenv()

        enabled = os.environ.get("AGENT_BRIDGE_HEARTBEAT_ENABLED", "false").lower() == "true"
        if not enabled:
            return cls()

        config = cls(
            enabled=True,
            interval_minutes=int(
                os.environ.get("AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES", "0")
            ),
            prompt=os.environ.get("AGENT_BRIDGE_HEARTBEAT_PROMPT", ""),
            state_path=Path(
                os.environ.get("AGENT_BRIDGE_HEARTBEAT_STATE_PATH", "./heartbeat.json")
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

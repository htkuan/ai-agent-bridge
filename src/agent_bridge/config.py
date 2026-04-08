from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BridgeConfig:
    """Core bridge settings (platform-agnostic, agent-agnostic)."""

    session_store_path: Path = field(default_factory=lambda: Path("./sessions.json"))
    session_ttl_hours: float = 72.0
    max_concurrent_sessions: int = 10

    @classmethod
    def from_env(cls) -> BridgeConfig:
        load_dotenv()

        config = cls(
            session_store_path=Path(
                os.environ.get("AGENT_BRIDGE_SESSION_STORE_PATH", "./sessions.json")
            ),
            session_ttl_hours=float(os.environ.get("AGENT_BRIDGE_SESSION_TTL_HOURS", "72")),
            max_concurrent_sessions=int(
                os.environ.get("AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS", "5")
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if self.session_ttl_hours <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_SESSION_TTL_HOURS must be positive, got {self.session_ttl_hours}"
            )
        if self.max_concurrent_sessions <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS must be positive, got {self.max_concurrent_sessions}"
            )

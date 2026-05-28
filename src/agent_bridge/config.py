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
    # Cross-session prompt dedupe. 0 disables the feature entirely.
    dedupe_ttl_seconds: float = 0.0
    dedupe_max_entries: int = 512

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
            dedupe_ttl_seconds=float(
                os.environ.get("AGENT_BRIDGE_DEDUPE_TTL_SECONDS", "0")
            ),
            dedupe_max_entries=int(
                os.environ.get("AGENT_BRIDGE_DEDUPE_MAX_ENTRIES", "512")
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
        if self.dedupe_ttl_seconds < 0:
            raise ValueError(
                "AGENT_BRIDGE_DEDUPE_TTL_SECONDS must be >= 0 "
                f"(0 disables the feature), got {self.dedupe_ttl_seconds}"
            )
        if self.dedupe_max_entries <= 0:
            raise ValueError(
                "AGENT_BRIDGE_DEDUPE_MAX_ENTRIES must be positive, "
                f"got {self.dedupe_max_entries}"
            )

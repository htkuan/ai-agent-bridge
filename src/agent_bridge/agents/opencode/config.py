from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.config_loader import ConfigSource


@dataclass(frozen=True)
class OpencodeConfig:
    work_dir: Path
    model: str | None = None
    timeout_seconds: float = 600.0
    session_map_path: Path = field(default_factory=lambda: Path("./opencode-sessions.json"))

    @classmethod
    def from_env(cls) -> OpencodeConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> OpencodeConfig:
        model = (
            source.get("AGENT_BRIDGE_OPENCODE_MODEL", "agents.opencode.model", "") or ""
        ).strip()
        config = cls(
            work_dir=Path(
                source.get("AGENT_BRIDGE_OPENCODE_WORK_DIR", "agents.opencode.work_dir", ".")
            ).resolve(),
            model=model or None,
            timeout_seconds=float(
                source.get(
                    "AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS",
                    "agents.opencode.timeout_seconds",
                    "600",
                )
            ),
            session_map_path=Path(
                source.get(
                    "AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH",
                    "agents.opencode.session_map_path",
                    "./opencode-sessions.json",
                )
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_OPENCODE_WORK_DIR does not exist or is not a directory: "
                f"{self.work_dir}"
            )
        if self.model is not None and "/" not in self.model:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_OPENCODE_MODEL: {self.model!r}. "
                "Must use the provider/model form (e.g. anthropic/claude-sonnet-4-5)"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                "AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS must be positive, "
                f"got {self.timeout_seconds}"
            )

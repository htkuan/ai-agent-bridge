from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.config_loader import ConfigSource

VALID_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}


@dataclass(frozen=True)
class CodexConfig:
    work_dir: Path
    model: str | None = None
    sandbox: str = "workspace-write"
    timeout_seconds: float = 600.0
    session_map_path: Path = field(
        default_factory=lambda: Path("./codex-sessions.json")
    )

    @classmethod
    def from_env(cls) -> CodexConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> CodexConfig:
        model = (
            source.get("AGENT_BRIDGE_CODEX_MODEL", "agents.codex.model", "") or ""
        ).strip()
        config = cls(
            work_dir=Path(
                source.get("AGENT_BRIDGE_CODEX_WORK_DIR", "agents.codex.work_dir", ".")
            ).resolve(),
            model=model or None,
            sandbox=source.get(
                "AGENT_BRIDGE_CODEX_SANDBOX", "agents.codex.sandbox", "workspace-write"
            ),
            timeout_seconds=float(
                source.get(
                    "AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS",
                    "agents.codex.timeout_seconds",
                    "600",
                )
            ),
            session_map_path=Path(
                source.get(
                    "AGENT_BRIDGE_CODEX_SESSION_MAP_PATH",
                    "agents.codex.session_map_path",
                    "./codex-sessions.json",
                )
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_WORK_DIR does not exist or is not a directory: {self.work_dir}"
            )
        if self.sandbox not in VALID_SANDBOX_MODES:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CODEX_SANDBOX: {self.sandbox!r}. "
                f"Must be one of: {', '.join(sorted(VALID_SANDBOX_MODES))}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS must be positive, got {self.timeout_seconds}"
            )

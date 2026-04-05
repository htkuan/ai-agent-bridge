from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
    "dangerously-skip-permissions",
}


@dataclass(frozen=True)
class ClaudeConfig:
    work_dir: Path
    permission_mode: str = "acceptEdits"
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> ClaudeConfig:
        load_dotenv()

        config = cls(
            work_dir=Path(os.environ.get("AGENT_BRIDGE_CLAUDE_WORK_DIR", ".")).resolve(),
            permission_mode=os.environ.get("AGENT_BRIDGE_CLAUDE_PERMISSION_MODE", "acceptEdits"),
            timeout_seconds=float(os.environ.get("AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS", "300")),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"CLAUDE_WORK_DIR does not exist or is not a directory: {self.work_dir}"
            )
        if self.permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"Invalid CLAUDE_PERMISSION_MODE: {self.permission_mode!r}. "
                f"Must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"CLAUDE_TIMEOUT_SECONDS must be positive, got {self.timeout_seconds}"
            )

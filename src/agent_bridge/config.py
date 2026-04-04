from __future__ import annotations

import os
from dataclasses import dataclass, field
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
class Config:
    slack_bot_token: str
    slack_app_token: str
    claude_work_dir: Path = field(default_factory=lambda: Path.cwd())
    claude_permission_mode: str = "acceptEdits"
    session_store_path: Path = field(default_factory=lambda: Path("./sessions.json"))
    session_ttl_hours: float = 72.0
    claude_timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not slack_bot_token or not slack_app_token:
            raise ValueError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN environment variables are required"
            )

        config = cls(
            slack_bot_token=slack_bot_token,
            slack_app_token=slack_app_token,
            claude_work_dir=Path(os.environ.get("CLAUDE_WORK_DIR", ".")).resolve(),
            claude_permission_mode=os.environ.get(
                "CLAUDE_PERMISSION_MODE", "acceptEdits"
            ),
            session_store_path=Path(
                os.environ.get("SESSION_STORE_PATH", "./sessions.json")
            ),
            session_ttl_hours=float(os.environ.get("SESSION_TTL_HOURS", "72")),
            claude_timeout_seconds=float(
                os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300")
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.claude_work_dir.is_dir():
            raise ValueError(
                f"CLAUDE_WORK_DIR does not exist or is not a directory: {self.claude_work_dir}"
            )
        if self.claude_permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"Invalid CLAUDE_PERMISSION_MODE: {self.claude_permission_mode!r}. "
                f"Must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
            )
        if self.session_ttl_hours <= 0:
            raise ValueError(
                f"SESSION_TTL_HOURS must be positive, got {self.session_ttl_hours}"
            )
        if self.claude_timeout_seconds <= 0:
            raise ValueError(
                f"CLAUDE_TIMEOUT_SECONDS must be positive, got {self.claude_timeout_seconds}"
            )

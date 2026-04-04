from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    claude_work_dir: Path = field(default_factory=lambda: Path.cwd())
    claude_permission_mode: str = "acceptEdits"
    session_store_path: Path = field(default_factory=lambda: Path("./sessions.json"))
    session_ttl_hours: float = 72.0

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not slack_bot_token or not slack_app_token:
            raise ValueError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN environment variables are required"
            )

        return cls(
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
        )

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str

    @classmethod
    def from_env(cls) -> SlackConfig:
        load_dotenv()

        bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not bot_token or not app_token:
            raise ValueError(
                "SLACK_BOT_TOKEN and SLACK_APP_TOKEN environment variables are required"
            )
        return cls(bot_token=bot_token, app_token=app_token)

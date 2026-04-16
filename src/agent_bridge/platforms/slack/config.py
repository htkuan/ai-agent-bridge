from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str
    startup_notify_channel: str | None = None
    startup_notify_message: str | None = None

    @classmethod
    def from_env(cls) -> SlackConfig:
        load_dotenv()

        bot_token = os.environ.get("AGENT_BRIDGE_SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("AGENT_BRIDGE_SLACK_APP_TOKEN", "")
        if not bot_token or not app_token:
            raise ValueError(
                "AGENT_BRIDGE_SLACK_BOT_TOKEN and AGENT_BRIDGE_SLACK_APP_TOKEN "
                "environment variables are required"
            )
        return cls(
            bot_token=bot_token,
            app_token=app_token,
            startup_notify_channel=os.environ.get("AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL"),
            startup_notify_message=os.environ.get("AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE"),
        )

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Default reply sent when a message arrives from a channel outside the
# allow-list. Override via AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE.
DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE = (
    "Sorry, I'm not available in this channel. "
    "Please contact the administrator if you think I should be."
)


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str
    startup_notify_channel: str | None = None
    startup_notify_message: str | None = None
    # Allow-list of channel names. Empty = allow every channel. When non-empty,
    # only messages from channels whose name is listed reach the agent; all
    # others (including DMs, which have no name) get channel_not_allowed_message.
    allow_channels: frozenset[str] = frozenset()
    channel_not_allowed_message: str = DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE

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
        raw_channels = os.environ.get("AGENT_BRIDGE_SLACK_ALLOW_CHANNELS", "")
        allow_channels = frozenset(
            _normalize_channel(name)
            for name in raw_channels.split(",")
            if name.strip()
        )
        return cls(
            bot_token=bot_token,
            app_token=app_token,
            startup_notify_channel=os.environ.get("AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL"),
            startup_notify_message=os.environ.get("AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE"),
            allow_channels=allow_channels,
            channel_not_allowed_message=os.environ.get(
                "AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE",
                DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
            ),
        )


def _normalize_channel(name: str) -> str:
    return name.strip().lstrip("#").lower()

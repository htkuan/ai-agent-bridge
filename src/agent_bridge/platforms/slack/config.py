from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.config_loader import ConfigSource

# Default reply sent when a message arrives from a channel outside the
# allow-list. Override via AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE.
DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE = (
    "Sorry, I'm not available in this channel. "
    "Please contact the administrator if you think I should be."
)

_TRUTHY = {"true", "1", "yes", "on"}


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
    # When True, append a usage/cost footer to the final agent reply. The
    # template (if set) is rendered with {placeholder} substitution; otherwise
    # a built-in default layout is used.
    usage_report_enabled: bool = False
    usage_report_template: str | None = None

    @classmethod
    def from_env(cls) -> SlackConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> SlackConfig:
        bot_token = source.get("AGENT_BRIDGE_SLACK_BOT_TOKEN", "platforms.slack.bot_token", "")
        app_token = source.get("AGENT_BRIDGE_SLACK_APP_TOKEN", "platforms.slack.app_token", "")
        if not bot_token or not app_token:
            raise ValueError(
                "AGENT_BRIDGE_SLACK_BOT_TOKEN and AGENT_BRIDGE_SLACK_APP_TOKEN "
                "(platforms.slack.bot_token / app_token) are required"
            )
        raw_channels = source.get(
            "AGENT_BRIDGE_SLACK_ALLOW_CHANNELS", "platforms.slack.allow_channels", ""
        )
        allow_channels = frozenset(
            _normalize_channel(name) for name in raw_channels.split(",") if name.strip()
        )
        return cls(
            bot_token=bot_token,
            app_token=app_token,
            startup_notify_channel=source.get(
                "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL",
                "platforms.slack.startup_notify_channel",
            ),
            startup_notify_message=source.get(
                "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE",
                "platforms.slack.startup_notify_message",
            ),
            allow_channels=allow_channels,
            channel_not_allowed_message=source.get(
                "AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE",
                "platforms.slack.channel_not_allowed_message",
                DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
            ),
            usage_report_enabled=source.get(
                "AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED",
                "platforms.slack.usage_report.enabled",
                "false",
            ).lower()
            in _TRUTHY,
            usage_report_template=source.get(
                "AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE",
                "platforms.slack.usage_report.template",
            )
            or None,
        )


def _normalize_channel(name: str) -> str:
    return name.strip().lstrip("#").lower()

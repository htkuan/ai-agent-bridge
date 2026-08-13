from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_bool,
    env_csv,
    env_str,
    env_str_or_none,
)

# Default reply sent when a message arrives from a channel outside the
# allow-list. Override via AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE.
DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE = (
    "Sorry, I'm not available in this channel. "
    "Please contact the administrator if you think I should be."
)

# Minimum interval between Slack message updates (seconds).
DEFAULT_UPDATE_THROTTLE_SECONDS = 1.5

# Slack chat_update/chat_postMessage effective ceiling. Empirically ~4000
# UTF-8 bytes (not characters) — a CJK char is 3 bytes, so a char-based
# check lets long CJK messages slip past and hit msg_too_long.
DEFAULT_MSG_MAX_BYTES = 3_900


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
    # Rendering knobs — no env var, but tunable per adapter instance (tests
    # shrink the throttle instead of monkeypatching module state).
    update_throttle_seconds: float = DEFAULT_UPDATE_THROTTLE_SECONDS
    msg_max_bytes: int = DEFAULT_MSG_MAX_BYTES

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> SlackConfig:
        return cls(
            bot_token=env_str(env, "AGENT_BRIDGE_SLACK_BOT_TOKEN", ""),
            app_token=env_str(env, "AGENT_BRIDGE_SLACK_APP_TOKEN", ""),
            startup_notify_channel=env_str_or_none(
                env, "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL"
            ),
            startup_notify_message=env_str_or_none(
                env, "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE"
            ),
            allow_channels=frozenset(
                normalize_channel(name)
                for name in env_csv(env, "AGENT_BRIDGE_SLACK_ALLOW_CHANNELS")
            ),
            channel_not_allowed_message=env_str(
                env,
                "AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE",
                DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
            ),
            usage_report_enabled=env_bool(
                env, "AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED", False
            ),
            usage_report_template=env_str_or_none(
                env, "AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE"
            ),
        )

    @classmethod
    def from_env_optional(cls, env: Env = PROCESS_ENV) -> SlackConfig | None:
        """None when Slack isn't configured at all. A half-configured Slack
        (one token set, the other missing) is a mistake, not an opt-out — it
        falls through to ``from_env`` and raises."""
        if not env_str(env, "AGENT_BRIDGE_SLACK_BOT_TOKEN", "") and not env_str(
            env, "AGENT_BRIDGE_SLACK_APP_TOKEN", ""
        ):
            return None
        return cls.from_env(env)

    def _validate(self) -> None:
        if not self.bot_token or not self.app_token:
            raise ValueError(
                "AGENT_BRIDGE_SLACK_BOT_TOKEN and AGENT_BRIDGE_SLACK_APP_TOKEN "
                "environment variables are required"
            )
        if self.update_throttle_seconds <= 0:
            raise ValueError(
                "SlackConfig.update_throttle_seconds must be positive, "
                f"got {self.update_throttle_seconds}"
            )
        if self.msg_max_bytes <= 0:
            raise ValueError(
                f"SlackConfig.msg_max_bytes must be positive, got {self.msg_max_bytes}"
            )


def normalize_channel(name: str) -> str:
    """Shared by config parsing and the adapter's allow-list gate."""
    return name.strip().lstrip("#").lower()

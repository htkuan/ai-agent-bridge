from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.config_loader import ConfigSource

DEFAULT_API_BASE_URL = "https://api.line.me"
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8080
DEFAULT_WEBHOOK_PATH = "/line/webhook"


@dataclass(frozen=True)
class LineConfig:
    # Channel secret signs every webhook request (X-Line-Signature).
    channel_secret: str
    # Channel access token authorizes Messaging API calls (reply/push).
    channel_access_token: str
    webhook_host: str = DEFAULT_WEBHOOK_HOST
    # 0 binds an ephemeral port (mainly for tests — read the actual port back
    # via LineAdapter.bound_port).
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    # Overridable so tests can point the adapter at a fake Messaging API server.
    api_base_url: str = DEFAULT_API_BASE_URL

    @classmethod
    def from_env(cls) -> LineConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> LineConfig:
        channel_secret = source.get(
            "AGENT_BRIDGE_LINE_CHANNEL_SECRET", "platforms.line.channel_secret", ""
        )
        if not channel_secret:
            raise ValueError(
                "AGENT_BRIDGE_LINE_CHANNEL_SECRET (platforms.line.channel_secret) is required"
            )
        channel_access_token = source.get(
            "AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN",
            "platforms.line.channel_access_token",
            "",
        )
        if not channel_access_token:
            raise ValueError(
                "AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN "
                "(platforms.line.channel_access_token) is required"
            )
        config = cls(
            channel_secret=channel_secret,
            channel_access_token=channel_access_token,
            webhook_host=source.get(
                "AGENT_BRIDGE_LINE_WEBHOOK_HOST",
                "platforms.line.webhook.host",
                DEFAULT_WEBHOOK_HOST,
            ),
            webhook_port=int(
                source.get(
                    "AGENT_BRIDGE_LINE_WEBHOOK_PORT",
                    "platforms.line.webhook.port",
                    str(DEFAULT_WEBHOOK_PORT),
                )
            ),
            webhook_path=source.get(
                "AGENT_BRIDGE_LINE_WEBHOOK_PATH",
                "platforms.line.webhook.path",
                DEFAULT_WEBHOOK_PATH,
            ),
            api_base_url=source.get(
                "AGENT_BRIDGE_LINE_API_BASE_URL",
                "platforms.line.api_base_url",
                DEFAULT_API_BASE_URL,
            ).rstrip("/"),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.webhook_host:
            raise ValueError("AGENT_BRIDGE_LINE_WEBHOOK_HOST must not be empty")
        if not 0 <= self.webhook_port <= 65535:
            raise ValueError(
                f"AGENT_BRIDGE_LINE_WEBHOOK_PORT must be 0-65535, got {self.webhook_port}"
            )
        if not self.webhook_path.startswith("/"):
            raise ValueError(
                f"AGENT_BRIDGE_LINE_WEBHOOK_PATH must start with '/', got {self.webhook_path!r}"
            )
        if not self.api_base_url:
            raise ValueError("AGENT_BRIDGE_LINE_API_BASE_URL must not be empty")

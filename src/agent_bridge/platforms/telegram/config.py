from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_bridge.config_loader import ConfigSource

DEFAULT_API_BASE_URL = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    # Allow-list of chat ids as strings (e.g. "-1001234567890"). Empty = allow
    # every chat. Messages from chats outside the list are silently ignored.
    allow_chats: frozenset[str] = frozenset()
    # Long-poll wait passed to getUpdates. 0 = short polling (mainly for tests).
    poll_timeout_seconds: int = 30
    # Persists the last processed update_id across restarts.
    state_path: Path = Path("./telegram.json")
    # Overridable so tests can point the adapter at a fake Bot API server.
    api_base_url: str = DEFAULT_API_BASE_URL

    @classmethod
    def from_env(cls) -> TelegramConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> TelegramConfig:
        bot_token = source.get(
            "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN", "platforms.telegram.bot_token", ""
        )
        if not bot_token:
            raise ValueError(
                "AGENT_BRIDGE_TELEGRAM_BOT_TOKEN "
                "(platforms.telegram.bot_token) is required"
            )
        raw_chats = source.get(
            "AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS", "platforms.telegram.allow_chats", ""
        )
        allow_chats = frozenset(
            chat.strip() for chat in raw_chats.split(",") if chat.strip()
        )
        config = cls(
            bot_token=bot_token,
            allow_chats=allow_chats,
            poll_timeout_seconds=int(
                source.get(
                    "AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS",
                    "platforms.telegram.poll_timeout_seconds",
                    "30",
                )
            ),
            state_path=Path(
                source.get(
                    "AGENT_BRIDGE_TELEGRAM_STATE_PATH",
                    "platforms.telegram.state_path",
                    "./telegram.json",
                )
            ),
            api_base_url=source.get(
                "AGENT_BRIDGE_TELEGRAM_API_BASE_URL",
                "platforms.telegram.api_base_url",
                DEFAULT_API_BASE_URL,
            ).rstrip("/"),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if self.poll_timeout_seconds < 0:
            raise ValueError(
                "AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS must be >= 0, "
                f"got {self.poll_timeout_seconds}"
            )
        if not self.api_base_url:
            raise ValueError("AGENT_BRIDGE_TELEGRAM_API_BASE_URL must not be empty")

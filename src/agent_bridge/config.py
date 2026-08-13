"""AppConfig — the whole application's configuration in one object.

Aggregates every layer's component config. ``app.py`` builds the running
system from an instance of this and nothing else, so tests wire the app by
constructing one instead of setting environment variables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.bridge.config import BridgeConfig
from agent_bridge.env import PROCESS_ENV, Env, env_str, load_env_file
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import SlackConfig

# Interval for periodic maintenance (session purge, stale pending cleanup).
DEFAULT_CLEANUP_INTERVAL_SECONDS = 3600.0


@dataclass(frozen=True)
class AppConfig:
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    # None ⇒ that platform is not configured; app.py skips building it.
    slack: SlackConfig | None = None
    heartbeat: HeartbeatConfig | None = None
    log_level: str = "INFO"
    cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env | None = None) -> AppConfig:
        """Read every ``AGENT_BRIDGE_*`` variable. Passing ``env`` explicitly
        skips ``.env`` loading — the process environment is the only source
        that gets a ``.env`` overlay, and it gets it exactly once, here.
        """
        if env is None:
            load_env_file()
            env = PROCESS_ENV
        return cls(
            bridge=BridgeConfig.from_env(env),
            claude=ClaudeConfig.from_env(env),
            slack=SlackConfig.from_env_optional(env),
            heartbeat=HeartbeatConfig.from_env_optional(env),
            log_level=env_str(env, "AGENT_BRIDGE_LOG_LEVEL", "INFO").upper(),
        )

    def _validate(self) -> None:
        if self.log_level not in logging.getLevelNamesMapping():
            raise ValueError(
                f"Invalid AGENT_BRIDGE_LOG_LEVEL: {self.log_level!r}. "
                f"Must be one of: {', '.join(logging.getLevelNamesMapping())}"
            )
        if self.cleanup_interval_seconds <= 0:
            raise ValueError(
                "AppConfig.cleanup_interval_seconds must be positive, "
                f"got {self.cleanup_interval_seconds}"
            )

from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.env import PROCESS_ENV, Env, env_bool, env_str

# Delivery/housekeeping knobs — config fields but no env var; tests tune them
# through the config instead of monkeypatching module state.
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 10.0
DEFAULT_CALLBACK_RETRY_DELAYS = (1.0, 5.0)
DEFAULT_IDLE_STATE_SECONDS = 3600.0


@dataclass(frozen=True)
class WebhookConfig:
    """Present only when the webhook platform is on — ``enabled`` lives in
    ``AppConfig.webhook is None``. Requires the shared HTTP server."""

    # No default: this endpoint drives the agent, so it must never come up
    # unauthenticated (same principle as ClaudeConfig.work_dir).
    token: str
    callback_timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS
    # Waits before each retry after a failed callback delivery attempt.
    callback_retry_delays: tuple[float, ...] = DEFAULT_CALLBACK_RETRY_DELAYS
    # Idle conversation state older than this is purged by cleanup().
    idle_state_seconds: float = DEFAULT_IDLE_STATE_SECONDS

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> WebhookConfig:
        return cls(token=env_str(env, "AGENT_BRIDGE_WEBHOOK_TOKEN", ""))

    @classmethod
    def from_env_optional(cls, env: Env = PROCESS_ENV) -> WebhookConfig | None:
        if not env_bool(env, "AGENT_BRIDGE_WEBHOOK_ENABLED", False):
            return None
        return cls.from_env(env)

    def _validate(self) -> None:
        if not self.token:
            raise ValueError(
                "AGENT_BRIDGE_WEBHOOK_TOKEN is required when the webhook "
                "platform is enabled"
            )
        if self.callback_timeout_seconds <= 0:
            raise ValueError(
                "WebhookConfig.callback_timeout_seconds must be positive, "
                f"got {self.callback_timeout_seconds}"
            )
        if any(delay < 0 for delay in self.callback_retry_delays):
            raise ValueError(
                "WebhookConfig.callback_retry_delays must be non-negative, "
                f"got {self.callback_retry_delays}"
            )
        if self.idle_state_seconds <= 0:
            raise ValueError(
                "WebhookConfig.idle_state_seconds must be positive, "
                f"got {self.idle_state_seconds}"
            )

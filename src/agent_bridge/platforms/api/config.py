from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.config_loader import ConfigSource

_TRUTHY = {"true", "1", "yes", "on"}

# Loopback by default: exposing the agent over HTTP is opt-in twice — once via
# `enabled`, and again by deliberately binding a routable address.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081


@dataclass(frozen=True)
class ApiConfig:
    # There is no secret to infer activation from (auth is optional), so the
    # adapter requires an explicit enable — same pattern as heartbeat.
    enabled: bool = False
    host: str = DEFAULT_HOST
    # 0 binds an ephemeral port (mainly for tests — read the actual port back
    # via ApiAdapter.bound_port).
    port: int = DEFAULT_PORT
    # Empty = no auth. When set, every /v1 request must carry
    # `Authorization: Bearer <token>`.
    auth_token: str = ""

    @classmethod
    def from_env(cls) -> ApiConfig:
        return cls.from_source(ConfigSource.empty())

    @classmethod
    def from_source(cls, source: ConfigSource) -> ApiConfig:
        enabled = (
            source.get(
                "AGENT_BRIDGE_API_ENABLED", "platforms.api.enabled", "false"
            ).lower()
            in _TRUTHY
        )
        if not enabled:
            return cls()

        config = cls(
            enabled=True,
            host=source.get("AGENT_BRIDGE_API_HOST", "platforms.api.host", DEFAULT_HOST),
            port=int(
                source.get(
                    "AGENT_BRIDGE_API_PORT", "platforms.api.port", str(DEFAULT_PORT)
                )
            ),
            auth_token=source.get(
                "AGENT_BRIDGE_API_AUTH_TOKEN", "platforms.api.auth_token", ""
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.host:
            raise ValueError("AGENT_BRIDGE_API_HOST must not be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError(
                f"AGENT_BRIDGE_API_PORT must be 0-65535, got {self.port}"
            )

from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.env import PROCESS_ENV, Env, env_bool, env_int, env_str

# Loopback by default: exposing the server beyond this host is an explicit
# operator decision (e.g. 0.0.0.0 behind a reverse proxy), never a fallback.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


@dataclass(frozen=True)
class HttpConfig:
    """Present only when the HTTP server is on — ``enabled`` lives in
    ``AppConfig.http is None`` (same pattern as heartbeat)."""

    host: str = DEFAULT_HOST
    # Port 0 asks the OS for a free port (HttpServer.port reports the real one).
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> HttpConfig:
        return cls(
            host=env_str(env, "AGENT_BRIDGE_HTTP_HOST", DEFAULT_HOST),
            port=env_int(env, "AGENT_BRIDGE_HTTP_PORT", DEFAULT_PORT),
        )

    @classmethod
    def from_env_optional(cls, env: Env = PROCESS_ENV) -> HttpConfig | None:
        if not env_bool(env, "AGENT_BRIDGE_HTTP_ENABLED", False):
            return None
        return cls.from_env(env)

    def _validate(self) -> None:
        if not self.host:
            raise ValueError("AGENT_BRIDGE_HTTP_HOST must not be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError(
                f"AGENT_BRIDGE_HTTP_PORT must be in 0..65535, got {self.port}"
            )

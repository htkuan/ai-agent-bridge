"""AppConfig — the whole application's configuration in one object.

Aggregates every layer's component config. ``app.py`` builds the running
system from an instance of this and nothing else, so tests wire the app by
constructing one instead of setting environment variables.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.bridge.config import BridgeConfig
from agent_bridge.env import PROCESS_ENV, Env, env_str, load_env_file
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.platforms.webhook.config import WebhookConfig
from agent_bridge.server.config import HttpConfig

# Interval for periodic maintenance (session purge, stale pending cleanup).
DEFAULT_CLEANUP_INTERVAL_SECONDS = 3600.0


@dataclass(frozen=True)
class AppConfig:
    # No default: an app without an agent isn't runnable, and ClaudeConfig's
    # work_dir has no safe default to fall back to.
    claude: ClaudeConfig
    # Named Claude profiles from the profiles file (AGENT_BRIDGE_PROFILES_PATH);
    # each becomes its own controller, routed to by name.
    claude_profiles: dict[str, ClaudeConfig] = field(
        default_factory=dict[str, ClaudeConfig]
    )
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    # None ⇒ that platform is not configured; app.py skips building it.
    slack: SlackConfig | None = None
    heartbeat: HeartbeatConfig | None = None
    webhook: WebhookConfig | None = None
    # None ⇒ no HTTP server. Shared infrastructure, not a platform: the
    # webhook platform (and the console) mount routes onto it.
    http: HttpConfig | None = None
    log_level: str = "INFO"
    cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env | None = None) -> AppConfig:
        """Read every ``AGENT_BRIDGE_*`` variable. Passing ``env`` explicitly
        skips ``.env`` loading — the process environment is the only source
        that gets a ``.env`` overlay, and it gets it exactly once, here.

        ``AGENT_BRIDGE_PROFILES_PATH``, when set, points at a TOML file that
        holds the structured part of the config: named Claude profiles and
        the Slack channel → profile mapping. Reading it is config parsing
        (same rank as the ``.env`` overlay), not a prerequisite probe.
        """
        if env is None:
            load_env_file()
            env = PROCESS_ENV
        claude = ClaudeConfig.from_env(env)
        slack = SlackConfig.from_env_optional(env)
        claude_profiles: dict[str, ClaudeConfig] = {}
        profiles_path = env_str(env, "AGENT_BRIDGE_PROFILES_PATH", "")
        if profiles_path:
            claude_section, slack_section = _load_profiles_file(Path(profiles_path))
            claude_profiles = ClaudeConfig.profiles_from_data(claude_section, claude)
            channel_profiles = SlackConfig.channel_profiles_from_data(slack_section)
            if channel_profiles:
                if slack is None:
                    raise ValueError(
                        "The profiles file maps Slack channels but Slack is "
                        "not configured: set the Slack tokens or remove "
                        "[slack.channel_profiles]"
                    )
                # replace() re-runs SlackConfig._validate with the mapping in.
                slack = replace(slack, channel_profiles=channel_profiles)
        return cls(
            claude=claude,
            claude_profiles=claude_profiles,
            bridge=BridgeConfig.from_env(env),
            slack=slack,
            heartbeat=HeartbeatConfig.from_env_optional(env),
            webhook=WebhookConfig.from_env_optional(env),
            http=HttpConfig.from_env_optional(env),
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
        if self.webhook is not None and self.http is None:
            raise ValueError(
                "The webhook platform needs the HTTP server: set "
                "AGENT_BRIDGE_HTTP_ENABLED=true alongside "
                "AGENT_BRIDGE_WEBHOOK_ENABLED"
            )
        if self.slack is not None:
            unknown = set(self.slack.channel_profiles.values()) - set(
                self.claude_profiles
            )
            if unknown:
                raise ValueError(
                    "slack.channel_profiles references unknown Claude "
                    f"profile(s): {', '.join(sorted(unknown))}. Defined: "
                    f"{', '.join(sorted(self.claude_profiles)) or '(none)'}"
                )


def _load_profiles_file(
    path: Path,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read the profiles file → the raw ``[claude.profiles]`` and
    ``[slack.channel_profiles]`` sections (each ``{}`` when absent).
    Shape errors — unparseable TOML, unknown sections/keys — fail fast here;
    each section's semantics are parsed by its own layer's config class.
    """
    if not path.is_file():
        raise ValueError(
            f"AGENT_BRIDGE_PROFILES_PATH does not exist or is not a file: {path}"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML in {path}: {e}") from e
    unknown = set(data) - {"claude", "slack"}
    if unknown:
        raise ValueError(
            f"Unknown section(s) in {path}: {', '.join(sorted(unknown))}. "
            "Valid sections: claude, slack"
        )
    return (
        _file_section(data, "claude", "profiles", path),
        _file_section(data, "slack", "channel_profiles", path),
    )


def _file_section(
    data: Mapping[str, object], section: str, only_key: str, path: Path
) -> Mapping[str, object]:
    """The ``[section.only_key]`` table, ``{}`` when absent. Any other key
    under ``[section]`` is a typo — fail fast."""
    raw = data.get(section)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"[{section}] in {path} must be a table")
    # tomllib guarantees string keys on parsed tables.
    table = cast("Mapping[str, object]", raw)
    unknown = set(table) - {only_key}
    if unknown:
        raise ValueError(
            f"Unknown key(s) in [{section}] of {path}: "
            f"{', '.join(sorted(unknown))}. Valid: {only_key}"
        )
    inner = table.get(only_key)
    if inner is None:
        return {}
    if not isinstance(inner, dict):
        raise ValueError(f"[{section}.{only_key}] in {path} must be a table")
    return cast("Mapping[str, object]", inner)

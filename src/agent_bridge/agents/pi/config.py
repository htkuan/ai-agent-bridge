from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_csv,
    env_float,
    env_path,
    env_str,
)
from agent_bridge.profile_fields import (
    field_number,
    field_opt_str,
    field_path,
    field_str,
    field_str_tuple,
)

VALID_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}

# Fields a named profile (profiles file, `[pi.profiles.<name>]`) may set.
# Anything else in a profile table is a typo — and a silently ignored
# `work_dir` typo is exactly the dangerous-field class we fail fast on.
PROFILE_FIELDS = frozenset(
    {
        "work_dir",
        "provider",
        "model",
        "thinking",
        "timeout_seconds",
        "cli_path",
        "tools",
        "exclude_tools",
    }
)

# "default" always routes to the bridge's env-built default controller;
# a profile by that name would never be reachable unambiguously.
RESERVED_PROFILE_NAME = "default"

_PROFILE_NAME_RE = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class PiConfig:
    # Required on purpose: this is the directory the agent gets loose in, and
    # every plausible default (cwd, home) is a directory it should not touch.
    work_dir: Path
    # None ⇒ no flag; pi's own settings pick the provider/model/thinking.
    provider: str | None = None
    model: str | None = None
    thinking: str | None = None
    timeout_seconds: float = 600.0
    cli_path: str = "pi"
    # Pi has no sandbox or permission prompts in print mode — the tool
    # allowlist/denylist IS its permission model (e.g. tools=("read", "grep",
    # "find", "ls") is read-only). Empty ⇒ no flag, all built-in tools on.
    tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> PiConfig:
        return cls(
            work_dir=env_path(env, "AGENT_BRIDGE_PI_WORK_DIR", ".").resolve(),
            provider=env_str(env, "AGENT_BRIDGE_PI_PROVIDER", "") or None,
            model=env_str(env, "AGENT_BRIDGE_PI_MODEL", "") or None,
            thinking=env_str(env, "AGENT_BRIDGE_PI_THINKING", "") or None,
            timeout_seconds=env_float(env, "AGENT_BRIDGE_PI_TIMEOUT_SECONDS", 600.0),
            cli_path=env_str(env, "AGENT_BRIDGE_PI_CLI_PATH", "pi"),
            tools=env_csv(env, "AGENT_BRIDGE_PI_TOOLS"),
            exclude_tools=env_csv(env, "AGENT_BRIDGE_PI_EXCLUDE_TOOLS"),
        )

    @classmethod
    def profiles_from_data(
        cls, data: Mapping[str, object], base: PiConfig
    ) -> dict[str, PiConfig]:
        """Build named profiles from the ``[pi.profiles]`` section of the
        profiles file. Fields a profile doesn't set inherit ``base``'s value;
        every profile runs the same ``_validate`` as the base config.
        """
        profiles: dict[str, PiConfig] = {}
        for name, fields in data.items():
            if not _PROFILE_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"Invalid profile name {name!r} in pi.profiles: "
                    "must match [a-z0-9_-]+"
                )
            if name == RESERVED_PROFILE_NAME:
                raise ValueError(
                    "Profile name 'default' is reserved: it always routes to "
                    "the bridge's env-built default controller"
                )
            profiles[name] = _profile_from_table(name, fields, base)
        return profiles

    def _validate(self) -> None:
        """Value checks only — runs on every construction, including tests."""
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_PI_TIMEOUT_SECONDS must be positive, "
                f"got {self.timeout_seconds}"
            )
        if not self.cli_path:
            raise ValueError("AGENT_BRIDGE_PI_CLI_PATH must not be empty")
        if self.thinking is not None and self.thinking not in VALID_THINKING_LEVELS:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_PI_THINKING: {self.thinking!r}. "
                f"Must be one of: {', '.join(sorted(VALID_THINKING_LEVELS))}"
            )
        for label, value in (
            ("AGENT_BRIDGE_PI_PROVIDER", self.provider),
            ("AGENT_BRIDGE_PI_MODEL", self.model),
        ):
            if value is not None and not value.strip():
                raise ValueError(
                    f"{label} must not be blank (unset it to use pi's default)"
                )
        for label, values in (
            ("AGENT_BRIDGE_PI_TOOLS", self.tools),
            ("AGENT_BRIDGE_PI_EXCLUDE_TOOLS", self.exclude_tools),
        ):
            if any(not item.strip() for item in values):
                raise ValueError(f"{label} entries must not be blank")

    def check_prerequisites(self) -> None:
        """Probe the world the config points at — the work dir must exist.
        Separate from ``_validate`` so constructing a config stays cheap and
        side-effect free. ``app.run`` calls it once at startup.
        """
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_PI_WORK_DIR does not exist or is not a directory: "
                f"{self.work_dir}"
            )


def _profile_from_table(name: str, fields: object, base: PiConfig) -> PiConfig:
    if not isinstance(fields, dict):
        raise ValueError(
            f"pi.profiles.{name} must be a table of fields, got {type(fields).__name__}"
        )
    # tomllib guarantees string keys on parsed tables.
    table = cast("Mapping[str, object]", fields)
    unknown = set(table) - PROFILE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s) in pi.profiles.{name}: "
            f"{', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(PROFILE_FIELDS))}"
        )
    where = f"pi.profiles.{name}"
    return PiConfig(
        work_dir=field_path(where, table, "work_dir", base.work_dir),
        provider=field_opt_str(where, table, "provider", base.provider),
        model=field_opt_str(where, table, "model", base.model),
        thinking=field_opt_str(where, table, "thinking", base.thinking),
        timeout_seconds=field_number(
            where, table, "timeout_seconds", base.timeout_seconds
        ),
        cli_path=field_str(where, table, "cli_path", base.cli_path),
        tools=field_str_tuple(where, table, "tools", base.tools),
        exclude_tools=field_str_tuple(
            where, table, "exclude_tools", base.exclude_tools
        ),
    )

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_float,
    env_path,
    env_str,
)
from agent_bridge.profile_fields import (
    field_number,
    field_opt_str,
    field_path,
    field_str,
)

# Fields a named profile (profiles file, `[opencode.profiles.<name>]`) may set.
# Anything else in a profile table is a typo — and a silently ignored
# `work_dir` typo is exactly the dangerous-field class we fail fast on.
PROFILE_FIELDS = frozenset(
    {
        "work_dir",
        "model",
        "variant",
        "timeout_seconds",
        "cli_path",
        "session_map_path",
    }
)

# "default" always routes to the bridge's env-built default controller;
# a profile by that name would never be reachable unambiguously.
RESERVED_PROFILE_NAME = "default"

_PROFILE_NAME_RE = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class OpencodeConfig:
    # Required on purpose: this is the directory the agent gets loose in, and
    # every plausible default (cwd, home) is a directory it should not touch.
    work_dir: Path
    # None ⇒ no flag; opencode's own settings pick the model/variant. Both are
    # opaque pass-throughs — opencode validates them itself.
    model: str | None = None
    variant: str | None = None
    timeout_seconds: float = 600.0
    cli_path: str = "opencode"
    # None ⇒ derived under work_dir (see resolved_session_map_path).
    session_map_path: Path | None = None

    def __post_init__(self) -> None:
        self._validate()

    @property
    def resolved_session_map_path(self) -> Path:
        """Where the bridge-session → opencode-session map lives. Profiles
        sharing a work_dir share the derived file — the store tolerates that."""
        if self.session_map_path is not None:
            return self.session_map_path
        return self.work_dir / ".agent-bridge" / "opencode-sessions.json"

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> OpencodeConfig:
        raw_map_path = env_str(env, "AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH", "")
        return cls(
            work_dir=env_path(env, "AGENT_BRIDGE_OPENCODE_WORK_DIR", ".").resolve(),
            model=env_str(env, "AGENT_BRIDGE_OPENCODE_MODEL", "") or None,
            variant=env_str(env, "AGENT_BRIDGE_OPENCODE_VARIANT", "") or None,
            timeout_seconds=env_float(
                env, "AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS", 600.0
            ),
            cli_path=env_str(env, "AGENT_BRIDGE_OPENCODE_CLI_PATH", "opencode"),
            session_map_path=Path(raw_map_path).resolve() if raw_map_path else None,
        )

    @classmethod
    def profiles_from_data(
        cls, data: Mapping[str, object], base: OpencodeConfig
    ) -> dict[str, OpencodeConfig]:
        """Build named profiles from the ``[opencode.profiles]`` section of the
        profiles file. Fields a profile doesn't set inherit ``base``'s value;
        every profile runs the same ``_validate`` as the base config.
        """
        profiles: dict[str, OpencodeConfig] = {}
        for name, fields in data.items():
            if not _PROFILE_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"Invalid profile name {name!r} in opencode.profiles: "
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
                f"AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS must be positive, "
                f"got {self.timeout_seconds}"
            )
        if not self.cli_path:
            raise ValueError("AGENT_BRIDGE_OPENCODE_CLI_PATH must not be empty")
        for label, value in (
            ("AGENT_BRIDGE_OPENCODE_MODEL", self.model),
            ("AGENT_BRIDGE_OPENCODE_VARIANT", self.variant),
        ):
            if value is not None and not value.strip():
                raise ValueError(
                    f"{label} must not be blank (unset it to use opencode's default)"
                )

    def check_prerequisites(self) -> None:
        """Probe the world the config points at. Separate from ``_validate``
        so constructing a config stays cheap and side-effect free; ``app.run``
        calls it once at startup.
        """
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_OPENCODE_WORK_DIR does not exist or is not a "
                f"directory: {self.work_dir}"
            )


def _field_opt_path(
    where: str, table: Mapping[str, object], key: str, default: Path | None
) -> Path | None:
    # No TOML spelling for "reset to unset" — absent means inherit.
    if key not in table:
        return default
    return field_path(where, table, key, Path("."))


def _profile_from_table(
    name: str, fields: object, base: OpencodeConfig
) -> OpencodeConfig:
    if not isinstance(fields, dict):
        raise ValueError(
            f"opencode.profiles.{name} must be a table of fields, "
            f"got {type(fields).__name__}"
        )
    # tomllib guarantees string keys on parsed tables.
    table = cast("Mapping[str, object]", fields)
    unknown = set(table) - PROFILE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s) in opencode.profiles.{name}: "
            f"{', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(PROFILE_FIELDS))}"
        )
    where = f"opencode.profiles.{name}"
    return OpencodeConfig(
        work_dir=field_path(where, table, "work_dir", base.work_dir),
        model=field_opt_str(where, table, "model", base.model),
        variant=field_opt_str(where, table, "variant", base.variant),
        timeout_seconds=field_number(
            where, table, "timeout_seconds", base.timeout_seconds
        ),
        cli_path=field_str(where, table, "cli_path", base.cli_path),
        session_map_path=_field_opt_path(
            where, table, "session_map_path", base.session_map_path
        ),
    )

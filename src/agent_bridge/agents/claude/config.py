from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_bridge.env import (
    PROCESS_ENV,
    Env,
    env_bool,
    env_float,
    env_path,
    env_str,
)

VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
    "dangerously-skip-permissions",
}

VALID_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

# Fields a named profile (profiles file, `[claude.profiles.<name>]`) may set.
# Anything else in a profile table is a typo — and a silently ignored
# `work_dir` typo is exactly the dangerous-field class we fail fast on.
PROFILE_FIELDS = frozenset(
    {
        "work_dir",
        "permission_mode",
        "timeout_seconds",
        "worktree_enabled",
        "effort",
        "model",
        "cli_path",
    }
)

# The default profile is configured via AGENT_BRIDGE_CLAUDE_*, not the file.
RESERVED_PROFILE_NAME = "default"

_PROFILE_NAME_RE = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class ClaudeConfig:
    # Required on purpose: this is the directory the agent gets loose in, and
    # every plausible default (cwd, home) is a directory it should not touch.
    work_dir: Path
    permission_mode: str = "acceptEdits"
    timeout_seconds: float = 600.0
    worktree_enabled: bool = False
    effort: str = "xhigh"
    cli_path: str = "claude"
    # None ⇒ no --model flag; the CLI picks its own default.
    model: str | None = None

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> ClaudeConfig:
        return cls(
            work_dir=env_path(env, "AGENT_BRIDGE_CLAUDE_WORK_DIR", ".").resolve(),
            permission_mode=env_str(
                env, "AGENT_BRIDGE_CLAUDE_PERMISSION_MODE", "acceptEdits"
            ),
            timeout_seconds=env_float(
                env, "AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS", 600.0
            ),
            worktree_enabled=env_bool(
                env, "AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", False
            ),
            effort=env_str(env, "AGENT_BRIDGE_CLAUDE_EFFORT", "xhigh"),
            cli_path=env_str(env, "AGENT_BRIDGE_CLAUDE_CLI_PATH", "claude"),
            model=env_str(env, "AGENT_BRIDGE_CLAUDE_MODEL", "") or None,
        )

    @classmethod
    def profiles_from_data(
        cls, data: Mapping[str, object], base: ClaudeConfig
    ) -> dict[str, ClaudeConfig]:
        """Build named profiles from the ``[claude.profiles]`` section of the
        profiles file. Fields a profile doesn't set inherit ``base``'s value;
        every profile runs the same ``_validate`` as the base config.
        """
        profiles: dict[str, ClaudeConfig] = {}
        for name, fields in data.items():
            if not _PROFILE_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"Invalid profile name {name!r} in claude.profiles: "
                    "must match [a-z0-9_-]+"
                )
            if name == RESERVED_PROFILE_NAME:
                raise ValueError(
                    "Profile name 'default' is reserved: the default profile "
                    "is configured via the AGENT_BRIDGE_CLAUDE_* variables"
                )
            profiles[name] = _profile_from_table(name, fields, base)
        return profiles

    def _validate(self) -> None:
        """Value checks only — runs on every construction, including tests."""
        if self.permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CLAUDE_PERMISSION_MODE: "
                f"{self.permission_mode!r}. "
                f"Must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS must be positive, "
                f"got {self.timeout_seconds}"
            )
        if not self.cli_path:
            raise ValueError("AGENT_BRIDGE_CLAUDE_CLI_PATH must not be empty")
        if self.effort not in VALID_EFFORT_LEVELS:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CLAUDE_EFFORT: {self.effort!r}. "
                f"Must be one of: {', '.join(sorted(VALID_EFFORT_LEVELS))}"
            )
        if self.model is not None and not self.model.strip():
            raise ValueError(
                "AGENT_BRIDGE_CLAUDE_MODEL must not be blank "
                "(unset it to use the CLI's default model)"
            )

    def check_prerequisites(self) -> None:
        """Probe the world the config points at — the work dir must exist, and
        worktree mode needs a git repo with a resolvable origin/HEAD. Separate
        from ``_validate`` so constructing a config stays cheap and side-effect
        free. ``app.run`` calls it once at startup, whatever built the config.
        """
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_CLAUDE_WORK_DIR does not exist or is not a directory: "
                f"{self.work_dir}"
            )
        if self.worktree_enabled:
            self._check_worktree_prereqs()

    def _check_worktree_prereqs(self) -> None:
        if not (self.work_dir / ".git").exists():
            raise ValueError(
                f"AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED=true "
                f"but work_dir is not a git repository: {self.work_dir}"
            )
        # Claude's -w uses origin/HEAD as the base branch; fail fast if it's not set.
        try:
            # git deliberately resolved from PATH, args are all literals.
            subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],  # noqa: S607
                cwd=self.work_dir,
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ) as e:
            raise ValueError(
                f"AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED=true but {self.work_dir} has no "
                f"'origin' remote with a resolvable default branch. "
                f"Run `git remote set-head origin --auto` or disable worktree mode."
            ) from e


def _profile_from_table(name: str, fields: object, base: ClaudeConfig) -> ClaudeConfig:
    if not isinstance(fields, dict):
        raise ValueError(
            f"claude.profiles.{name} must be a table of fields, "
            f"got {type(fields).__name__}"
        )
    # tomllib guarantees string keys on parsed tables.
    table = cast("Mapping[str, object]", fields)
    unknown = set(table) - PROFILE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s) in claude.profiles.{name}: "
            f"{', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(PROFILE_FIELDS))}"
        )
    return ClaudeConfig(
        work_dir=_field_path(name, table, "work_dir", base.work_dir),
        permission_mode=_field_str(
            name, table, "permission_mode", base.permission_mode
        ),
        timeout_seconds=_field_number(
            name, table, "timeout_seconds", base.timeout_seconds
        ),
        worktree_enabled=_field_bool(
            name, table, "worktree_enabled", base.worktree_enabled
        ),
        effort=_field_str(name, table, "effort", base.effort),
        cli_path=_field_str(name, table, "cli_path", base.cli_path),
        model=_field_model(name, table, base.model),
    )


def _field_str(name: str, table: Mapping[str, object], key: str, default: str) -> str:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"claude.profiles.{name}.{key} must be a non-empty string, got {value!r}"
        )
    return value


def _field_model(
    name: str, table: Mapping[str, object], default: str | None
) -> str | None:
    # No TOML spelling for "reset to the CLI default" — absent means inherit.
    if "model" not in table:
        return default
    return _field_str(name, table, "model", "")


def _field_path(
    name: str, table: Mapping[str, object], key: str, default: Path
) -> Path:
    if key not in table:
        return default
    # An empty path would resolve to the process cwd — the exact dangerous
    # fallback work_dir exists to avoid — so _field_str's non-empty check
    # is load-bearing here.
    return Path(_field_str(name, table, key, "")).resolve()


def _field_number(
    name: str, table: Mapping[str, object], key: str, default: float
) -> float:
    if key not in table:
        return default
    value = table[key]
    # bool is an int subclass — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(
            f"claude.profiles.{name}.{key} must be a number, got {value!r}"
        )
    if not math.isfinite(value):
        raise ValueError(
            f"claude.profiles.{name}.{key} must be a finite number, got {value!r}"
        )
    return float(value)


def _field_bool(
    name: str, table: Mapping[str, object], key: str, default: bool
) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ValueError(
            f"claude.profiles.{name}.{key} must be a boolean, got {value!r}"
        )
    return value

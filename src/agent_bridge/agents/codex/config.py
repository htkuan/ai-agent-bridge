from __future__ import annotations

import re
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
from agent_bridge.profile_fields import (
    field_bool,
    field_number,
    field_opt_path,
    field_opt_str,
    field_path,
    field_str,
)

VALID_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}

PROFILE_FIELDS = frozenset(
    {
        "work_dir",
        "sandbox_mode",
        "model",
        "effort",
        "timeout_seconds",
        "cli_path",
        "skip_git_repo_check",
        "session_map_path",
    }
)

RESERVED_PROFILE_NAME = "default"

_PROFILE_NAME_RE = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class CodexConfig:
    work_dir: Path
    sandbox_mode: str = "workspace-write"
    model: str | None = None
    effort: str | None = None
    timeout_seconds: float = 600.0
    cli_path: str = "codex"
    skip_git_repo_check: bool = False
    session_map_path: Path | None = None

    def __post_init__(self) -> None:
        self._validate()

    @property
    def resolved_session_map_path(self) -> Path:
        return self.session_map_path or (
            self.work_dir / ".agent-bridge" / "codex-sessions.json"
        )

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> CodexConfig:
        raw_session_map_path = env_str(env, "AGENT_BRIDGE_CODEX_SESSION_MAP_PATH", "")
        return cls(
            work_dir=env_path(env, "AGENT_BRIDGE_CODEX_WORK_DIR", ".").resolve(),
            sandbox_mode=env_str(
                env, "AGENT_BRIDGE_CODEX_SANDBOX_MODE", "workspace-write"
            ),
            model=env_str(env, "AGENT_BRIDGE_CODEX_MODEL", "") or None,
            effort=env_str(env, "AGENT_BRIDGE_CODEX_EFFORT", "") or None,
            timeout_seconds=env_float(env, "AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS", 600.0),
            cli_path=env_str(env, "AGENT_BRIDGE_CODEX_CLI_PATH", "codex"),
            skip_git_repo_check=env_bool(
                env, "AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK", False
            ),
            session_map_path=Path(raw_session_map_path).resolve()
            if raw_session_map_path
            else None,
        )

    @classmethod
    def profiles_from_data(
        cls, data: Mapping[str, object], base: CodexConfig
    ) -> dict[str, CodexConfig]:
        profiles: dict[str, CodexConfig] = {}
        for name, fields in data.items():
            if not _PROFILE_NAME_RE.fullmatch(name):
                raise ValueError(
                    f"Invalid profile name {name!r} in codex.profiles: "
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
        if self.sandbox_mode not in VALID_SANDBOX_MODES:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CODEX_SANDBOX_MODE: {self.sandbox_mode!r}. "
                f"Must be one of: {', '.join(sorted(VALID_SANDBOX_MODES))}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS must be positive, "
                f"got {self.timeout_seconds}"
            )
        if not self.cli_path:
            raise ValueError("AGENT_BRIDGE_CODEX_CLI_PATH must not be empty")
        for label, value in (
            ("AGENT_BRIDGE_CODEX_MODEL", self.model),
            ("AGENT_BRIDGE_CODEX_EFFORT", self.effort),
        ):
            if value is not None and not value.strip():
                raise ValueError(
                    f"{label} must not be blank (unset it to use codex's default)"
                )

    def check_prerequisites(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_WORK_DIR does not exist or is not a directory: "
                f"{self.work_dir}"
            )
        if self.skip_git_repo_check:
            return
        if not (self.work_dir / ".git").exists():
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_WORK_DIR is not a git repository: "
                f"{self.work_dir}. Set AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK=true "
                "to pass --skip-git-repo-check."
            )


def _profile_from_table(name: str, fields: object, base: CodexConfig) -> CodexConfig:
    if not isinstance(fields, dict):
        raise ValueError(
            f"codex.profiles.{name} must be a table of fields, "
            f"got {type(fields).__name__}"
        )
    table = cast("Mapping[str, object]", fields)
    unknown = set(table) - PROFILE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown field(s) in codex.profiles.{name}: "
            f"{', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(PROFILE_FIELDS))}"
        )
    where = f"codex.profiles.{name}"
    return CodexConfig(
        work_dir=field_path(where, table, "work_dir", base.work_dir),
        sandbox_mode=field_str(where, table, "sandbox_mode", base.sandbox_mode),
        model=field_opt_str(where, table, "model", base.model),
        effort=field_opt_str(where, table, "effort", base.effort),
        timeout_seconds=field_number(
            where, table, "timeout_seconds", base.timeout_seconds
        ),
        cli_path=field_str(where, table, "cli_path", base.cli_path),
        skip_git_repo_check=field_bool(
            where, table, "skip_git_repo_check", base.skip_git_repo_check
        ),
        session_map_path=field_opt_path(
            where, table, "session_map_path", base.session_map_path
        ),
    )

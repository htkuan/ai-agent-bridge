from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class ClaudeConfig:
    work_dir: Path = field(default_factory=Path.cwd)
    permission_mode: str = "acceptEdits"
    timeout_seconds: float = 600.0
    worktree_enabled: bool = False
    effort: str = "xhigh"
    cli_path: str = "claude"

    def __post_init__(self) -> None:
        self._validate()

    @classmethod
    def from_env(cls, env: Env = PROCESS_ENV) -> ClaudeConfig:
        config = cls(
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
        )
        config.check_prerequisites()
        return config

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

    def check_prerequisites(self) -> None:
        """Probe the world the config points at — the work dir must exist, and
        worktree mode needs a git repo with a resolvable origin/HEAD. Separate
        from ``_validate`` so constructing a config stays cheap and side-effect
        free; ``from_env`` runs it so startup still fails fast.
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

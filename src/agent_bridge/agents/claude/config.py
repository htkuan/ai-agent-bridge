from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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

_TRUTHY = {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class ClaudeConfig:
    work_dir: Path
    permission_mode: str = "acceptEdits"
    timeout_seconds: float = 600.0
    worktree_enabled: bool = False
    effort: str = "xhigh"
    cli_path: str = "claude"

    @classmethod
    def from_env(cls) -> ClaudeConfig:
        load_dotenv()

        config = cls(
            work_dir=Path(
                os.environ.get("AGENT_BRIDGE_CLAUDE_WORK_DIR", ".")
            ).resolve(),
            permission_mode=os.environ.get(
                "AGENT_BRIDGE_CLAUDE_PERMISSION_MODE", "acceptEdits"
            ),
            timeout_seconds=float(
                os.environ.get("AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS", "600")
            ),
            worktree_enabled=os.environ.get(
                "AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED", "false"
            ).lower()
            in _TRUTHY,
            effort=os.environ.get("AGENT_BRIDGE_CLAUDE_EFFORT", "xhigh").strip()
            or "xhigh",
            cli_path=os.environ.get("AGENT_BRIDGE_CLAUDE_CLI_PATH", "claude").strip()
            or "claude",
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_CLAUDE_WORK_DIR does not exist or is not a directory: "
                f"{self.work_dir}"
            )
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
        if self.worktree_enabled:
            self._validate_worktree_prereqs()

    def _validate_worktree_prereqs(self) -> None:
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

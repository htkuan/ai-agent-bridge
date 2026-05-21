from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
VALID_APPROVAL_MODES = {"untrusted", "on-request", "never"}


@dataclass(frozen=True)
class CodexConfig:
    work_dir: Path
    sandbox: str = "workspace-write"
    approval: str = "never"
    model: str = ""
    timeout_seconds: float = 600.0
    thread_map_path: Path = Path("./codex_threads.json")
    skip_git_repo_check: bool = True
    extra_config: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> CodexConfig:
        load_dotenv()

        extra_raw = os.environ.get("AGENT_BRIDGE_CODEX_EXTRA_CONFIG", "").strip()
        extra = tuple(item.strip() for item in extra_raw.split(",") if item.strip())

        config = cls(
            work_dir=Path(os.environ.get("AGENT_BRIDGE_CODEX_WORK_DIR", ".")).resolve(),
            sandbox=os.environ.get("AGENT_BRIDGE_CODEX_SANDBOX", "workspace-write"),
            approval=os.environ.get("AGENT_BRIDGE_CODEX_APPROVAL", "never"),
            model=os.environ.get("AGENT_BRIDGE_CODEX_MODEL", "").strip(),
            timeout_seconds=float(os.environ.get("AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS", "600")),
            thread_map_path=Path(
                os.environ.get("AGENT_BRIDGE_CODEX_THREAD_MAP_PATH", "./codex_threads.json")
            ),
            skip_git_repo_check=os.environ.get(
                "AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK", "true"
            ).lower() in {"true", "1", "yes", "on"},
            extra_config=extra,
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if not self.work_dir.is_dir():
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_WORK_DIR does not exist or is not a directory: {self.work_dir}"
            )
        if self.sandbox not in VALID_SANDBOX_MODES:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CODEX_SANDBOX: {self.sandbox!r}. "
                f"Must be one of: {', '.join(sorted(VALID_SANDBOX_MODES))}"
            )
        if self.approval not in VALID_APPROVAL_MODES:
            raise ValueError(
                f"Invalid AGENT_BRIDGE_CODEX_APPROVAL: {self.approval!r}. "
                f"Must be one of: {', '.join(sorted(VALID_APPROVAL_MODES))}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS must be positive, got {self.timeout_seconds}"
            )
        for entry in self.extra_config:
            if "=" not in entry:
                raise ValueError(
                    f"AGENT_BRIDGE_CODEX_EXTRA_CONFIG entry must be key=value, got {entry!r}"
                )

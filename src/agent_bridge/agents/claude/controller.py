from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent_bridge.agents.base import CliAgentController, RunState
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.events import (
    ResultEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import BridgeEvent

logger = logging.getLogger(__name__)


class ClaudeController(CliAgentController[RunState]):
    agent_name = "Claude"

    def __init__(self, config: ClaudeConfig) -> None:
        super().__init__(
            work_dir=config.work_dir, timeout_seconds=config.timeout_seconds
        )
        self._config = config

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        cmd = [
            self._config.cli_path,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if self._config.worktree_enabled:
            # Use session_id as worktree name so path/branch are deterministic.
            # On new sessions Claude creates <work_dir>/.claude/worktrees/<session_id>;
            # on --resume it reuses the existing one and cd's into it automatically.
            cmd.extend(["-w", session_id])

        if is_new:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        permission_mode = self._config.permission_mode
        if permission_mode == "dangerously-skip-permissions":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", permission_mode])

        cmd.extend(["--effort", self._config.effort])

        if self._config.model:
            cmd.extend(["--model", self._config.model])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        return cmd

    def new_run_state(self) -> RunState:
        return RunState()

    def parse_line(self, line: str, state: RunState) -> list[BridgeEvent]:
        events: list[BridgeEvent] = []
        for claude_event in parse_stream_line(line):
            logger.debug("Parsed Claude event: %s", claude_event)
            bridge_event = to_bridge_event(claude_event)
            if bridge_event is not None:
                events.append(bridge_event)
            else:
                logger.debug("Filtered out (internal): %s", type(claude_event).__name__)
            if isinstance(claude_event, ResultEvent):
                # `result` is the terminal stream-json line.
                state.terminal = True
        return events

    async def cleanup_session(self, session_id: str) -> None:
        """Remove the worktree and branch that -w created for this session.

        No-op when worktree mode is disabled. Never raises. Sessions are
        ephemeral and any uncommitted state in the worktree is disposable, so
        on a clean-remove failure we retry with --force to prevent unbounded
        accumulation of dirty worktrees on disk.
        """
        if not self._config.worktree_enabled:
            return
        repo_root = self._config.work_dir
        worktree_path = repo_root / ".claude" / "worktrees" / session_id
        branch_name = f"worktree-{session_id}"

        if worktree_path.exists():
            rc, err = await self._run_git(
                repo_root, "worktree", "remove", str(worktree_path)
            )
            if rc != 0:
                # Common cause: untracked or modified files in the worktree.
                logger.info(
                    "Worktree remove for session %s failed (%s); retrying with --force",
                    session_id,
                    err,
                )
                rc, err = await self._run_git(
                    repo_root, "worktree", "remove", "--force", str(worktree_path)
                )
                if rc != 0:
                    logger.warning(
                        "Worktree force-remove failed for session %s "
                        "(leaving on disk): %s",
                        session_id,
                        err,
                    )
                    return
        else:
            # Worktree dir gone but admin entry may still linger
            await self._run_git(repo_root, "worktree", "prune")

        # Branch deletion is best-effort; it may already be gone
        await self._run_git(repo_root, "branch", "-D", branch_name)
        logger.info("Cleaned up worktree for session %s", session_id)

    @staticmethod
    async def _run_git(cwd: Path, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "timeout"
        return proc.returncode or 0, stderr.decode(errors="replace").strip()

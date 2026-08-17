from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.events import (
    ResultEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import BridgeEvent, Completion

logger = logging.getLogger(__name__)


class ClaudeController:
    def __init__(self, config: ClaudeConfig) -> None:
        self._config = config

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Run a Claude Code prompt and yield streaming BridgeEvents."""
        cwd = self._config.work_dir
        timeout = self._config.timeout_seconds

        cmd = self._build_command(session_id, prompt, is_new, system_prompt)
        logger.info("Running claude: %s (cwd=%s, timeout=%ss)", cmd, cwd, timeout)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            limit=10 * 1024 * 1024,  # 10 MB line buffer (default 64 KB is too small)
            start_new_session=True,  # isolate process group for clean tree cleanup
        )

        # Drain stderr in background to prevent buffer deadlock
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        timed_out = False
        result_seen = False
        try:
            async for event in self._read_stream_with_timeout(process, timeout):
                if isinstance(event, Completion):
                    result_seen = True
                yield event
        except TimeoutError:
            timed_out = True
            logger.error("Claude process timed out after %ss", timeout)
            yield Completion(
                text=f"Claude process timed out after {timeout}s",
                is_error=True,
            )
        finally:
            # Kill the whole process group up front. An orphaned grandchild
            # (e.g. a nested `claude -p` backgrounded by a skill) inherits the
            # stdout/stderr pipes and keeps them open, which would wedge both
            # process.wait() and the stderr drain on the still-open pipes.
            self._kill_process_tree(process, graceful=True)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                self._kill_process_tree(process, graceful=False)
                await process.wait()

            try:
                stderr_text = await asyncio.wait_for(stderr_task, timeout=5.0)
            except TimeoutError:
                stderr_task.cancel()
                stderr_text = ""
            return_code = process.returncode

            # Once a result was streamed, the task succeeded — a non-zero exit
            # code is just our own group teardown (signal), not a failure.
            # Without a result the run failed no matter the exit code: a clean
            # exit 0 that never emitted `result` must still complete the
            # stream, or consumers hang on a contract violation.
            if not timed_out and not result_seen:
                if stderr_text:
                    logger.error("Claude stderr: %s", stderr_text[:500])
                yield Completion(
                    text=(
                        f"Claude process exited with code {return_code} "
                        "before emitting a result"
                    ),
                    is_error=True,
                )

    def _build_command(
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

    async def _read_stream_with_timeout(
        self,
        process: asyncio.subprocess.Process,
        # Manual deadline bookkeeping (not asyncio.timeout): one overall budget
        # is re-split across many readline waits below.
        timeout: float,  # noqa: ASYNC109
    ) -> AsyncIterator[BridgeEvent]:
        """Read stdout stream with an overall timeout, yielding BridgeEvents."""
        deadline = asyncio.get_event_loop().time() + timeout
        # Narrowing only: stdout=PIPE is always set by run().
        assert process.stdout is not None  # noqa: S101
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError()
            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(), timeout=remaining
                )
            except TimeoutError:
                raise
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            logger.debug("Raw stream line: %s", line.rstrip())
            for claude_event in parse_stream_line(line):
                logger.debug("Parsed Claude event: %s", claude_event)
                bridge_event = to_bridge_event(claude_event)
                if bridge_event is not None:
                    logger.debug("Converted to BridgeEvent: %s", bridge_event)
                    yield bridge_event
                else:
                    logger.debug(
                        "Filtered out (internal): %s", type(claude_event).__name__
                    )
                if isinstance(claude_event, ResultEvent):
                    # `result` is the terminal line; stop reading instead of
                    # blocking on EOF, which may never arrive if a backgrounded
                    # grandchild still holds the inherited stdout pipe open.
                    return

    @staticmethod
    def _kill_process_tree(
        process: asyncio.subprocess.Process, *, graceful: bool
    ) -> None:
        """Kill the entire process group (main process + all children).

        Requires the subprocess to have been started with start_new_session=True
        so it has its own process group.
        """
        pid = process.pid
        sig = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            # start_new_session=True guarantees PGID == PID, so use pid
            # directly instead of os.getpgid() which fails after process exits
            os.killpg(pid, sig)
            logger.info("Sent %s to process group (pid=%d)", sig.name, pid)
        except ProcessLookupError:
            pass  # entire group already exited
        except OSError:
            # Fallback: kill just the main process
            logger.warning("killpg failed for pid=%d, falling back to direct kill", pid)
            with contextlib.suppress(ProcessLookupError):
                process.terminate() if graceful else process.kill()

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> str:
        """Read all stderr in background to prevent pipe buffer deadlock."""
        # Narrowing only: stderr=PIPE is always set by run().
        assert process.stderr is not None  # noqa: S101
        stderr_bytes = await process.stderr.read()
        return stderr_bytes.decode(errors="replace").strip()

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

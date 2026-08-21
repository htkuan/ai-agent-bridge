from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from agent_bridge.bridge.events import BridgeEvent, Completion

logger = logging.getLogger(__name__)


@dataclass
class RunState:
    """Per-run parser state. Subclass with whatever the agent's parser
    accumulates across lines; ``parse_line`` sets ``terminal`` once the
    stream's terminal event is seen so the engine stops reading instead of
    blocking on an EOF that may never arrive (an orphaned grandchild can
    hold the inherited stdout pipe open).
    """

    terminal: bool = False


class CliAgentController[RunStateT: RunState]:
    """Shared CLI-agent engine: spawn → stream-parse → teardown.

    The ``AgentController`` protocol stays the contract; subclassing this is
    optional reuse. The base owns the subprocess mechanics — process-group
    isolation, background stderr draining, one overall deadline, tree kill,
    and the guarantee that every stream ends with exactly one ``Completion``.
    A subclass describes its CLI: ``build_command`` for the invocation,
    ``parse_line`` for turning stdout lines into bridge events (updating its
    ``RunState`` along the way), and optionally ``on_stream_end`` to
    synthesize the final ``Completion`` for CLIs whose stream has no
    terminal event.
    """

    # Prefixes log and error-completion messages ("Claude process timed out…").
    agent_name: str = "Agent"

    def __init__(self, *, work_dir: Path, timeout_seconds: float) -> None:
        self._work_dir = work_dir
        self._timeout_seconds = timeout_seconds

    # --- hooks a subclass must implement ---

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def new_run_state(self) -> RunStateT:
        raise NotImplementedError

    def parse_line(self, line: str, state: RunStateT) -> list[BridgeEvent]:
        raise NotImplementedError

    # --- hooks with working defaults ---

    def on_stream_end(
        self, state: RunStateT, return_code: int | None, stderr: str
    ) -> Completion | None:
        """Build the final ``Completion`` when the stream ended without one.

        Agents whose CLI has no terminal event synthesize their result here
        from the accumulated ``state``. Returning None (the default) makes
        the engine yield its generic error ``Completion`` instead.
        """
        return None

    async def cleanup_session(self, session_id: str) -> None:
        """Release per-session resources (worktrees, id mappings, …).

        The app's cleanup loop calls this for every purged session on every
        controller, including ones that never saw the session — it must stay
        a cheap no-op then, and must never raise for a session it did own.
        """

    # --- the engine ---

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Run one prompt through the agent's CLI, yielding BridgeEvents."""
        cwd = self._work_dir
        timeout = self._timeout_seconds

        cmd = self.build_command(session_id, prompt, is_new, system_prompt)
        logger.info(
            "Running %s: %s (cwd=%s, timeout=%ss)", self.agent_name, cmd, cwd, timeout
        )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            # Some CLIs read (or announce reading) stdin whenever it isn't a
            # TTY; an explicit EOF keeps the stream pure and the process
            # unblocked.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            limit=10 * 1024 * 1024,  # 10 MB line buffer (default 64 KB is too small)
            start_new_session=True,  # isolate process group for clean tree cleanup
        )

        # Drain stderr in background to prevent buffer deadlock
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        state = self.new_run_state()
        timed_out = False
        result_seen = False
        try:
            async for event in self._read_stream_with_timeout(process, timeout, state):
                if isinstance(event, Completion):
                    result_seen = True
                yield event
        except TimeoutError:
            timed_out = True
            logger.error("%s process timed out after %ss", self.agent_name, timeout)
            yield Completion(
                text=f"{self.agent_name} process timed out after {timeout}s",
                is_error=True,
            )
        finally:
            # Kill the whole process group up front. An orphaned grandchild
            # (e.g. a nested CLI backgrounded by the agent) inherits the
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
            # Without a result the run must still complete the stream, or
            # consumers hang on a contract violation: the subclass gets first
            # say (a terminal-event-less CLI synthesizes success here), then
            # the generic error Completion covers the rest.
            if not timed_out and not result_seen:
                completion = self.on_stream_end(state, return_code, stderr_text)
                if completion is None:
                    if stderr_text:
                        logger.error(
                            "%s stderr: %s", self.agent_name, stderr_text[:500]
                        )
                    completion = Completion(
                        text=(
                            f"{self.agent_name} process exited with code "
                            f"{return_code} before emitting a result"
                        ),
                        is_error=True,
                    )
                yield completion

    async def _read_stream_with_timeout(
        self,
        process: asyncio.subprocess.Process,
        # Manual deadline bookkeeping (not asyncio.timeout): one overall budget
        # is re-split across many readline waits below.
        timeout: float,  # noqa: ASYNC109
        state: RunStateT,
    ) -> AsyncIterator[BridgeEvent]:
        """Read stdout with an overall timeout, yielding parsed BridgeEvents."""
        deadline = asyncio.get_event_loop().time() + timeout
        # Narrowing only: stdout=PIPE is always set by run().
        assert process.stdout is not None  # noqa: S101
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError()
            line_bytes = await asyncio.wait_for(
                process.stdout.readline(), timeout=remaining
            )
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            logger.debug("Raw stream line: %s", line.rstrip())
            for event in self.parse_line(line, state):
                yield event
            if state.terminal:
                # The terminal line was parsed; stop reading instead of
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

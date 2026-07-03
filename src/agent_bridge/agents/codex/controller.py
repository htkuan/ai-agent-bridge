from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.events import (
    TERMINAL_EVENTS,
    CodexEvent,
    ThreadStartedEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import BridgeEvent, Completion, TextDelta

logger = logging.getLogger(__name__)

# `codex exec` has no stable system-prompt flag; platform directives are
# prepended to the prompt with an explicit delimiter (documented behavior).
_DIRECTIVES_TEMPLATE = "<platform-directives>\n{system_prompt}\n</platform-directives>\n\n{prompt}"


class CodexSessionMap:
    """Persisted ``bridge_session_id → codex thread_id`` mapping.

    Codex mints its own thread id (from the ``thread.started`` event), so the
    controller must remember which native thread backs each bridge session.
    Stored as a flat JSON object. Write failures are logged, never raised —
    worst case a restart loses resume continuity and the controller falls
    back to a fresh thread.
    """

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._map: dict[str, str] = {}
        self._load()

    def get(self, session_id: str) -> str | None:
        return self._map.get(session_id)

    def set(self, session_id: str, native_id: str) -> None:
        self._map[session_id] = native_id
        self._save()

    def remove(self, session_id: str) -> None:
        if self._map.pop(session_id, None) is not None:
            self._save()

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load codex session map: %s", e)
            return
        if not isinstance(data, dict):
            logger.warning("Codex session map is not a JSON object, ignoring: %s", self._store_path)
            return
        self._map = {str(k): str(v) for k, v in data.items()}

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(self._map, indent=2))
        except OSError as e:
            logger.error("Failed to save codex session map: %s", e)


class CodexController:
    def __init__(self, config: CodexConfig) -> None:
        self._config = config
        self._session_map = CodexSessionMap(config.session_map_path)

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Run a Codex CLI prompt and yield streaming BridgeEvents."""
        timeout = self._config.timeout_seconds

        native_id: str | None = None
        if not is_new:
            native_id = self._session_map.get(session_id)
            if native_id is None:
                # Mapping lost (restart, deleted map file) or first sighting —
                # degrade to a fresh thread instead of failing the message.
                logger.warning(
                    "No codex thread mapping for session %s; starting a fresh thread",
                    session_id,
                )

        cmd = self._build_command(prompt, native_id, system_prompt)
        logger.info("Running codex: %s (cwd=%s, timeout=%ss)", cmd, self._config.work_dir, timeout)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._config.work_dir),
            limit=10 * 1024 * 1024,  # 10 MB line buffer (default 64 KB is too small)
            start_new_session=True,  # isolate process group for clean tree cleanup
        )

        # Drain stderr in background to prevent buffer deadlock
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        started_at = asyncio.get_event_loop().time()
        timed_out = False
        result_seen = False
        last_agent_text = ""
        try:
            async for codex_event in self._read_stream_with_timeout(process, timeout):
                if isinstance(codex_event, ThreadStartedEvent):
                    if native_id is None and codex_event.thread_id:
                        self._session_map.set(session_id, codex_event.thread_id)
                    continue
                bridge_event = to_bridge_event(codex_event)
                if bridge_event is None:
                    continue
                if isinstance(bridge_event, TextDelta):
                    last_agent_text = bridge_event.text
                if isinstance(bridge_event, Completion):
                    result_seen = True
                    # turn.completed carries only usage; the final text is the
                    # last agent message. The CLI reports no durations either,
                    # so measure wall-clock ourselves.
                    if not bridge_event.is_error and not bridge_event.text:
                        bridge_event.text = last_agent_text
                    bridge_event.duration_ms = int(
                        (asyncio.get_event_loop().time() - started_at) * 1000
                    )
                yield bridge_event
        except TimeoutError:
            timed_out = True
            logger.error("Codex process timed out after %ss", timeout)
            yield Completion(
                text=f"Codex process timed out after {timeout}s",
                is_error=True,
            )
        finally:
            # Kill the whole process group up front — an orphaned grandchild
            # holding the inherited stdout pipe would otherwise wedge both
            # process.wait() and the stderr drain (same pattern as claude).
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

            # Once a terminal event was streamed, the task finished — a
            # non-zero exit is just our own group teardown, not a failure.
            if not timed_out and not result_seen and return_code and return_code != 0:
                if stderr_text:
                    logger.error("Codex stderr: %s", stderr_text[:500])
                yield Completion(
                    text=f"Codex process exited with code {return_code}",
                    is_error=True,
                )

    def _build_command(
        self,
        prompt: str,
        native_id: str | None,
        system_prompt: str | None = None,
    ) -> list[str]:
        cmd = ["codex", "exec"]
        if native_id is not None:
            cmd.extend(["resume", native_id])
        cmd.extend(["--json", "--sandbox", self._config.sandbox])
        # `codex exec` refuses to run outside a git repo by default; the
        # bridge work_dir is not necessarily one, and the sandbox mode is the
        # guardrail that matters here.
        cmd.append("--skip-git-repo-check")
        if self._config.model:
            cmd.extend(["-m", self._config.model])
        if system_prompt:
            prompt = _DIRECTIVES_TEMPLATE.format(system_prompt=system_prompt, prompt=prompt)
        cmd.append(prompt)
        return cmd

    async def _read_stream_with_timeout(
        self, process: asyncio.subprocess.Process, timeout: float
    ) -> AsyncIterator[CodexEvent]:
        """Read stdout with an overall timeout, yielding parsed CodexEvents.

        Stops right after the terminal event (turn.completed / turn.failed /
        error) instead of waiting for EOF, which may never arrive if a
        backgrounded grandchild still holds the inherited stdout pipe open.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        assert process.stdout is not None
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError()
            line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            logger.debug("Raw codex stream line: %s", line.rstrip())
            for codex_event in parse_stream_line(line):
                yield codex_event
                if isinstance(codex_event, TERMINAL_EVENTS):
                    return

    @staticmethod
    def _kill_process_tree(process: asyncio.subprocess.Process, *, graceful: bool) -> None:
        """Kill the entire process group (main process + all children)."""
        pid = process.pid
        if pid is None:
            return
        sig = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            # start_new_session=True guarantees PGID == PID
            os.killpg(pid, sig)
            logger.info("Sent %s to process group (pid=%d)", sig.name, pid)
        except ProcessLookupError:
            pass  # entire group already exited
        except OSError:
            logger.warning("killpg failed for pid=%d, falling back to direct kill", pid)
            with contextlib.suppress(ProcessLookupError):
                process.terminate() if graceful else process.kill()

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> str:
        """Read all stderr in background to prevent pipe buffer deadlock."""
        assert process.stderr is not None
        stderr_bytes = await process.stderr.read()
        return stderr_bytes.decode(errors="replace").strip()

    async def cleanup_session(self, session_id: str) -> None:
        """Drop the thread mapping for an expired session. Never raises."""
        try:
            self._session_map.remove(session_id)
        except Exception:
            logger.exception("Failed to clean up codex session %s", session_id)

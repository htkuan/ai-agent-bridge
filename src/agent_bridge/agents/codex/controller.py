from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.events import (
    CodexEventTranslator,
    ThreadStartedEvent,
    parse_stream_line,
)
from agent_bridge.agents.codex.thread_map import ThreadMap
from agent_bridge.events import BridgeEvent, Completion

logger = logging.getLogger(__name__)


class CodexController:
    """AgentController backed by `codex exec --json`.

    Implements the :class:`AgentController` protocol:

        run(session_id, prompt, is_new, context=None, system_prompt=None)
            -> AsyncIterator[BridgeEvent]

    Codex CLI does not accept an externally provided session id, so the
    bridge UUID and the codex thread id are kept distinct: this controller
    captures codex's own ``thread_id`` from ``thread.started`` and persists
    it into :class:`ThreadMap` keyed by the bridge ``session_id``. Resume
    looks the codex id back up.

    The controller is platform-agnostic: it never inspects ``context``, and
    it forwards ``prompt`` and ``system_prompt`` to codex as opaque strings.
    """

    def __init__(self, config: CodexConfig) -> None:
        self._config = config
        self._thread_map = ThreadMap(config.thread_map_path)

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        cwd = self._config.work_dir
        timeout = self._config.timeout_seconds

        codex_thread_id = None if is_new else self._thread_map.get(session_id)
        if not is_new and codex_thread_id is None:
            logger.warning(
                "No codex thread_id stored for bridge session %s; starting fresh",
                session_id,
            )

        cmd = self._build_command(codex_thread_id)
        stdin_payload = _compose_prompt(system_prompt, prompt)

        logger.info(
            "Running codex: %s (cwd=%s, timeout=%ss, resume=%s)",
            cmd,
            cwd,
            timeout,
            codex_thread_id,
        )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            limit=10 * 1024 * 1024,
            start_new_session=True,
        )

        await self._write_stdin(process, stdin_payload)

        stderr_task = asyncio.create_task(self._drain_stderr(process))
        translator = CodexEventTranslator()
        timed_out = False
        completion_emitted = False

        try:
            async for codex_event in self._read_events(process, timeout):
                # thread.started carries the codex-side id we have to persist.
                # Side-effect lives here, not in the translator, so the
                # translator stays free of I/O and is fully unit-testable.
                if isinstance(codex_event, ThreadStartedEvent):
                    if codex_event.thread_id:
                        self._thread_map.set(session_id, codex_event.thread_id)
                    continue

                for bridge_event in translator.translate(codex_event):
                    if isinstance(bridge_event, Completion):
                        completion_emitted = True
                    yield bridge_event
        except asyncio.TimeoutError:
            timed_out = True
            logger.error("Codex process timed out after %ss", timeout)
            yield Completion(
                text=f"Codex process timed out after {timeout}s",
                is_error=True,
            )
            completion_emitted = True
        finally:
            if process.returncode is None:
                self._kill_process_tree(process, graceful=True)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._kill_process_tree(process, graceful=False)
                    await process.wait()
            else:
                self._kill_process_tree(process, graceful=True)

            stderr_text = await stderr_task
            return_code = process.returncode

            # Fallback Completion: callers (and platforms) rely on exactly one
            # Completion per run to know the conversation finished. If codex
            # ended without emitting turn.completed/turn.failed/error we have
            # to synthesize one — otherwise the platform adapter hangs.
            if not timed_out and not completion_emitted:
                if return_code and return_code != 0:
                    if stderr_text:
                        logger.error("Codex stderr: %s", stderr_text[:500])
                    yield Completion(
                        text=f"Codex process exited with code {return_code}",
                        is_error=True,
                    )
                else:
                    logger.warning(
                        "Codex finished without emitting turn.completed; "
                        "synthesizing fallback Completion"
                    )
                    yield Completion(
                        text=translator.last_assistant_text,
                        is_error=False,
                    )

    async def cleanup_session(self, session_id: str) -> None:
        """Drop the bridge_session_id → codex_thread_id mapping.

        Called by the bridge layer when a session is purged (TTL expiry,
        explicit reset, etc). Never raises.
        """
        if self._thread_map.delete(session_id):
            logger.info("Cleaned up codex thread mapping for session %s", session_id)

    # --- internals -----------------------------------------------------

    def _build_command(self, codex_thread_id: str | None) -> list[str]:
        # `-s/-C/-a/-m` are TOP-LEVEL flags on codex 0.128+ and must come
        # BEFORE the `exec` subcommand. `--json` and `--skip-git-repo-check`
        # belong to the `exec` (or `exec resume`) subcommand.
        cmd: list[str] = ["codex"]
        cmd.extend(["-s", self._config.sandbox])
        cmd.extend(["-a", self._config.approval])
        cmd.extend(["-C", str(self._config.work_dir)])
        if self._config.model:
            cmd.extend(["-m", self._config.model])
        for entry in self._config.extra_config:
            cmd.extend(["-c", entry])

        if codex_thread_id:
            cmd.extend(["exec", "resume", codex_thread_id])
        else:
            cmd.append("exec")

        cmd.append("--json")
        if self._config.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")

        # Trailing `-` tells codex to read the prompt from stdin.
        cmd.append("-")
        return cmd

    @staticmethod
    async def _write_stdin(
        process: asyncio.subprocess.Process, payload: str
    ) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(payload.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning("Failed to write codex stdin: %s", e)
        finally:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    async def _read_events(
        self, process: asyncio.subprocess.Process, timeout: float
    ):
        deadline = asyncio.get_event_loop().time() + timeout
        assert process.stdout is not None
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line_bytes = await asyncio.wait_for(
                process.stdout.readline(), timeout=remaining
            )
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            logger.debug("Raw codex line: %s", line.rstrip())
            for codex_event in parse_stream_line(line):
                logger.debug("Parsed codex event: %s", codex_event)
                yield codex_event

    @staticmethod
    def _kill_process_tree(
        process: asyncio.subprocess.Process, *, graceful: bool
    ) -> None:
        pid = process.pid
        if pid is None:
            return
        sig = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            os.killpg(pid, sig)
            logger.info("Sent %s to codex process group (pid=%d)", sig.name, pid)
        except ProcessLookupError:
            pass
        except OSError:
            logger.warning("killpg failed for pid=%d, falling back to direct kill", pid)
            try:
                process.terminate() if graceful else process.kill()
            except ProcessLookupError:
                pass

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> str:
        assert process.stderr is not None
        stderr_bytes = await process.stderr.read()
        return stderr_bytes.decode(errors="replace").strip()


def _compose_prompt(system_prompt: str | None, prompt: str) -> str:
    """Combine platform system_prompt and user prompt for codex stdin.

    Codex CLI has no `--append-system-prompt` equivalent, so we inline both
    into a single stdin payload with explicit tags so the model can tell
    them apart. When ``system_prompt`` is empty the user prompt is sent on
    its own to keep simple invocations clean.
    """
    if not system_prompt:
        return prompt
    return f"<system>\n{system_prompt}\n</system>\n\n<user>\n{prompt}\n</user>\n"

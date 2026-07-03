from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.events import (
    TERMINAL_EVENTS,
    OpencodeEvent,
    StepFinishedEvent,
    StepStartedEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import BridgeEvent, Completion, TextDelta

logger = logging.getLogger(__name__)

# `opencode run` has no system-prompt flag; platform directives are prepended
# to the prompt with an explicit delimiter (documented behavior, same as codex).
_DIRECTIVES_TEMPLATE = "<platform-directives>\n{system_prompt}\n</platform-directives>\n\n{prompt}"


class OpencodeSessionMap:
    """Persisted ``bridge_session_id → opencode session_id`` mapping.

    `opencode run --session` only *continues* an existing session — the CLI
    mints its own ids (``ses_...``, reported on every JSON event), so the
    controller must remember which native session backs each bridge session.
    Stored as a flat JSON object. Write failures are logged, never raised —
    worst case a restart loses resume continuity and the controller falls
    back to a fresh session.
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
            logger.warning("Failed to load opencode session map: %s", e)
            return
        if not isinstance(data, dict):
            logger.warning(
                "OpenCode session map is not a JSON object, ignoring: %s",
                self._store_path,
            )
            return
        self._map = {str(k): str(v) for k, v in data.items()}

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(self._map, indent=2))
        except OSError as e:
            logger.error("Failed to save opencode session map: %s", e)


class OpencodeController:
    def __init__(self, config: OpencodeConfig) -> None:
        self._config = config
        self._session_map = OpencodeSessionMap(config.session_map_path)

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Run an OpenCode CLI prompt and yield streaming BridgeEvents."""
        timeout = self._config.timeout_seconds

        native_id: str | None = None
        if not is_new:
            native_id = self._session_map.get(session_id)
            if native_id is None:
                # Mapping lost (restart, deleted map file) or first sighting —
                # degrade to a fresh session instead of failing the message.
                logger.warning(
                    "No opencode session mapping for session %s; starting fresh",
                    session_id,
                )

        cmd = self._build_command(prompt, native_id, system_prompt)
        logger.info(
            "Running opencode: %s (cwd=%s, timeout=%ss)",
            cmd,
            self._config.work_dir,
            timeout,
        )

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
        session_captured = native_id is not None
        # Completion.text = the text parts of the last text-producing step
        # (the final answer); usage/cost accumulate across all steps.
        final_text_parts: list[str] = []
        step_index = 0
        text_step = -1
        steps_finished = 0
        cost_usd = 0.0
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "num_turns": 0,
        }
        try:
            async for oc_event in self._read_stream_with_timeout(process, timeout):
                if not session_captured and oc_event.session_id:
                    self._session_map.set(session_id, oc_event.session_id)
                    session_captured = True
                match oc_event:
                    case StepStartedEvent():
                        step_index += 1
                        continue
                    case StepFinishedEvent() as step:
                        steps_finished += 1
                        cost_usd += step.cost
                        usage["input_tokens"] += step.input_tokens
                        usage["output_tokens"] += step.output_tokens
                        usage["cache_read_tokens"] += step.cache_read_tokens
                        usage["cache_creation_tokens"] += step.cache_write_tokens
                        continue
                bridge_event = to_bridge_event(oc_event)
                if bridge_event is None:
                    continue
                if isinstance(bridge_event, TextDelta):
                    if step_index != text_step:
                        final_text_parts.clear()
                        text_step = step_index
                    final_text_parts.append(bridge_event.text)
                if isinstance(bridge_event, Completion):  # session error
                    result_seen = True
                    bridge_event.duration_ms = int(
                        (asyncio.get_event_loop().time() - started_at) * 1000
                    )
                yield bridge_event
        except TimeoutError:
            timed_out = True
            logger.error("OpenCode process timed out after %ss", timeout)
            yield Completion(
                text=f"OpenCode process timed out after {timeout}s",
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

            if not timed_out and not result_seen:
                if return_code and return_code != 0:
                    if stderr_text:
                        logger.error("OpenCode stderr: %s", stderr_text[:500])
                    yield Completion(
                        text=f"OpenCode process exited with code {return_code}",
                        is_error=True,
                    )
                else:
                    # The CLI prints no terminal event — a clean exit (EOF) is
                    # the success signal, so synthesize the Completion here.
                    # It reports no durations either; measure wall-clock.
                    completion = Completion(
                        text="\n\n".join(final_text_parts),
                        is_error=False,
                        cost_usd=cost_usd,
                        duration_ms=int((asyncio.get_event_loop().time() - started_at) * 1000),
                    )
                    if steps_finished:
                        usage["num_turns"] = steps_finished
                        completion.metadata = {"usage": usage}
                    yield completion

    def _build_command(
        self,
        prompt: str,
        native_id: str | None,
        system_prompt: str | None = None,
    ) -> list[str]:
        cmd = ["opencode", "run", "--format", "json"]
        if native_id is not None:
            cmd.extend(["--session", native_id])
        if self._config.model:
            cmd.extend(["--model", self._config.model])
        if system_prompt:
            prompt = _DIRECTIVES_TEMPLATE.format(system_prompt=system_prompt, prompt=prompt)
        cmd.append(prompt)
        return cmd

    async def _read_stream_with_timeout(
        self, process: asyncio.subprocess.Process, timeout: float
    ) -> AsyncIterator[OpencodeEvent]:
        """Read stdout with an overall timeout, yielding parsed OpencodeEvents.

        Reads until EOF — `opencode run` exits once the session goes idle and
        emits no success terminal event. A session error is fatal (the CLI
        exits 1 after it), so reading stops right there.
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
            logger.debug("Raw opencode stream line: %s", line.rstrip())
            for oc_event in parse_stream_line(line):
                yield oc_event
                if isinstance(oc_event, TERMINAL_EVENTS):
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
        """Drop the session mapping for an expired session. Never raises."""
        try:
            self._session_map.remove(session_id)
        except Exception:
            logger.exception("Failed to clean up opencode session %s", session_id)

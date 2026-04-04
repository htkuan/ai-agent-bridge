from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.events import (
    ResultEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.events import BridgeEvent, Completion

logger = logging.getLogger(__name__)


class ClaudeController:
    def __init__(self, config: ClaudeConfig) -> None:
        self._config = config

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        work_dir: Path | None = None,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        """Run a Claude Code prompt and yield streaming BridgeEvents."""
        cwd = work_dir or self._config.work_dir
        timeout = self._config.timeout_seconds

        cmd = self._build_command(session_id, prompt, is_new, context)
        logger.info("Running claude: %s (cwd=%s, timeout=%ss)", cmd[:5], cwd, timeout)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )

        # Drain stderr in background to prevent buffer deadlock
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        timed_out = False
        try:
            async for event in self._read_stream_with_timeout(process, timeout):
                yield event
        except asyncio.TimeoutError:
            timed_out = True
            logger.error("Claude process timed out after %ss", timeout)
            yield Completion(
                text=f"Claude process timed out after {timeout}s",
                is_error=True,
            )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            stderr_text = await stderr_task
            return_code = process.returncode

            if not timed_out and return_code and return_code != 0:
                if stderr_text:
                    logger.error("Claude stderr: %s", stderr_text[:500])
                yield Completion(
                    text=f"Claude process exited with code {return_code}",
                    is_error=True,
                )

    def _build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
    ) -> list[str]:
        # Prefix prompt with sender identity so Claude knows who is speaking
        if context:
            user_name = context.get("user_name", "unknown")
            user_id = context.get("user_id", "")
            tag = f"{user_name} ({user_id})" if user_id else user_name
            tagged_prompt = f"[{tag}]: {prompt}"
        else:
            tagged_prompt = prompt

        cmd = [
            "claude",
            "-p",
            tagged_prompt,
            "--output-format",
            "stream-json",
        ]

        if is_new:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        permission_mode = self._config.permission_mode
        if permission_mode == "dangerously-skip-permissions":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", permission_mode])

        if context:
            parts = [
                f"Platform: {context.get('platform', 'unknown')}",
            ]
            if context.get("workspace"):
                parts.append(f"Workspace: {context['workspace']}")
            channel_name = context.get("channel_name", "")
            channel_id = context.get("channel_id", "")
            if channel_name and channel_id:
                parts.append(f"Channel: #{channel_name} ({channel_id})")
            elif channel_id:
                parts.append(f"Channel: {channel_id}")
            if context.get("thread_ts"):
                parts.append(f"Thread: {context['thread_ts']}")

            system_prompt = (
                "This conversation is from a chat platform. "
                "Each message is prefixed with [user_name (user_id)] to identify the speaker.\n"
                + "\n".join(parts)
            )
            cmd.extend(["--append-system-prompt", system_prompt])

        return cmd

    async def _read_stream_with_timeout(
        self, process: asyncio.subprocess.Process, timeout: float
    ) -> AsyncIterator[BridgeEvent]:
        """Read stdout stream with an overall timeout, yielding BridgeEvents."""
        deadline = asyncio.get_event_loop().time() + timeout
        assert process.stdout is not None
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            for claude_event in parse_stream_line(line):
                bridge_event = to_bridge_event(claude_event)
                if bridge_event is not None:
                    yield bridge_event

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> str:
        """Read all stderr in background to prevent pipe buffer deadlock."""
        assert process.stderr is not None
        stderr_bytes = await process.stderr.read()
        return stderr_bytes.decode(errors="replace").strip()

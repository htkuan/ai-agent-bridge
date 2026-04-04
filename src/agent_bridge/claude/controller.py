from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from agent_bridge.claude.events import Event, ResultEvent, parse_stream_line
from agent_bridge.config import Config

logger = logging.getLogger(__name__)


class ClaudeController:
    def __init__(self, config: Config) -> None:
        self._config = config

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        work_dir: Path | None = None,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[Event]:
        """Run a Claude Code prompt and yield streaming events."""
        cwd = work_dir or self._config.claude_work_dir

        cmd = self._build_command(session_id, prompt, is_new, context)
        logger.info("Running claude: %s (cwd=%s)", cmd[:5], cwd)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )

        try:
            async for event in self._read_stream(process):
                yield event
        finally:
            # Ensure process is cleaned up
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()

            return_code = process.returncode
            if return_code and return_code != 0:
                stderr_bytes = await process.stderr.read() if process.stderr else b""
                stderr_text = stderr_bytes.decode(errors="replace").strip()
                if stderr_text:
                    logger.error("Claude process stderr: %s", stderr_text[:500])
                yield ResultEvent(
                    session_id=session_id,
                    result_text=f"Claude process exited with code {return_code}",
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

        permission_mode = self._config.claude_permission_mode
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

    async def _read_stream(
        self, process: asyncio.subprocess.Process
    ) -> AsyncIterator[Event]:
        assert process.stdout is not None
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace")
            event = parse_stream_line(line)
            if event is not None:
                yield event

from __future__ import annotations

import logging

from agent_bridge.agents.base import CliAgentController
from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.events import (
    CodexRunState,
    ThreadStartedEvent,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.agents.handles import SessionHandleStore
from agent_bridge.bridge.events import BridgeEvent

logger = logging.getLogger(__name__)


class CodexController(CliAgentController[CodexRunState]):
    agent_name = "Codex"

    def __init__(self, config: CodexConfig) -> None:
        super().__init__(
            work_dir=config.work_dir, timeout_seconds=config.timeout_seconds
        )
        self._config = config
        self._handles = SessionHandleStore(config.resolved_session_map_path)

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        if is_new:
            return self._new_session_command()

        thread_id = self._handles.get(session_id)
        if thread_id is None:
            logger.warning(
                "No Codex thread id for bridge session %s; starting a new session",
                session_id,
            )
            return self._new_session_command()
        return self._resume_command(thread_id)

    def stdin_payload(self, prompt: str, system_prompt: str | None) -> bytes | None:
        if not system_prompt:
            return prompt.encode()
        return (
            f"<system_directives>\n{system_prompt}\n</system_directives>\n\n{prompt}"
        ).encode()

    def new_run_state(self) -> CodexRunState:
        return CodexRunState()

    def parse_line(self, line: str, state: CodexRunState) -> list[BridgeEvent]:
        events: list[BridgeEvent] = []
        for codex_event in parse_stream_line(line):
            bridge_event = to_bridge_event(codex_event, state)
            if isinstance(codex_event, ThreadStartedEvent) and state.session_id:
                self._handles.put(state.session_id, codex_event.thread_id)
            if bridge_event is not None:
                events.append(bridge_event)
        return events

    async def cleanup_session(self, session_id: str) -> None:
        self._handles.discard(session_id)

    def _new_session_command(self) -> list[str]:
        cmd = [
            self._config.cli_path,
            "exec",
            "--json",
            "--sandbox",
            self._config.sandbox_mode,
        ]
        self._append_optional_flags(cmd)
        if self._config.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        cmd.append("-")
        return cmd

    def _resume_command(self, thread_id: str) -> list[str]:
        cmd = [
            self._config.cli_path,
            "exec",
            "resume",
            thread_id,
            "--json",
            "-c",
            f'sandbox_mode="{self._config.sandbox_mode}"',
        ]
        self._append_optional_flags(cmd)
        if self._config.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        cmd.append("-")
        return cmd

    def _append_optional_flags(self, cmd: list[str]) -> None:
        if self._config.model:
            cmd.extend(["-m", self._config.model])
        if self._config.effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self._config.effort}"'])

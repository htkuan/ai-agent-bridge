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
        # Codex mints its own thread id and cannot accept the bridge's
        # session id — this maps bridge session_id → codex thread_id.
        self._handles = SessionHandleStore(config.resolved_session_map_path)

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        thread_id = None if is_new else self._handles.get(session_id)
        if not is_new and thread_id is None:
            # Graceful degrade: the mapping is re-recorded from the fresh
            # run's thread.started, so only this turn loses its history.
            logger.warning(
                "No codex thread recorded for session %s; starting a new thread",
                session_id,
            )
        if thread_id is None:
            cmd = [
                self._config.cli_path,
                "exec",
                "--json",
                "--sandbox",
                self._config.sandbox_mode,
            ]
        else:
            cmd = [
                self._config.cli_path,
                "exec",
                "resume",
                thread_id,
                "--json",
                # `resume` doesn't accept --sandbox; only the config override
                # spelling works there.
                "-c",
                f'sandbox_mode="{self._config.sandbox_mode}"',
            ]
        if self._config.model:
            cmd.extend(["-m", self._config.model])
        if self._config.effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self._config.effort}"'])
        # On resume too: codex re-runs the trusted-directory probe there, so
        # gating this on thread_id would strand every non-git work dir after
        # its first turn (caught live by test_live_run_resumes_the_same_agent_session).
        if self._config.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        # The prompt is deliberately NOT here — the trailing "-" makes codex
        # read it from stdin, keeping user text out of argv.
        cmd.append("-")
        return cmd

    def stdin_payload(
        self, prompt: str, system_prompt: str | None = None
    ) -> bytes | None:
        # Codex has no system-prompt flag: fold it into the prompt as a
        # tagged block, on every turn (platforms resend it each call).
        if system_prompt:
            return (
                f"<system_directives>\n{system_prompt}\n</system_directives>"
                f"\n\n{prompt}"
            ).encode()
        return prompt.encode()

    def new_run_state(self) -> CodexRunState:
        return CodexRunState()

    def parse_line(self, line: str, state: CodexRunState) -> list[BridgeEvent]:
        events: list[BridgeEvent] = []
        for codex_event in parse_stream_line(line):
            bridge_event = to_bridge_event(codex_event, state)
            if bridge_event is not None:
                events.append(bridge_event)
            # The store write lives here to keep events.py pure. Upserting on
            # every thread.started is safe: resume runs re-emit the same id.
            if isinstance(codex_event, ThreadStartedEvent) and codex_event.thread_id:
                self._handles.put(state.session_id, codex_event.thread_id)
        return events

    async def cleanup_session(self, session_id: str) -> None:
        self._handles.discard(session_id)

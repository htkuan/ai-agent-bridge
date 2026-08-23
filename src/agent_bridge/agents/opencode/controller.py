from __future__ import annotations

import logging

from agent_bridge.agents.base import CliAgentController
from agent_bridge.agents.handles import SessionHandleStore
from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.events import (
    OpencodeRunState,
    SessionAnnouncedEvent,
    completion_at_eof,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import BridgeEvent, Completion

logger = logging.getLogger(__name__)


class OpencodeController(CliAgentController[OpencodeRunState]):
    agent_name = "Opencode"

    def __init__(self, config: OpencodeConfig) -> None:
        super().__init__(
            work_dir=config.work_dir, timeout_seconds=config.timeout_seconds
        )
        self._config = config
        # Opencode mints its own session id (ses_…) and cannot accept the
        # bridge's — this maps bridge session_id → opencode session id.
        self._handles = SessionHandleStore(config.resolved_session_map_path)

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        handle = None if is_new else self._handles.get(session_id)
        if not is_new and handle is None:
            # Graceful degrade: the mapping is re-recorded from the fresh
            # run's stream, so only this turn loses its history.
            logger.warning(
                "No opencode session recorded for session %s; starting a new one",
                session_id,
            )
        cmd = [self._config.cli_path, "run", "--format", "json"]
        if handle is None:
            # Debuggability only: opencode session lists show the title.
            cmd.extend(["--title", f"bridge-{session_id}"])
        else:
            cmd.extend(["-s", handle])
        if self._config.model:
            cmd.extend(["-m", self._config.model])
        if self._config.variant:
            cmd.extend(["--variant", self._config.variant])
        # The prompt is deliberately NOT here — opencode reads piped stdin as
        # the message, keeping user text out of argv.
        return cmd

    def stdin_payload(
        self, prompt: str, system_prompt: str | None = None
    ) -> bytes | None:
        # Opencode has no system-prompt flag: fold it into the prompt as a
        # tagged block, on every turn (platforms resend it each call).
        if system_prompt:
            return (
                f"<system_directives>\n{system_prompt}\n</system_directives>"
                f"\n\n{prompt}"
            ).encode()
        return prompt.encode()

    def new_run_state(self) -> OpencodeRunState:
        return OpencodeRunState()

    def parse_line(self, line: str, state: OpencodeRunState) -> list[BridgeEvent]:
        events: list[BridgeEvent] = []
        for opencode_event in parse_stream_line(line):
            # The store write lives here to keep events.py pure. Every line
            # re-announces the session id — persist only when it changes, i.e.
            # once per run (checked before the fold records it on the state).
            if (
                isinstance(opencode_event, SessionAnnouncedEvent)
                and opencode_event.opencode_session_id != state.opencode_session_id
            ):
                self._handles.put(state.session_id, opencode_event.opencode_session_id)
            bridge_event = to_bridge_event(opencode_event, state)
            if bridge_event is not None:
                events.append(bridge_event)
        return events

    def on_stream_end(
        self, state: OpencodeRunState, return_code: int | None, stderr: str
    ) -> Completion | None:
        # Opencode's stream has no terminal event — every run ends here.
        return completion_at_eof(state, return_code)

    async def cleanup_session(self, session_id: str) -> None:
        # Opencode's own session storage is out of scope; only the handle
        # mapping is discarded.
        self._handles.discard(session_id)

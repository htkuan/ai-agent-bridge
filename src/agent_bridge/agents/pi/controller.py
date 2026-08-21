from __future__ import annotations

from agent_bridge.agents.base import CliAgentController
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.agents.pi.events import (
    PiRunState,
    parse_stream_line,
    to_bridge_event,
)
from agent_bridge.bridge.events import BridgeEvent


class PiController(CliAgentController[PiRunState]):
    agent_name = "Pi"

    def __init__(self, config: PiConfig) -> None:
        super().__init__(
            work_dir=config.work_dir, timeout_seconds=config.timeout_seconds
        )
        self._config = config

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        # --session-id covers both branches of is_new: pi creates the session
        # when the id is unknown and resumes it when it exists — so a resume
        # whose session file was lost degrades to a fresh session (with a
        # stderr warning) instead of failing the turn.
        cmd = [
            self._config.cli_path,
            "-p",
            "--mode",
            "json",
            "--session-id",
            session_id,
        ]

        if self._config.provider:
            cmd.extend(["--provider", self._config.provider])

        if self._config.model:
            cmd.extend(["--model", self._config.model])

        if self._config.thinking:
            cmd.extend(["--thinking", self._config.thinking])

        if self._config.tools:
            cmd.extend(["--tools", ",".join(self._config.tools)])

        if self._config.exclude_tools:
            cmd.extend(["--exclude-tools", ",".join(self._config.exclude_tools)])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # The prompt is deliberately NOT here — pi takes it as a positional
        # argument, where user text starting with "-" would parse as flags.
        return cmd

    def stdin_payload(self, prompt: str) -> bytes | None:
        # Print mode reads piped stdin as the message, keeping user text out
        # of argv entirely.
        return prompt.encode()

    def new_run_state(self) -> PiRunState:
        return PiRunState()

    def parse_line(self, line: str, state: PiRunState) -> list[BridgeEvent]:
        events: list[BridgeEvent] = []
        for pi_event in parse_stream_line(line):
            bridge_event = to_bridge_event(pi_event, state)
            if bridge_event is not None:
                events.append(bridge_event)
        return events

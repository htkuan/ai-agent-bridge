"""Scripted stand-in for the ``pi`` CLI.

Reuses ``claude_cli``'s step runner — the installed wrapper execs the same
script, so every step kind (raw/stderr/sleep/exit/…) works here too — with
builders that mimic the ``pi -p --mode json`` event stream instead. The
output contract mirrors what ``agent_bridge.agents.pi.events`` parses; keep
the builders below in sync with that parser.

Use ``install()`` (or the ``fake_pi`` fixture in ``tests/conftest.py``) to
materialise a scenario plus an executable wrapper and get a ready-to-use
``PiConfig``.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.fakes import claude_cli
from tests.fakes.claude_cli import Step, exit_code, hang, raw_line, stderr_line

if TYPE_CHECKING:
    from agent_bridge.agents.pi.config import PiConfig

__all__ = [
    "agent_end",
    "assistant_message",
    "exit_code",
    "hang",
    "install",
    "raw_line",
    "reply_steps",
    "session_header",
    "stderr_line",
    "tool_result_message",
    "tool_start",
    "turn_end",
]

# -- pi --mode json line builders (shapes pinned by tests/agents/pi) ---------


def session_header(session_id: str = "fake-pi-session") -> Step:
    return {"emit": {"type": "session", "version": 3, "id": session_id, "cwd": "."}}


def assistant_message(
    text: str | None = None,
    *,
    stop_reason: str = "stop",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: float = 0.0,
    tool_call: str | None = None,
) -> Step:
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_call is not None:
        content.append({"type": "toolCall", "id": "call_1", "name": tool_call})
    return {
        "emit": {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": content,
                "usage": {
                    "input": input_tokens,
                    "output": output_tokens,
                    "cacheRead": cache_read,
                    "cacheWrite": cache_write,
                    "cost": {"total": cost},
                },
                "stopReason": stop_reason,
            },
        }
    }


def tool_start(name: str, args: dict[str, Any] | None = None) -> Step:
    return {
        "emit": {
            "type": "tool_execution_start",
            "toolCallId": "call_1",
            "toolName": name,
            "args": args or {},
        }
    }


def tool_result_message(output: str = "ok") -> Step:
    return {
        "emit": {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "call_1",
                "content": [{"type": "text", "text": output}],
                "isError": False,
            },
        }
    }


def turn_end() -> Step:
    return {"emit": {"type": "turn_end", "message": {}, "toolResults": []}}


def agent_end(*, will_retry: bool = False) -> Step:
    return {"emit": {"type": "agent_end", "messages": [], "willRetry": will_retry}}


def reply_steps(text: str, **message_kwargs: Any) -> list[Step]:
    """The minimal happy path: header, one assistant message, terminal end."""
    return [
        session_header(),
        assistant_message(text, **message_kwargs),
        turn_end(),
        agent_end(),
    ]


# -- Installation ------------------------------------------------------------


@dataclass(frozen=True)
class FakePiCLI:
    """A materialised scenario: feed ``config`` straight to PiController."""

    config: PiConfig
    args_file: Path
    stdin_file: Path

    def invocations(self) -> list[list[str]]:
        """argv (after the scenario path) of each run, oldest first."""
        if not self.args_file.exists():
            return []
        return [
            json.loads(line) for line in self.args_file.read_text().splitlines() if line
        ]

    def stdin_payloads(self) -> list[str]:
        """What each run received on stdin, oldest first."""
        if not self.stdin_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.stdin_file.read_text().splitlines()
            if line
        ]


def install(
    directory: Path,
    steps: list[Step],
    *,
    work_dir: Path | None = None,
    timeout_seconds: float = 600.0,
) -> FakePiCLI:
    """Write the scenario + executable wrapper; return a ready PiConfig."""
    from agent_bridge.agents.pi.config import PiConfig

    directory.mkdir(parents=True, exist_ok=True)
    args_file = directory / "argv.jsonl"
    stdin_file = directory / "stdin.jsonl"
    scenario_path = directory / "scenario.json"
    scenario_path.write_text(
        json.dumps(
            {
                "steps": steps,
                "record_args": str(args_file),
                "record_stdin": str(stdin_file),
            }
        )
    )
    wrapper = directory / "pi"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(claude_cli.__file__)} "
        f'{shlex.quote(str(scenario_path))} "$@"\n'
    )
    wrapper.chmod(0o755)
    resolved_work_dir = work_dir if work_dir is not None else directory / "work"
    resolved_work_dir.mkdir(parents=True, exist_ok=True)
    config = PiConfig(
        work_dir=resolved_work_dir,
        timeout_seconds=timeout_seconds,
        cli_path=str(wrapper),
    )
    return FakePiCLI(config=config, args_file=args_file, stdin_file=stdin_file)

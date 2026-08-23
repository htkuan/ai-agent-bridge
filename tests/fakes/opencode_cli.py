"""Scripted stand-in for the ``opencode`` CLI.

Reuses ``claude_cli``'s step runner — the installed wrapper execs the same
script, so every step kind (raw/stderr/sleep/exit/…) works here too — with
builders that mimic the ``opencode run --format json`` JSONL stream instead.
The output contract mirrors what ``agent_bridge.agents.opencode.events``
parses; keep the builders below in sync with that parser.

Use ``install()`` (or the ``fake_opencode`` fixture in ``tests/conftest.py``)
to materialise a scenario plus an executable wrapper and get a ready-to-use
``OpencodeConfig``.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.fakes import claude_cli
from tests.fakes.claude_cli import Step, exit_code, hang, raw_line, stderr_line

if TYPE_CHECKING:
    from agent_bridge.agents.opencode.config import OpencodeConfig

__all__ = [
    "exit_code",
    "hang",
    "install",
    "raw_line",
    "reply_steps",
    "stderr_line",
    "step_finish",
    "step_start",
    "stream_error",
    "text_event",
    "tool_use",
]

# -- opencode run --format json line builders (pinned by tests/agents/opencode)


def step_start(*, session_id: str = "ses_fake") -> Step:
    return {
        "emit": {
            "type": "step_start",
            "sessionID": session_id,
            "part": {"type": "step-start"},
        }
    }


def text_event(text: str, *, session_id: str = "ses_fake") -> Step:
    return {
        "emit": {
            "type": "text",
            "sessionID": session_id,
            "part": {"type": "text", "text": text},
        }
    }


def tool_use(tool: str, *, session_id: str = "ses_fake") -> Step:
    return {
        "emit": {
            "type": "tool_use",
            "sessionID": session_id,
            "part": {
                "type": "tool",
                "tool": tool,
                "callID": "call_0",
                "state": {"status": "completed", "input": {}},
            },
        }
    }


def step_finish(
    *,
    session_id: str = "ses_fake",
    reason: str = "stop",
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: float = 0.0,
) -> Step:
    return {
        "emit": {
            "type": "step_finish",
            "sessionID": session_id,
            "part": {
                "type": "step-finish",
                "reason": reason,
                "tokens": {
                    "total": input_tokens + output_tokens + cache_read + cache_write,
                    "input": input_tokens,
                    "output": output_tokens,
                    "reasoning": reasoning_tokens,
                    "cache": {"write": cache_write, "read": cache_read},
                },
                "cost": cost,
            },
        }
    }


def stream_error(message: str, *, session_id: str = "ses_fake") -> Step:
    return {
        "emit": {
            "type": "error",
            "sessionID": session_id,
            "error": {"name": "UnknownError", "data": {"message": message}},
        }
    }


def reply_steps(text: str, *, session_id: str = "ses_fake") -> list[Step]:
    """The minimal happy path: step header, one message, step usage, EOF.

    Scenarios that care about usage numbers build their steps explicitly."""
    return [
        step_start(session_id=session_id),
        text_event(text, session_id=session_id),
        step_finish(session_id=session_id),
    ]


# -- Installation ------------------------------------------------------------


@dataclass(frozen=True)
class FakeOpencodeCLI:
    """A materialised scenario: feed ``config`` straight to OpencodeController."""

    config: OpencodeConfig
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
) -> FakeOpencodeCLI:
    """Write the scenario + executable wrapper; return a ready OpencodeConfig."""
    from agent_bridge.agents.opencode.config import OpencodeConfig

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
    wrapper = directory / "opencode"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(claude_cli.__file__)} "
        f'{shlex.quote(str(scenario_path))} "$@"\n'
    )
    wrapper.chmod(0o755)
    resolved_work_dir = work_dir if work_dir is not None else directory / "work"
    resolved_work_dir.mkdir(parents=True, exist_ok=True)
    config = OpencodeConfig(
        work_dir=resolved_work_dir,
        timeout_seconds=timeout_seconds,
        cli_path=str(wrapper),
    )
    return FakeOpencodeCLI(config=config, args_file=args_file, stdin_file=stdin_file)

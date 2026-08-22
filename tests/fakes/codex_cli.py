"""Scripted stand-in for the ``codex`` CLI.

Reuses ``claude_cli``'s step runner — the installed wrapper execs the same
script, so every step kind (raw/stderr/sleep/exit/…) works here too — with
builders that mimic the ``codex exec --json`` JSONL stream instead. The
output contract mirrors what ``agent_bridge.agents.codex.events`` parses;
keep the builders below in sync with that parser.

Use ``install()`` (or the ``fake_codex`` fixture in ``tests/conftest.py``)
to materialise a scenario plus an executable wrapper and get a ready-to-use
``CodexConfig``.
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
    from agent_bridge.agents.codex.config import CodexConfig

__all__ = [
    "agent_message",
    "command_execution_completed",
    "command_execution_started",
    "error_item",
    "exit_code",
    "file_change",
    "hang",
    "install",
    "raw_line",
    "reply_steps",
    "stderr_line",
    "stream_error",
    "thread_started",
    "turn_completed",
    "turn_failed",
    "turn_started",
]

# -- codex exec --json line builders (shapes pinned by tests/agents/codex) ---


def thread_started(thread_id: str = "fake-thread-id") -> Step:
    return {"emit": {"type": "thread.started", "thread_id": thread_id}}


def turn_started() -> Step:
    return {"emit": {"type": "turn.started"}}


def agent_message(text: str, *, item_id: str = "item_0") -> Step:
    return {
        "emit": {
            "type": "item.completed",
            "item": {"id": item_id, "type": "agent_message", "text": text},
        }
    }


def command_execution_started(command: str, *, item_id: str = "item_1") -> Step:
    return {
        "emit": {
            "type": "item.started",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": "",
                "exit_code": None,
                "status": "in_progress",
            },
        }
    }


def command_execution_completed(
    command: str, *, exit_code: int = 0, item_id: str = "item_1"
) -> Step:
    return {
        "emit": {
            "type": "item.completed",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": "",
                "exit_code": exit_code,
                "status": "completed",
            },
        }
    }


def file_change(paths: list[str], *, item_id: str = "item_2") -> Step:
    return {
        "emit": {
            "type": "item.started",
            "item": {
                "id": item_id,
                "type": "file_change",
                "changes": [{"path": path, "kind": "update"} for path in paths],
                "status": "in_progress",
            },
        }
    }


def error_item(message: str, *, item_id: str = "item_0") -> Step:
    return {
        "emit": {
            "type": "item.completed",
            "item": {"id": item_id, "type": "error", "message": message},
        }
    }


def stream_error(message: str) -> Step:
    return {"emit": {"type": "error", "message": message}}


def turn_completed(
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> Step:
    return {
        "emit": {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_write_input_tokens": cache_write_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
            },
        }
    }


def turn_failed(message: str) -> Step:
    return {"emit": {"type": "turn.failed", "error": {"message": message}}}


def reply_steps(
    text: str, *, thread_id: str = "fake-thread-id", **usage: int
) -> list[Step]:
    """The minimal happy path: thread header, one message, terminal usage."""
    return [
        thread_started(thread_id),
        turn_started(),
        agent_message(text),
        turn_completed(**usage),
    ]


# -- Installation ------------------------------------------------------------


@dataclass(frozen=True)
class FakeCodexCLI:
    """A materialised scenario: feed ``config`` straight to CodexController."""

    config: CodexConfig
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
) -> FakeCodexCLI:
    """Write the scenario + executable wrapper; return a ready CodexConfig."""
    from agent_bridge.agents.codex.config import CodexConfig

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
    wrapper = directory / "codex"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(claude_cli.__file__)} "
        f'{shlex.quote(str(scenario_path))} "$@"\n'
    )
    wrapper.chmod(0o755)
    resolved_work_dir = work_dir if work_dir is not None else directory / "work"
    resolved_work_dir.mkdir(parents=True, exist_ok=True)
    # skip_git_repo_check=True: the fake work dir is not a git repo, and the
    # scripted CLI doesn't care either way.
    config = CodexConfig(
        work_dir=resolved_work_dir,
        timeout_seconds=timeout_seconds,
        cli_path=str(wrapper),
        skip_git_repo_check=True,
    )
    return FakeCodexCLI(config=config, args_file=args_file, stdin_file=stdin_file)

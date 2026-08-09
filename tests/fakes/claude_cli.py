"""Scripted stand-in for the ``claude`` CLI.

Runs as a subprocess (``python claude_cli.py <scenario.json> [claude args…]``)
and replays the scenario's steps on stdout, mimicking
``claude -p --output-format stream-json``. The output contract mirrors what
``agent_bridge.agents.claude.events`` parses; keep the builders below in sync
with that parser.

Scenario schema (JSON)::

    {
      "record_args": "<path>",   # optional: append one JSON line of argv per run
      "steps": [
        {"emit": {...}},         # dump one stream-json line to stdout
        {"raw": "text"},         # write a raw (e.g. malformed) line to stdout
        {"stderr": "text"},      # write a line to stderr
        {"sleep": 30.0},         # block (hang/timeout scenarios)
        {"exit": 1}              # exit immediately with this code
      ]
    }

Steps run in order; the default exit code is 0. Two extra steps shape the
process itself: ``{"ignore_sigterm": true}`` makes the CLI survive the
controller's graceful kill (SIGKILL-fallback tests) and
``{"hold_stderr_grandchild": 8.0}`` spawns a TERM-immune child that inherits
and holds the stderr pipe open (stderr-drain-timeout tests).

Use ``install()`` (or the ``fake_claude`` fixture in ``tests/conftest.py``)
to materialise a scenario plus an executable wrapper and get a ready-to-use
``ClaudeConfig``.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_bridge.agents.claude.config import ClaudeConfig

type Step = dict[str, Any]

# -- Stream-json line builders (shapes pinned by tests/agents/claude) -------


def assistant_text(text: str, *, session_id: str = "fake-session") -> Step:
    return {
        "emit": {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            "session_id": session_id,
        }
    }


def tool_use(
    name: str,
    tool_input: dict[str, Any] | None = None,
    *,
    session_id: str = "fake-session",
) -> Step:
    return {
        "emit": {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": name, "input": tool_input or {}}
                ]
            },
            "session_id": session_id,
        }
    }


def ask_user_question(
    questions: list[dict[str, Any]], *, session_id: str = "fake-session"
) -> Step:
    return tool_use("AskUserQuestion", {"questions": questions}, session_id=session_id)


def result(
    text: str = "Done",
    *,
    is_error: bool = False,
    cost_usd: float = 0.001,
    duration_ms: int = 42,
    usage: dict[str, int] | None = None,
    num_turns: int = 1,
    duration_api_ms: int = 40,
    session_id: str = "fake-session",
) -> Step:
    line: dict[str, Any] = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": text,
        "total_cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
        "duration_api_ms": duration_api_ms,
        "session_id": session_id,
    }
    if usage is not None:
        line["usage"] = usage
    return {"emit": line}


def thinking(text: str, *, session_id: str = "fake-session") -> Step:
    return {
        "emit": {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": text}]},
            "session_id": session_id,
        }
    }


def raw_line(text: str) -> Step:
    return {"raw": text}


def stderr_line(text: str) -> Step:
    return {"stderr": text}


def hang(seconds: float = 30.0) -> Step:
    return {"sleep": seconds}


def exit_code(code: int) -> Step:
    return {"exit": code}


def ignore_sigterm() -> Step:
    return {"ignore_sigterm": True}


def hold_stderr_grandchild(seconds: float = 8.0) -> Step:
    return {"hold_stderr_grandchild": seconds}


def reply_steps(text: str, **result_kwargs: Any) -> list[Step]:
    """The minimal happy path: one text block, then the terminal result."""
    return [assistant_text(text), result(text, **result_kwargs)]


# -- Installation ------------------------------------------------------------


@dataclass(frozen=True)
class FakeClaudeCLI:
    """A materialised scenario: feed ``config`` straight to ClaudeController."""

    config: ClaudeConfig
    args_file: Path

    def invocations(self) -> list[list[str]]:
        """argv (after the scenario path) of each run, oldest first."""
        if not self.args_file.exists():
            return []
        return [
            json.loads(line) for line in self.args_file.read_text().splitlines() if line
        ]


def install(
    directory: Path,
    steps: list[Step],
    *,
    work_dir: Path | None = None,
    timeout_seconds: float = 600.0,
) -> FakeClaudeCLI:
    """Write the scenario + executable wrapper; return a ready ClaudeConfig."""
    from agent_bridge.agents.claude.config import ClaudeConfig

    directory.mkdir(parents=True, exist_ok=True)
    args_file = directory / "argv.jsonl"
    scenario_path = directory / "scenario.json"
    scenario_path.write_text(
        json.dumps({"steps": steps, "record_args": str(args_file)})
    )
    wrapper = directory / "claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(__file__)} "
        f'{shlex.quote(str(scenario_path))} "$@"\n'
    )
    wrapper.chmod(0o755)
    resolved_work_dir = work_dir if work_dir is not None else directory / "work"
    resolved_work_dir.mkdir(parents=True, exist_ok=True)
    config = ClaudeConfig(
        work_dir=resolved_work_dir,
        timeout_seconds=timeout_seconds,
        cli_path=str(wrapper),
    )
    return FakeClaudeCLI(config=config, args_file=args_file)


# -- Subprocess runtime ------------------------------------------------------


def _spawn_stderr_holding_grandchild(seconds: float, scenario_dir: Path) -> None:
    """Spawn a TERM-immune child that inherits and holds our stderr pipe.

    The ready-file handshake guarantees the TERM handler is installed before
    we move on — otherwise the controller's group-kill could win the race and
    reap the grandchild with the default handler.
    """
    import subprocess

    ready = scenario_dir / "grandchild-ready"
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).touch(); "
            f"time.sleep({seconds})",
        ]
    )
    while not ready.exists():
        time.sleep(0.01)


def _execute_step(step: Step, scenario_dir: Path) -> int | None:
    """Run one step; return an exit code to stop with, or None to continue."""
    if "emit" in step:
        sys.stdout.write(json.dumps(step["emit"]) + "\n")
        sys.stdout.flush()
    elif "raw" in step:
        sys.stdout.write(str(step["raw"]) + "\n")
        sys.stdout.flush()
    elif "stderr" in step:
        sys.stderr.write(str(step["stderr"]) + "\n")
        sys.stderr.flush()
    elif "sleep" in step:
        time.sleep(float(step["sleep"]))
    elif "exit" in step:
        return int(step["exit"])
    elif "ignore_sigterm" in step:
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif "hold_stderr_grandchild" in step:
        _spawn_stderr_holding_grandchild(
            float(step["hold_stderr_grandchild"]), scenario_dir
        )
    return None


def _run(scenario_path: str, argv: list[str]) -> int:
    scenario: dict[str, Any] = json.loads(Path(scenario_path).read_text())
    record_args = scenario.get("record_args")
    if record_args:
        with Path(record_args).open("a") as fh:
            fh.write(json.dumps(argv) + "\n")
    for step in scenario.get("steps", []):
        code = _execute_step(step, Path(scenario_path).parent)
        if code is not None:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1], sys.argv[2:]))

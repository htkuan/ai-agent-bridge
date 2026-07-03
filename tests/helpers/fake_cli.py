from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path


def _sh_single_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def install_fake_cli(
    bin_dir: Path,
    name: str = "claude",
    *,
    lines: Sequence[str] = (),
    line_delay: float = 0.0,
    exit_code: int = 0,
    args_log: Path | None = None,
    orphan_pidfile: Path | None = None,
) -> Path:
    """Write an executable fake agent-CLI shell script into ``bin_dir``.

    Prepend ``bin_dir`` to PATH (see the ``prepend_path`` fixture) so a real
    controller spawns this script instead of the real CLI.

    - ``lines``: stdout lines emitted in order (e.g. stream-json events)
    - ``line_delay``: seconds slept before each line (simulate slow streaming;
      pick delays above the controller timeout to exercise timeout paths)
    - ``exit_code``: process exit status (non-zero simulates CLI failure)
    - ``args_log``: file appended with one space-joined line of argv per
      invocation — lets tests assert flags like ``--resume <session_id>``
    - ``orphan_pidfile``: when set, the script backgrounds a ``sleep 30``
      child that inherits (and holds open) the stdout pipe, and writes its
      PID to this file — reproduces a nested CLI leaking a grandchild
    """
    script = ["#!/bin/sh"]
    if args_log is not None:
        script.append(f'printf \'%s\\n\' "$*" >> {_sh_single_quote(str(args_log))}')
    if orphan_pidfile is not None:
        script.append("sleep 30 &")
        script.append(f"echo $! > {_sh_single_quote(str(orphan_pidfile))}")
    for line in lines:
        if line_delay > 0:
            script.append(f"sleep {line_delay}")
        script.append(f"printf '%s\\n' {_sh_single_quote(line)}")
    script.append(f"exit {exit_code}")

    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("\n".join(script) + "\n")
    path.chmod(0o755)
    return path


# --- Claude stream-json line builders ---


def claude_assistant_line(text: str, session_id: str = "fake-session") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            "session_id": session_id,
        }
    )


def claude_result_line(
    text: str = "done",
    *,
    session_id: str = "fake-session",
    is_error: bool = False,
    cost_usd: float = 0.01,
    duration_ms: int = 100,
    duration_api_ms: int = 80,
    num_turns: int = 1,
    usage: dict[str, int] | None = None,
) -> str:
    payload = {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": text,
        "total_cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "duration_api_ms": duration_api_ms,
        "num_turns": num_turns,
        "session_id": session_id,
    }
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload)


# --- Codex exec --json line builders ---


def codex_thread_started_line(thread_id: str = "thread-1") -> str:
    return json.dumps({"type": "thread.started", "thread_id": thread_id})


def codex_turn_started_line() -> str:
    return json.dumps({"type": "turn.started"})


def codex_agent_message_line(text: str, item_id: str = "item_0") -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"id": item_id, "type": "agent_message", "text": text},
        }
    )


def codex_command_start_line(command: str, item_id: str = "item_1") -> str:
    return json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "status": "in_progress",
            },
        }
    )


def codex_turn_completed_line(
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
            },
        }
    )


def codex_turn_failed_line(message: str = "boom") -> str:
    return json.dumps({"type": "turn.failed", "error": {"message": message}})

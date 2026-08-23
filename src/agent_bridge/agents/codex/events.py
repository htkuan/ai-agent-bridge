from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent_bridge.agents.base import RunState
from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    StatusUpdate,
    TextDelta,
)

logger = logging.getLogger(__name__)


# --- Codex-specific event dataclasses (internal to this module) ---


@dataclass
class ThreadStartedEvent:
    """Carries the codex-minted thread id — the only resume handle."""

    thread_id: str


@dataclass
class AgentMessageEvent:
    text: str


@dataclass
class CommandStartEvent:
    command: str


@dataclass
class FileChangeStartEvent:
    paths: list[str] = field(default_factory=list[str])


@dataclass
class ErrorItemEvent:
    message: str


@dataclass
class StreamErrorEvent:
    """Top-level ``{"type": "error"}`` line — recorded, not terminal (the
    terminal decision belongs to ``turn.failed``)."""

    message: str


@dataclass
class TurnCompletedEvent:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TurnFailedEvent:
    message: str


type CodexEvent = (
    ThreadStartedEvent
    | AgentMessageEvent
    | CommandStartEvent
    | FileChangeStartEvent
    | ErrorItemEvent
    | StreamErrorEvent
    | TurnCompletedEvent
    | TurnFailedEvent
)


@dataclass
class CodexRunState(RunState):
    """Accumulates the turn across codex's JSONL events.

    Multiple ``agent_message`` items can occur per run (intermediate
    narration + final); the last one is the final answer. Usage arrives on
    the terminal ``turn.completed`` — codex reports no cost and no API
    duration.
    """

    thread_id: str = ""
    last_text: str = ""
    last_error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


def parse_stream_line(line: str) -> list[CodexEvent]:
    """Parse one line of ``codex exec --json`` output into typed events."""
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        # E.g. codex's "Reading additional input from stdin..." notice goes
        # to stderr, but keep the stdout parser tolerant anyway.
        logger.warning("Failed to parse stream line: %s", line[:200])
        return []

    event_type = data.get("type")
    if event_type == "thread.started":
        return [ThreadStartedEvent(thread_id=data.get("thread_id", ""))]
    if event_type in ("item.started", "item.completed"):
        return _parse_item(event_type, data.get("item") or {})
    if event_type == "turn.completed":
        usage: dict[str, Any] = data.get("usage") or {}
        return [
            TurnCompletedEvent(
                input_tokens=usage.get("input_tokens", 0),
                cached_input_tokens=usage.get("cached_input_tokens", 0),
                cache_write_input_tokens=usage.get("cache_write_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        ]
    if event_type == "turn.failed":
        error: dict[str, Any] = data.get("error") or {}
        return [TurnFailedEvent(message=error.get("message", ""))]
    if event_type == "error":
        return [StreamErrorEvent(message=data.get("message", ""))]
    # turn.started and unknown types are lifecycle noise.
    return []


def _parse_item(event_type: str, item: dict[str, Any]) -> list[CodexEvent]:
    item_type = item.get("type")
    # Error items surface on either lifecycle edge — record them from both.
    if item_type == "error":
        return [ErrorItemEvent(message=item.get("message", ""))]
    if event_type == "item.started":
        if item_type == "command_execution":
            return [CommandStartEvent(command=item.get("command", ""))]
        if item_type == "file_change":
            changes: list[dict[str, Any]] = item.get("changes") or []
            return [
                FileChangeStartEvent(
                    paths=[change.get("path", "") for change in changes]
                )
            ]
    elif item_type == "agent_message":
        return [AgentMessageEvent(text=item.get("text", ""))]
    # item.completed for command/file items duplicates what started told us.
    return []


def to_bridge_event(event: CodexEvent, state: CodexRunState) -> BridgeEvent | None:
    """Fold one codex event into the run state; return the BridgeEvent to emit.

    Returns None for events that only mutate state (the thread header, error
    records) — the terminal ``turn.completed`` / ``turn.failed`` build the
    ``Completion`` from the accumulated state.
    """
    match event:
        case ThreadStartedEvent(thread_id=thread_id):
            state.thread_id = thread_id
            return None
        case AgentMessageEvent(text=text):
            state.last_text = text
            return TextDelta(text=text)
        case CommandStartEvent(command=command):
            return StatusUpdate(status="Running a command...", detail=command)
        case FileChangeStartEvent(paths=paths):
            return StatusUpdate(status="Editing files...", detail=", ".join(paths))
        case ErrorItemEvent(message=message) | StreamErrorEvent(message=message):
            logger.warning("Codex reported an error: %s", message[:500])
            state.last_error = message
            return None
        case TurnCompletedEvent() as turn:
            state.num_turns += 1
            # cached_input_tokens is a subset of input_tokens; canonical
            # input_tokens excludes cache (billed separately).
            state.input_tokens += turn.input_tokens - turn.cached_input_tokens
            state.cache_read_tokens += turn.cached_input_tokens
            state.cache_creation_tokens += turn.cache_write_input_tokens
            state.output_tokens += turn.output_tokens
            state.terminal = True
            return _build_completion(state)
        case TurnFailedEvent(message=message):
            state.terminal = True
            return Completion(
                text=message or state.last_error or "Codex turn failed",
                is_error=True,
                duration_ms=_elapsed_ms(state),
            )


def _elapsed_ms(state: CodexRunState) -> int:
    return int((time.monotonic() - state.started_monotonic) * 1000)


def _build_completion(state: CodexRunState) -> Completion:
    # Token/turn detail rides in metadata using canonical keys; the bridge
    # assembles it into a typed Usage. Codex reports no cost and no per-turn
    # API duration.
    return Completion(
        text=state.last_text,
        is_error=False,
        cost_usd=0.0,
        duration_ms=_elapsed_ms(state),
        metadata={
            "usage": {
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "cache_read_tokens": state.cache_read_tokens,
                "cache_creation_tokens": state.cache_creation_tokens,
                "num_turns": state.num_turns,
                "duration_api_ms": 0,
            }
        },
    )

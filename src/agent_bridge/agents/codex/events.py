from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from agent_bridge.agents.base import RunState
from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    StatusUpdate,
    TextDelta,
)

logger = logging.getLogger(__name__)


@dataclass
class ThreadStartedEvent:
    thread_id: str


@dataclass
class AgentMessageEvent:
    text: str


@dataclass
class CommandExecutionStartedEvent:
    command: str


@dataclass
class FileChangeStartedEvent:
    paths: list[str] = field(default_factory=list[str])


@dataclass
class ErrorItemEvent:
    message: str


@dataclass
class TopLevelErrorEvent:
    message: str


@dataclass
class TurnCompletedEvent:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass
class TurnFailedEvent:
    message: str


type CodexEvent = (
    ThreadStartedEvent
    | AgentMessageEvent
    | CommandExecutionStartedEvent
    | FileChangeStartedEvent
    | ErrorItemEvent
    | TopLevelErrorEvent
    | TurnCompletedEvent
    | TurnFailedEvent
)


@dataclass
class CodexRunState(RunState):
    thread_id: str = ""
    last_text: str = ""
    error_message: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    num_turns: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


def parse_stream_line(line: str) -> list[CodexEvent]:
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse stream line: %s", line[:200])
        return []

    event_type = data.get("type")
    if event_type == "thread.started":
        return [ThreadStartedEvent(thread_id=str(data.get("thread_id", "")))]
    if event_type == "item.started":
        return _parse_item_started(data)
    if event_type == "item.completed":
        return _parse_item_completed(data)
    if event_type == "turn.completed":
        return [_parse_turn_completed(data)]
    if event_type == "turn.failed":
        return [TurnFailedEvent(message=_error_message(data.get("error")))]
    if event_type == "error":
        return [TopLevelErrorEvent(message=str(data.get("message", "")))]
    return []


def _parse_item_started(data: dict[str, Any]) -> list[CodexEvent]:
    item = _dict(data.get("item"))
    item_type = item.get("type")
    if item_type == "command_execution":
        return [CommandExecutionStartedEvent(command=str(item.get("command", "")))]
    if item_type == "file_change":
        return [FileChangeStartedEvent(paths=_file_change_paths(item))]
    if item_type == "error":
        return [ErrorItemEvent(message=str(item.get("message", "")))]
    return []


def _parse_item_completed(data: dict[str, Any]) -> list[CodexEvent]:
    item = _dict(data.get("item"))
    item_type = item.get("type")
    if item_type == "agent_message":
        return [AgentMessageEvent(text=str(item.get("text", "")))]
    if item_type == "error":
        return [ErrorItemEvent(message=str(item.get("message", "")))]
    return []


def _parse_turn_completed(data: dict[str, Any]) -> TurnCompletedEvent:
    usage = _dict(data.get("usage"))
    return TurnCompletedEvent(
        input_tokens=_int_field(usage, "input_tokens"),
        cached_input_tokens=_int_field(usage, "cached_input_tokens"),
        cache_write_input_tokens=_int_field(usage, "cache_write_input_tokens"),
        output_tokens=_int_field(usage, "output_tokens"),
        reasoning_output_tokens=_int_field(usage, "reasoning_output_tokens"),
    )


def _file_change_paths(item: dict[str, Any]) -> list[str]:
    raw_changes = item.get("changes")
    if not isinstance(raw_changes, list):
        return []
    paths: list[str] = []
    for raw_change in cast("list[object]", raw_changes):
        change = _dict(raw_change)
        path = change.get("path")
        if isinstance(path, str) and path:
            paths.append(path)
    return paths


def _error_message(raw: object) -> str:
    if isinstance(raw, dict):
        data = cast("Mapping[str, object]", raw)
        return str(data.get("message", ""))
    if isinstance(raw, str):
        return raw
    return ""


def _dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    return {}


def _int_field(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def to_bridge_event(event: CodexEvent, state: CodexRunState) -> BridgeEvent | None:
    match event:
        case ThreadStartedEvent(thread_id=thread_id):
            state.thread_id = thread_id
            return None
        case AgentMessageEvent(text=text):
            state.last_text = text
            return TextDelta(text=text)
        case CommandExecutionStartedEvent(command=command):
            return StatusUpdate(status="Running a command...", detail=command)
        case FileChangeStartedEvent(paths=paths):
            return StatusUpdate(status="Editing files...", detail=", ".join(paths))
        case ErrorItemEvent(message=message) | TopLevelErrorEvent(message=message):
            if message:
                state.error_message = message
                logger.warning("Codex error event: %s", message[:500])
            return None
        case TurnCompletedEvent() as completed:
            state.terminal = True
            _record_success_usage(completed, state)
            return _build_success_completion(state)
        case TurnFailedEvent(message=message):
            state.terminal = True
            if message:
                state.error_message = message
            return _build_error_completion(state)


def _record_success_usage(event: TurnCompletedEvent, state: CodexRunState) -> None:
    state.input_tokens = event.input_tokens - event.cached_input_tokens
    state.cache_read_tokens = event.cached_input_tokens
    state.cache_creation_tokens = event.cache_write_input_tokens
    state.output_tokens = event.output_tokens
    state.reasoning_output_tokens = event.reasoning_output_tokens
    state.num_turns += 1


def _build_success_completion(state: CodexRunState) -> Completion:
    return Completion(
        text=state.last_text,
        is_error=False,
        cost_usd=0.0,
        duration_ms=int((time.monotonic() - state.started_monotonic) * 1000),
        metadata={
            "usage": {
                "input_tokens": state.input_tokens,
                "cache_read_tokens": state.cache_read_tokens,
                "cache_creation_tokens": state.cache_creation_tokens,
                "output_tokens": state.output_tokens,
                "cost_usd": 0.0,
                "duration_api_ms": 0,
                "num_turns": state.num_turns,
            }
        },
    )


def _build_error_completion(state: CodexRunState) -> Completion:
    return Completion(
        text=state.error_message or "Codex turn failed",
        is_error=True,
        duration_ms=int((time.monotonic() - state.started_monotonic) * 1000),
    )

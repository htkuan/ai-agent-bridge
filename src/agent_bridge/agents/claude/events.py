from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent_bridge.events import (
    BridgeEvent,
    Completion,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)

logger = logging.getLogger(__name__)


# --- Claude-specific event dataclasses (internal to this module) ---


@dataclass
class InitEvent:
    session_id: str
    model: str = ""
    tools: list[str] = field(default_factory=list[str])


@dataclass
class AssistantTextEvent:
    session_id: str
    text: str = ""


@dataclass
class ThinkingEvent:
    session_id: str
    thinking: str = ""


@dataclass
class ToolUseEvent:
    session_id: str
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class ToolResultEvent:
    session_id: str
    output: str = ""
    is_error: bool = False


@dataclass
class ResultEvent:
    session_id: str
    result_text: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    is_error: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    duration_api_ms: int = 0


type ClaudeEvent = (
    InitEvent
    | AssistantTextEvent
    | ThinkingEvent
    | ToolUseEvent
    | ToolResultEvent
    | ResultEvent
)


# Complexity hotspot (13 > 10); refactor tracked separately.
def parse_stream_line(line: str) -> list[ClaudeEvent]:  # noqa: C901
    """Parse a single line of Claude CLI stream-json output into typed events.

    Returns a list because one JSON line may contain multiple content blocks
    (e.g., text + thinking in the same assistant message).
    """
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse stream line: %s", line[:200])
        return []

    event_type = data.get("type")
    session_id = data.get("session_id", "")

    if event_type == "system" and data.get("subtype") == "init":
        return [
            InitEvent(
                session_id=session_id,
                model=data.get("model", ""),
                tools=data.get("tools", []),
            )
        ]

    if event_type == "assistant":
        events: list[ClaudeEvent] = []
        message = data.get("message", {})
        contents = message.get("content", [])
        for content in contents:
            content_type = content.get("type")
            if content_type == "text":
                events.append(
                    AssistantTextEvent(
                        session_id=session_id,
                        text=content.get("text", ""),
                    )
                )
            elif content_type == "thinking":
                events.append(
                    ThinkingEvent(
                        session_id=session_id,
                        thinking=content.get("thinking", ""),
                    )
                )
            elif content_type == "tool_use":
                events.append(
                    ToolUseEvent(
                        session_id=session_id,
                        tool_name=content.get("name", ""),
                        tool_input=content.get("input", {}),
                    )
                )
        return events

    if event_type == "user":
        events = []
        message = data.get("message", {})
        contents = message.get("content", [])
        for content in contents:
            if content.get("type") == "tool_result":
                events.append(
                    ToolResultEvent(
                        session_id=session_id,
                        output=content.get("content", ""),
                        is_error=content.get("is_error", False),
                    )
                )
        return events

    if event_type == "result":
        # `usage` carries Anthropic-style token counts (input/output exclude
        # cache, which is reported separately). Map to canonical bridge keys.
        usage: dict[str, Any] = data.get("usage") or {}
        return [
            ResultEvent(
                session_id=session_id,
                result_text=data.get("result", ""),
                cost_usd=data.get("total_cost_usd", 0.0),
                duration_ms=data.get("duration_ms", 0),
                is_error=data.get("is_error", False),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                num_turns=data.get("num_turns", 0),
                duration_api_ms=data.get("duration_api_ms", 0),
            )
        ]

    return []


def to_bridge_event(event: ClaudeEvent) -> BridgeEvent | None:
    """Convert a Claude-specific event to a generic BridgeEvent.

    Returns None for events that are internal to the agent (InitEvent,
    ThinkingEvent, ToolResultEvent) and should not be exposed to the platform.
    """
    match event:
        case AssistantTextEvent(text=text):
            return TextDelta(text=text)
        case ToolUseEvent(tool_name="AskUserQuestion", tool_input=inp):
            questions = inp.get("questions", [])
            if not questions:
                return StatusUpdate(status="Using AskUserQuestion...")
            return UserQuestion(questions=questions)
        case ToolUseEvent(tool_name=name):
            return StatusUpdate(status=f"Using {name}...")
        case ResultEvent() as result:
            # Token/turn detail rides in metadata using canonical keys; the
            # bridge assembles it into a typed Usage. cost/duration stay as
            # first-class Completion fields.
            return Completion(
                text=result.result_text,
                is_error=result.is_error,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                metadata={
                    "usage": {
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cache_read_tokens": result.cache_read_tokens,
                        "cache_creation_tokens": result.cache_creation_tokens,
                        "num_turns": result.num_turns,
                        "duration_api_ms": result.duration_api_ms,
                    }
                },
            )
        case _:
            return None

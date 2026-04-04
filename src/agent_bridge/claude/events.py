from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# --- Event dataclasses ---


@dataclass
class InitEvent:
    session_id: str
    model: str = ""
    tools: list[str] = field(default_factory=list)


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
    tool_input: dict = field(default_factory=dict)


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


type Event = (
    InitEvent
    | AssistantTextEvent
    | ThinkingEvent
    | ToolUseEvent
    | ToolResultEvent
    | ResultEvent
)


def parse_stream_line(line: str) -> list[Event]:
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
        events: list[Event] = []
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
        return [
            ResultEvent(
                session_id=session_id,
                result_text=data.get("result", ""),
                cost_usd=data.get("total_cost_usd", 0.0),
                duration_ms=data.get("duration_ms", 0),
                is_error=data.get("is_error", False),
            )
        ]

    return []

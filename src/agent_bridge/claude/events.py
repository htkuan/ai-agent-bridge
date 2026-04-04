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


def parse_stream_line(line: str) -> Event | None:
    """Parse a single line of Claude CLI stream-json output into a typed event."""
    line = line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse stream line: %s", line[:200])
        return None

    event_type = data.get("type")
    session_id = data.get("session_id", "")

    if event_type == "system" and data.get("subtype") == "init":
        return InitEvent(
            session_id=session_id,
            model=data.get("model", ""),
            tools=data.get("tools", []),
        )

    if event_type == "assistant":
        message = data.get("message", {})
        contents = message.get("content", [])
        for content in contents:
            content_type = content.get("type")
            if content_type == "text":
                return AssistantTextEvent(
                    session_id=session_id,
                    text=content.get("text", ""),
                )
            if content_type == "thinking":
                return ThinkingEvent(
                    session_id=session_id,
                    thinking=content.get("thinking", ""),
                )
            if content_type == "tool_use":
                return ToolUseEvent(
                    session_id=session_id,
                    tool_name=content.get("name", ""),
                    tool_input=content.get("input", {}),
                )
        return None

    if event_type == "user":
        message = data.get("message", {})
        contents = message.get("content", [])
        for content in contents:
            if content.get("type") == "tool_result":
                return ToolResultEvent(
                    session_id=session_id,
                    output=content.get("content", ""),
                    is_error=content.get("is_error", False),
                )
        return None

    if event_type == "result":
        return ResultEvent(
            session_id=session_id,
            result_text=data.get("result", ""),
            cost_usd=data.get("total_cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0),
            is_error=data.get("is_error", False),
        )

    return None

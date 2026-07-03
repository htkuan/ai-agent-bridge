from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from agent_bridge.events import BridgeEvent, Completion, StatusUpdate, TextDelta

logger = logging.getLogger(__name__)

_DETAIL_MAX_CHARS = 200


# --- Codex-specific event dataclasses (internal to this module) ---
#
# Modeled on the `codex exec --json` JSONL schema (verified against the
# official TypeScript SDK types, npm @openai/codex 0.130.0):
#   thread.started / turn.started / item.started|updated|completed /
#   turn.completed / turn.failed / error


@dataclass
class ThreadStartedEvent:
    thread_id: str


@dataclass
class TurnStartedEvent:
    pass


@dataclass
class AgentMessageEvent:
    text: str = ""


@dataclass
class CommandExecutionEvent:
    command: str = ""


@dataclass
class McpToolCallEvent:
    server: str = ""
    tool: str = ""


@dataclass
class WebSearchEvent:
    query: str = ""


@dataclass
class FileChangeEvent:
    changes: list = field(default_factory=list)


@dataclass
class TurnCompletedEvent:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TurnFailedEvent:
    message: str = ""


@dataclass
class ErrorEvent:
    message: str = ""


type CodexEvent = (
    ThreadStartedEvent
    | TurnStartedEvent
    | AgentMessageEvent
    | CommandExecutionEvent
    | McpToolCallEvent
    | WebSearchEvent
    | FileChangeEvent
    | TurnCompletedEvent
    | TurnFailedEvent
    | ErrorEvent
)

# `codex exec` runs exactly one turn, so any of these ends the stream. A bare
# `error` event is a fatal stream error (the SDK aborts on it too).
TERMINAL_EVENTS = (TurnCompletedEvent, TurnFailedEvent, ErrorEvent)


def parse_stream_line(line: str) -> list[CodexEvent]:
    """Parse one line of `codex exec --json` output into typed events.

    Unknown event/item types are logged and skipped — the Codex CLI evolves
    quickly and new event types must not break the bridge.
    """
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse codex stream line: %s", line[:200])
        return []
    if not isinstance(data, dict):
        logger.warning("Ignoring non-object codex stream line: %s", line[:200])
        return []

    event_type = data.get("type")
    match event_type:
        case "thread.started":
            return [ThreadStartedEvent(thread_id=data.get("thread_id", ""))]
        case "turn.started":
            return [TurnStartedEvent()]
        case "turn.completed":
            usage = data.get("usage") or {}
            return [
                TurnCompletedEvent(
                    input_tokens=usage.get("input_tokens", 0),
                    cached_input_tokens=usage.get("cached_input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                )
            ]
        case "turn.failed":
            error = data.get("error") or {}
            return [TurnFailedEvent(message=error.get("message", ""))]
        case "error":
            return [ErrorEvent(message=data.get("message", ""))]
        case "item.started" | "item.updated" | "item.completed":
            return _parse_item(event_type, data.get("item") or {})
        case _:
            logger.info("Skipping unknown codex event type: %r", event_type)
            return []


def _parse_item(event_type: str, item: dict) -> list[CodexEvent]:
    item_type = item.get("type")

    # Agent message text is only complete on item.completed; activity items
    # surface once, when they start. Everything else is internal.
    if event_type == "item.completed":
        if item_type == "agent_message":
            return [AgentMessageEvent(text=item.get("text", ""))]
        return []
    if event_type != "item.started":
        return []

    match item_type:
        case "command_execution":
            return [CommandExecutionEvent(command=item.get("command", ""))]
        case "mcp_tool_call":
            return [
                McpToolCallEvent(
                    server=item.get("server", ""), tool=item.get("tool", "")
                )
            ]
        case "web_search":
            return [WebSearchEvent(query=item.get("query", ""))]
        case "file_change":
            return [FileChangeEvent(changes=item.get("changes", []))]
        case "agent_message" | "reasoning" | "todo_list" | "error":
            return []
        case _:
            logger.info("Skipping unknown codex item type: %r", item_type)
            return []


def to_bridge_event(event: CodexEvent) -> BridgeEvent | None:
    """Convert a Codex-specific event to a generic BridgeEvent.

    Returns None for agent-internal events (thread/turn start, reasoning,
    todo lists) that must not reach the platform. The Completion built from
    ``turn.completed`` has an empty ``text`` — the controller fills it with
    the final agent message (the CLI's terminal event carries only usage).
    """
    match event:
        case AgentMessageEvent(text=text):
            return TextDelta(text=text)
        case CommandExecutionEvent(command=command):
            return StatusUpdate(status="Running command...", detail=_truncate(command))
        case McpToolCallEvent(server=server, tool=tool):
            return StatusUpdate(status=f"Using {server}.{tool}...")
        case WebSearchEvent(query=query):
            return StatusUpdate(status="Searching the web...", detail=_truncate(query))
        case FileChangeEvent(changes=changes):
            paths = ", ".join(
                c.get("path", "") for c in changes if isinstance(c, dict)
            )
            return StatusUpdate(status="Applying file changes...", detail=_truncate(paths))
        case TurnCompletedEvent() as turn:
            # Codex reports OpenAI-style usage: input_tokens *includes* the
            # cached prefix (cached_input_tokens is a subset). The canonical
            # bridge keys expect input/output to exclude cache, so split it
            # out. Codex has no cache-creation concept and reports no cost.
            return Completion(
                text="",
                is_error=False,
                metadata={
                    "usage": {
                        "input_tokens": max(
                            turn.input_tokens - turn.cached_input_tokens, 0
                        ),
                        "output_tokens": turn.output_tokens,
                        "cache_read_tokens": turn.cached_input_tokens,
                        "cache_creation_tokens": 0,
                        "num_turns": 1,
                    }
                },
            )
        case TurnFailedEvent(message=message):
            return Completion(text=message or "Codex turn failed", is_error=True)
        case ErrorEvent(message=message):
            return Completion(text=message or "Codex stream error", is_error=True)
        case _:
            return None


def _truncate(text: str) -> str:
    if len(text) <= _DETAIL_MAX_CHARS:
        return text
    return text[: _DETAIL_MAX_CHARS - 1] + "…"

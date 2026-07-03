from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from agent_bridge.events import BridgeEvent, Completion, StatusUpdate, TextDelta

logger = logging.getLogger(__name__)

_DETAIL_MAX_CHARS = 200


# --- OpenCode-specific event dataclasses (internal to this module) ---
#
# Modeled on the `opencode run --format json` JSONL schema (verified against
# the run command source, opencode-ai 1.17.x): every line is
# `{type, timestamp, sessionID, ...}` with type one of step_start /
# step_finish / text / tool_use / reasoning / error. Each parsed event keeps
# the top-level sessionID so the controller can capture the CLI-minted
# session id for the session map.


@dataclass
class StepStartedEvent:
    session_id: str = ""


@dataclass
class StepFinishedEvent:
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    session_id: str = ""


@dataclass
class TextPartEvent:
    text: str = ""
    session_id: str = ""


@dataclass
class ToolUseEvent:
    tool: str = ""
    title: str = ""
    failed: bool = False
    session_id: str = ""


@dataclass
class SessionErrorEvent:
    message: str = ""
    session_id: str = ""


type OpencodeEvent = (
    StepStartedEvent
    | StepFinishedEvent
    | TextPartEvent
    | ToolUseEvent
    | SessionErrorEvent
)

# `opencode run` has no success terminal event — the CLI simply exits (EOF)
# once the session goes idle. A session error aborts the run (the CLI sets
# exit code 1), so it is the only event worth short-circuiting on.
TERMINAL_EVENTS = (SessionErrorEvent,)


def parse_stream_line(line: str) -> list[OpencodeEvent]:
    """Parse one line of `opencode run --format json` output into typed events.

    Unknown event types are logged and skipped — the OpenCode CLI evolves
    quickly and new event types must not break the bridge.
    """
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse opencode stream line: %s", line[:200])
        return []
    if not isinstance(data, dict):
        logger.warning("Ignoring non-object opencode stream line: %s", line[:200])
        return []

    session_id = str(data.get("sessionID") or "")
    part = data.get("part") or {}

    match data.get("type"):
        case "step_start":
            return [StepStartedEvent(session_id=session_id)]
        case "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            return [
                StepFinishedEvent(
                    cost=float(part.get("cost") or 0.0),
                    input_tokens=int(tokens.get("input") or 0),
                    output_tokens=int(tokens.get("output") or 0),
                    reasoning_tokens=int(tokens.get("reasoning") or 0),
                    cache_read_tokens=int(cache.get("read") or 0),
                    cache_write_tokens=int(cache.get("write") or 0),
                    session_id=session_id,
                )
            ]
        case "text":
            text = part.get("text") or ""
            if not text.strip():
                return []
            return [TextPartEvent(text=text, session_id=session_id)]
        case "tool_use":
            state = part.get("state") or {}
            return [
                ToolUseEvent(
                    tool=part.get("tool") or "",
                    title=state.get("title") or "",
                    failed=state.get("status") == "error",
                    session_id=session_id,
                )
            ]
        case "error":
            error = data.get("error") or {}
            error_data = error.get("data") or {}
            message = str(
                error_data.get("message")
                or error.get("message")
                or error.get("name")
                or ""
            )
            return [SessionErrorEvent(message=message, session_id=session_id)]
        case "reasoning":
            return []  # internal (only emitted with --thinking anyway)
        case event_type:
            logger.info("Skipping unknown opencode event type: %r", event_type)
            return []


def to_bridge_event(event: OpencodeEvent) -> BridgeEvent | None:
    """Convert an OpenCode-specific event to a generic BridgeEvent.

    Returns None for step boundaries — the controller aggregates them into
    the Completion it synthesizes at stream EOF (usage/cost come from the
    step_finish events; there is no CLI-side terminal event to map).
    ``tool_use`` is retrospective: the CLI only emits it once a tool call
    has completed or errored.
    """
    match event:
        case TextPartEvent(text=text):
            return TextDelta(text=text)
        case ToolUseEvent(tool=tool, title=title, failed=failed):
            status = f"Tool {tool} failed" if failed else f"Ran {tool}"
            return StatusUpdate(status=status, detail=_truncate(title))
        case SessionErrorEvent(message=message):
            return Completion(text=message or "OpenCode error", is_error=True)
        case _:
            return None


def _truncate(text: str) -> str:
    if len(text) <= _DETAIL_MAX_CHARS:
        return text
    return text[: _DETAIL_MAX_CHARS - 1] + "…"

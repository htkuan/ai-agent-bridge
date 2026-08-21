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


# --- Pi-specific event dataclasses (internal to this module) ---


@dataclass
class SessionEvent:
    session_id: str


@dataclass
class ToolStartEvent:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class AssistantMessageEvent:
    texts: list[str] = field(default_factory=list[str])
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class TurnEndEvent:
    pass


@dataclass
class AgentEndEvent:
    will_retry: bool = False


type PiEvent = (
    SessionEvent | ToolStartEvent | AssistantMessageEvent | TurnEndEvent | AgentEndEvent
)


@dataclass
class PiRunState(RunState):
    """Accumulates the turn across pi's per-message events.

    Pi's stream has no terminal result payload — ``pi -p --mode json`` ends
    with a bare ``agent_end`` — so the final text is the last assistant text
    seen, and usage/cost sum over every assistant message.
    """

    last_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


def parse_stream_line(line: str) -> list[PiEvent]:
    """Parse one line of ``pi -p --mode json`` output into typed events."""
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse stream line: %s", line[:200])
        return []

    event_type = data.get("type")
    if event_type == "session":
        return [SessionEvent(session_id=data.get("id", ""))]
    if event_type == "tool_execution_start":
        return [
            ToolStartEvent(
                tool_name=data.get("toolName", ""),
                args=data.get("args") or {},
            )
        ]
    if event_type == "message_end":
        return _parse_message_end(data)
    if event_type == "turn_end":
        return [TurnEndEvent()]
    if event_type == "agent_end":
        return [AgentEndEvent(will_retry=bool(data.get("willRetry", False)))]
    # message_start/update carry deltas we don't need (message_end is the
    # authoritative snapshot); tool_execution_end, agent_start, turn_start
    # and agent_settled are lifecycle noise.
    return []


def _parse_message_end(data: dict[str, Any]) -> list[PiEvent]:
    message: dict[str, Any] = data.get("message") or {}
    if message.get("role") != "assistant":
        # The prompt echo (user) and toolResult messages are internal.
        return []
    texts = [
        content.get("text", "")
        for content in message.get("content", [])
        if content.get("type") == "text"
    ]
    usage: dict[str, Any] = message.get("usage") or {}
    cost: dict[str, Any] = usage.get("cost") or {}
    return [
        AssistantMessageEvent(
            texts=[text for text in texts if text],
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            cache_read_tokens=usage.get("cacheRead", 0),
            cache_write_tokens=usage.get("cacheWrite", 0),
            cost_usd=cost.get("total", 0.0),
        )
    ]


def to_bridge_event(event: PiEvent, state: PiRunState) -> BridgeEvent | None:
    """Fold one pi event into the run state; return the BridgeEvent to emit.

    Returns None for events that are internal to the agent (session header,
    turn boundaries, text-less assistant messages) or that only mutate state.
    """
    match event:
        case AssistantMessageEvent() as message:
            state.input_tokens += message.input_tokens
            state.output_tokens += message.output_tokens
            state.cache_read_tokens += message.cache_read_tokens
            state.cache_creation_tokens += message.cache_write_tokens
            state.cost_usd += message.cost_usd
            if not message.texts:
                return None  # tool-call-only message
            text = "\n".join(message.texts)
            state.last_text = text
            return TextDelta(text=text)
        case ToolStartEvent(tool_name=name):
            return StatusUpdate(status=f"Using {name}...")
        case TurnEndEvent():
            state.num_turns += 1
            return None
        case AgentEndEvent(will_retry=True):
            return None  # pi is about to retry the turn; keep reading
        case AgentEndEvent():
            state.terminal = True
            return _build_completion(state)
        case _:  # SessionEvent
            return None


def _build_completion(state: PiRunState) -> Completion:
    # Token/turn detail rides in metadata using canonical keys; the bridge
    # assembles it into a typed Usage. Pi reports no per-turn API duration.
    return Completion(
        text=state.last_text,
        is_error=False,
        cost_usd=state.cost_usd,
        duration_ms=int((time.monotonic() - state.started_monotonic) * 1000),
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

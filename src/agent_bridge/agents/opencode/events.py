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


# --- Opencode-specific event dataclasses (internal to this module) ---


@dataclass
class SessionAnnouncedEvent:
    """Carries the opencode-minted session id (``ses_…``) — the only resume
    handle. Every stream line re-announces it; the fold records it once."""

    opencode_session_id: str


@dataclass
class TextEvent:
    text: str


@dataclass
class ToolUseEvent:
    tool: str


@dataclass
class StepFinishEvent:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class StreamErrorEvent:
    """Top-level ``{"type": "error"}`` line — recorded, not terminal (the
    stream has no terminal event; the exit code makes the call at EOF)."""

    message: str


type OpencodeEvent = (
    SessionAnnouncedEvent
    | TextEvent
    | ToolUseEvent
    | StepFinishEvent
    | StreamErrorEvent
)


@dataclass
class OpencodeRunState(RunState):
    """Accumulates the turn across opencode's JSONL events.

    Multiple ``text`` events can occur per run (intermediate narration +
    final); the last one is the final answer. Usage/cost arrive per step on
    ``step_finish`` and are summed. The stream has **no terminal event** —
    ``terminal`` is never set, and the ``Completion`` is synthesized at EOF
    from this state (see ``completion_at_eof``).
    """

    opencode_session_id: str = ""
    last_text: str = ""
    error_message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


def parse_stream_line(line: str) -> list[OpencodeEvent]:
    """Parse one line of ``opencode run --format json`` output into typed
    events. Every line carries a top-level ``sessionID``; it is surfaced as
    its own event so the fold can record the resume handle."""
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse stream line: %s", line[:200])
        return []

    events: list[OpencodeEvent] = []
    session_id = data.get("sessionID", "")
    if session_id:
        events.append(SessionAnnouncedEvent(opencode_session_id=session_id))

    part: dict[str, Any] = data.get("part") or {}
    match data.get("type"):
        case "text":
            events.append(TextEvent(text=part.get("text", "")))
        case "tool_use":
            events.append(ToolUseEvent(tool=part.get("tool", "")))
        case "step_finish":
            tokens: dict[str, Any] = part.get("tokens") or {}
            cache: dict[str, Any] = tokens.get("cache") or {}
            events.append(
                StepFinishEvent(
                    input_tokens=tokens.get("input", 0),
                    output_tokens=tokens.get("output", 0),
                    cache_read_tokens=cache.get("read", 0),
                    cache_write_tokens=cache.get("write", 0),
                    cost_usd=part.get("cost", 0.0),
                )
            )
        case "error":
            error: dict[str, Any] = data.get("error") or {}
            error_data: dict[str, Any] = error.get("data") or {}
            events.append(
                StreamErrorEvent(
                    message=error_data.get("message", "") or error.get("name", "")
                )
            )
        case _:
            # step_start and unknown types are lifecycle noise.
            pass
    return events


def to_bridge_event(
    event: OpencodeEvent, state: OpencodeRunState
) -> BridgeEvent | None:
    """Fold one opencode event into the run state; return the BridgeEvent to
    emit. Returns None for events that only mutate state (the session header,
    step usage, error records) — the ``Completion`` is synthesized at EOF."""
    match event:
        case SessionAnnouncedEvent(opencode_session_id=session_id):
            state.opencode_session_id = session_id
            return None
        case TextEvent(text=text):
            state.last_text = text
            return TextDelta(text=text)
        case ToolUseEvent(tool=tool):
            return StatusUpdate(status=f"Using {tool}...")
        case StepFinishEvent() as step:
            # tokens.input already excludes cache reads/writes.
            state.num_turns += 1
            state.input_tokens += step.input_tokens
            state.output_tokens += step.output_tokens
            state.cache_read_tokens += step.cache_read_tokens
            state.cache_creation_tokens += step.cache_write_tokens
            state.cost_usd += step.cost_usd
            return None
        case StreamErrorEvent(message=message):
            logger.warning("Opencode reported an error: %s", message[:500])
            state.error_message = message
            return None


def completion_at_eof(
    state: OpencodeRunState, return_code: int | None
) -> Completion | None:
    """Synthesize the final ``Completion`` when the stream hits EOF.

    Opencode's stream has no terminal event, so the exit code decides:
    0 with a reply ⇒ success built from the accumulated state; a recorded
    ``error`` event with no reply ⇒ that error (whatever the exit code — the
    CLI has been seen exiting 0 mid-run); 0 with nothing at all ⇒ an error
    too, since a real turn always ends in a ``text`` event; non-zero with
    nothing recorded ⇒ None, letting the engine's generic exit-code error
    cover it (e.g. ``Error: Session not found`` prints to stderr with no
    JSON at all).
    """
    if return_code == 0 and state.last_text:
        return Completion(
            text=state.last_text,
            is_error=False,
            cost_usd=state.cost_usd,
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
    if state.error_message:
        return Completion(
            text=state.error_message,
            is_error=True,
            duration_ms=_elapsed_ms(state),
        )
    if return_code == 0:
        return Completion(
            text="Opencode stream ended without a reply",
            is_error=True,
            duration_ms=_elapsed_ms(state),
        )
    return None


def _elapsed_ms(state: OpencodeRunState) -> int:
    return int((time.monotonic() - state.started_monotonic) * 1000)

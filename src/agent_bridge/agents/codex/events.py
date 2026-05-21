from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from agent_bridge.events import BridgeEvent, Completion, StatusUpdate, TextDelta

logger = logging.getLogger(__name__)


# --- Codex-specific event dataclasses (internal to this module) ---


@dataclass
class ThreadStartedEvent:
    thread_id: str


@dataclass
class TurnStartedEvent:
    pass


@dataclass
class TurnCompletedEvent:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass
class TurnFailedEvent:
    message: str = ""


@dataclass
class AgentMessageItem:
    item_id: str
    text: str
    completed: bool


@dataclass
class CommandExecutionItem:
    item_id: str
    command: str
    status: str
    exit_code: int | None
    completed: bool


@dataclass
class McpToolCallItem:
    item_id: str
    server: str
    tool: str
    status: str
    completed: bool


@dataclass
class WebSearchItem:
    item_id: str
    completed: bool


@dataclass
class TodoListItem:
    item_id: str
    completed: bool


@dataclass
class FileChangeItem:
    item_id: str
    completed: bool


@dataclass
class ReasoningItem:
    item_id: str
    completed: bool


@dataclass
class UnknownItem:
    item_id: str
    item_type: str
    completed: bool
    raw: dict = field(default_factory=dict)


@dataclass
class ErrorEvent:
    message: str = ""


type CodexEvent = (
    ThreadStartedEvent
    | TurnStartedEvent
    | TurnCompletedEvent
    | TurnFailedEvent
    | AgentMessageItem
    | CommandExecutionItem
    | McpToolCallItem
    | WebSearchItem
    | TodoListItem
    | FileChangeItem
    | ReasoningItem
    | UnknownItem
    | ErrorEvent
)


# --- Pure parser: NDJSON line → typed CodexEvent(s) ---


def parse_stream_line(line: str) -> list[CodexEvent]:
    """Parse a single NDJSON line from `codex exec --json`.

    Pure function: malformed input becomes an empty list (warning logged),
    unknown event types are skipped silently.
    """
    line = line.strip()
    if not line:
        return []

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Failed to parse codex stream line: %s", line[:200])
        return []

    event_type = data.get("type", "")

    if event_type == "thread.started":
        return [ThreadStartedEvent(thread_id=str(data.get("thread_id", "")))]

    if event_type == "turn.started":
        return [TurnStartedEvent()]

    if event_type == "turn.completed":
        usage = data.get("usage") or {}
        return [
            TurnCompletedEvent(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                reasoning_output_tokens=int(usage.get("reasoning_output_tokens", 0) or 0),
            )
        ]

    if event_type == "turn.failed":
        return [TurnFailedEvent(message=_extract_error_message(data) or "turn failed")]

    if event_type == "error":
        return [ErrorEvent(message=_extract_error_message(data) or "codex error")]

    if event_type in {"item.started", "item.updated", "item.completed"}:
        completed = event_type == "item.completed"
        item = data.get("item") or {}
        return [_parse_item(item, completed)]

    return []


def _extract_error_message(data: dict) -> str:
    """Try several known shapes for the error payload."""
    for key in ("error", "message"):
        value = data.get(key)
        if isinstance(value, dict):
            msg = value.get("message")
            if isinstance(msg, str) and msg:
                return msg
            return json.dumps(value)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_item(item: dict, completed: bool) -> CodexEvent:
    item_id = str(item.get("id", ""))
    item_type = str(item.get("type", "") or item.get("item_type", ""))

    if item_type == "agent_message":
        return AgentMessageItem(
            item_id=item_id,
            text=str(item.get("text", "") or ""),
            completed=completed,
        )
    if item_type == "command_execution":
        exit_code_raw = item.get("exit_code")
        return CommandExecutionItem(
            item_id=item_id,
            command=str(item.get("command", "") or ""),
            status=str(item.get("status", "") or ""),
            exit_code=int(exit_code_raw) if exit_code_raw is not None else None,
            completed=completed,
        )
    if item_type == "mcp_tool_call":
        return McpToolCallItem(
            item_id=item_id,
            server=str(item.get("server", "") or ""),
            tool=str(item.get("tool", "") or ""),
            status=str(item.get("status", "") or ""),
            completed=completed,
        )
    if item_type == "web_search":
        return WebSearchItem(item_id=item_id, completed=completed)
    if item_type == "todo_list":
        return TodoListItem(item_id=item_id, completed=completed)
    if item_type == "file_change":
        return FileChangeItem(item_id=item_id, completed=completed)
    if item_type == "reasoning":
        return ReasoningItem(item_id=item_id, completed=completed)

    return UnknownItem(
        item_id=item_id,
        item_type=item_type,
        completed=completed,
        raw=item,
    )


# --- Stateless conversion for events that don't need translator state ---


def to_bridge_event(event: CodexEvent) -> BridgeEvent | None:
    """Convert a stateless Codex event to a BridgeEvent.

    Returns ``None`` for events that are agent-internal (thread/turn lifecycle,
    reasoning, item.updated/completed for agent_message — those are owned by
    :class:`CodexEventTranslator` because they need cross-event state).
    """
    match event:
        case CommandExecutionItem(command=cmd, completed=False):
            preview = cmd.splitlines()[0][:80] if cmd else ""
            label = f"Running: {preview}" if preview else "Running command..."
            return StatusUpdate(status=label)
        case McpToolCallItem(server=server, tool=tool, completed=False):
            label = f"Using {server}.{tool}..." if server else f"Using {tool}..."
            return StatusUpdate(status=label)
        case WebSearchItem(completed=False):
            return StatusUpdate(status="Searching the web...")
        case TodoListItem(completed=True):
            return StatusUpdate(status="Updated todo list")
        case FileChangeItem(completed=True):
            return StatusUpdate(status="Edited files")
        case _:
            return None


# --- Stateful translator: full event stream → BridgeEvent stream ---


class CodexEventTranslator:
    """Converts a stream of Codex events into BridgeEvents, holding state.

    Owns two pieces of cross-event state that pure functions can't:

    - ``_item_text``: cumulative agent_message text per ``item_id``. Codex
      sends each ``item.updated`` carrying the *full* current text, not a
      delta — we diff against the last seen value to emit incremental
      :class:`TextDelta` events.
    - ``_last_assistant_text``: the most recently completed agent_message,
      used as the body of the final :class:`Completion` constructed when
      ``turn.completed`` arrives (codex doesn't include the assistant text
      in that envelope, only token usage).

    The controller still owns ``thread.started`` because that side-effect
    (writing into the persistent thread_map) is not the translator's job.
    """

    def __init__(self) -> None:
        self._item_text: dict[str, str] = {}
        self._last_assistant_text: str = ""

    @property
    def last_assistant_text(self) -> str:
        return self._last_assistant_text

    def translate(self, event: CodexEvent) -> list[BridgeEvent]:
        if isinstance(event, AgentMessageItem):
            return self._translate_agent_message(event)

        if isinstance(event, TurnCompletedEvent):
            return [
                Completion(
                    text=self._last_assistant_text,
                    is_error=False,
                    cost_usd=0.0,
                    duration_ms=0,
                    metadata={
                        "input_tokens": event.input_tokens,
                        "cached_input_tokens": event.cached_input_tokens,
                        "output_tokens": event.output_tokens,
                        "reasoning_output_tokens": event.reasoning_output_tokens,
                    },
                )
            ]

        if isinstance(event, TurnFailedEvent):
            return [
                Completion(
                    text=event.message or "Codex turn failed",
                    is_error=True,
                )
            ]

        if isinstance(event, ErrorEvent):
            return [Completion(text=event.message or "Codex error", is_error=True)]

        bridge_event = to_bridge_event(event)
        return [bridge_event] if bridge_event is not None else []

    def _translate_agent_message(self, event: AgentMessageItem) -> list[BridgeEvent]:
        prev = self._item_text.get(event.item_id, "")
        new_text = event.text

        if not new_text:
            # Empty update; nothing to emit. Still record completion.
            if event.completed:
                self._last_assistant_text = new_text
            return []

        if new_text.startswith(prev):
            delta = new_text[len(prev):]
        else:
            # Non-monotonic update (codex rewrote the message). Emit the
            # whole new text rather than dropping it; downstream rendering
            # will append, which produces visible duplication but never
            # silent loss.
            logger.debug(
                "Non-monotonic agent_message update for %s; sending full text",
                event.item_id,
            )
            delta = new_text

        self._item_text[event.item_id] = new_text
        if event.completed:
            self._last_assistant_text = new_text

        if not delta:
            return []
        return [TextDelta(text=delta)]

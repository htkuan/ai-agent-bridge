"""The canonical event scripts every platform renders in its tests.

Standardising the *stimulus* is what lets each platform's rendering spec be
read side by side: the assertions differ (a Slack message, a callback
payload, a log line), the events that produced them do not.

Treat these as immutable — take a copy (``list(ALL_EVENTS)``) before handing
them to anything that might mutate the list.
"""

from __future__ import annotations

from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)

# One of every event type, in the order a real turn produces them.
ALL_EVENTS: list[BridgeEvent] = [
    Processing(),
    TextDelta(text="hello"),
    StatusUpdate(status="Using Bash...", detail="ls"),
    UserQuestion(questions=[{"question": "which?"}]),
    Completion(text="done"),
]

# A turn that dies mid-flight: no Completion, so ``on_stream_end`` is the
# only thing left to give the consumer a terminal state.
CUT_STREAM: list[BridgeEvent] = [
    Processing(),
    TextDelta(text="partial"),
    StatusUpdate(status="Running tests"),
]

__all__ = ["ALL_EVENTS", "CUT_STREAM"]

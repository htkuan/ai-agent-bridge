from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Processing:
    """Processing slot acquired, agent is starting."""


@dataclass
class TextDelta:
    """Incremental text from the agent."""

    text: str


@dataclass
class StatusUpdate:
    """Agent is performing an action (tool use, thinking, etc.)."""

    status: str
    detail: str = ""


@dataclass
class Completion:
    """Agent finished responding."""

    text: str
    is_error: bool = False
    cost_usd: float = 0.0
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)


type BridgeEvent = Processing | TextDelta | StatusUpdate | Completion

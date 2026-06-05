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
class UserQuestion:
    """Agent is asking the user a question (e.g. AskUserQuestion tool)."""

    questions: list[dict]


@dataclass
class Usage:
    """Generic usage/cost report for one measurement.

    The same shape serves both a single turn (one agent invocation) and an
    accumulated session total. Agents report raw token counts via
    ``Completion.metadata['usage']`` using these canonical keys; the bridge
    assembles them into this typed structure. ``total_tokens`` is the real
    total — input/output exclude cache, which is billed separately.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    duration_api_ms: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
            num_turns=self.num_turns + other.num_turns,
            duration_api_ms=self.duration_api_ms + other.duration_api_ms,
            duration_ms=self.duration_ms + other.duration_ms,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    @classmethod
    def from_completion(cls, completion: Completion) -> Usage | None:
        """Assemble from a Completion: token/turn detail comes from
        ``metadata['usage']``, cost/wall-duration from the first-class fields.
        Returns None when no usage was reported (e.g. bridge-minted error or
        dedupe completions).
        """
        raw = completion.metadata.get("usage")
        if not raw:
            return None
        return cls(
            input_tokens=raw.get("input_tokens", 0),
            output_tokens=raw.get("output_tokens", 0),
            cache_read_tokens=raw.get("cache_read_tokens", 0),
            cache_creation_tokens=raw.get("cache_creation_tokens", 0),
            num_turns=raw.get("num_turns", 0),
            duration_api_ms=raw.get("duration_api_ms", 0),
            duration_ms=completion.duration_ms,
            cost_usd=completion.cost_usd,
        )


@dataclass
class Completion:
    """Agent finished responding."""

    text: str
    is_error: bool = False
    cost_usd: float = 0.0
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)
    # Assembled by the bridge from metadata. ``usage`` is this turn; the bridge
    # also sets ``session_usage`` to the running total when it has tracked the
    # session from its first turn (None when partial/untracked).
    usage: Usage | None = None
    session_usage: Usage | None = None


type BridgeEvent = Processing | TextDelta | StatusUpdate | UserQuestion | Completion

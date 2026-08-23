from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeRequest:
    """One turn's input — the single argument to ``MessageRouter.handle_message``.

    Built by the platform adapter's pre-processing: the platform builds
    ``text`` (pre-tagged with sender identity if it has one) and
    ``system_prompt`` (platform-flavored directives), decides whether the
    same ``session_key`` may resume the session later (``resumable``), and
    picks the named agent to route to (``agent``; None = default).
    """

    session_key: str
    text: str
    context: dict[str, str] | None = None
    system_prompt: str | None = None
    resumable: bool = True
    agent: str | None = None

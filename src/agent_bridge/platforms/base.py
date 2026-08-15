from __future__ import annotations

from dataclasses import dataclass

from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.bridge.protocols import MessageRouter


def make_session_key(platform: str, scope: str, identifier: str) -> str:
    # Canonical `{platform}:{scope}:{identifier}` — the bridge's dedupe
    # scoping (router.py rpartition on ":") relies on this shape.
    return f"{platform}:{scope}:{identifier}"


@dataclass(frozen=True)
class BridgeRequest:
    """What pre-processing must produce before a turn is forwarded.

    Mirrors ``MessageRouter.handle_message`` parameter-for-parameter: the
    platform builds ``text`` (pre-tagged with sender identity if it has one)
    and ``system_prompt`` (platform-flavored directives), and decides whether
    the same ``session_key`` may resume the session later (``resumable``).
    """

    session_key: str
    text: str
    context: dict[str, str] | None = None
    system_prompt: str | None = None
    resumable: bool = True


class BasePlatformAdapter[RunStateT]:
    """Shared adapter flow: pre-process → forward → post-process.

    The ``PlatformAdapter`` protocol stays the contract; subclassing this is
    optional reuse. A subclass's platform callback pre-processes its native
    event into a ``BridgeRequest`` and calls ``process``; the base forwards
    it to the bridge and dispatches every streamed event to an ``on_*`` hook.
    ``RunStateT`` is whatever per-turn state the hooks need (a render state,
    a session key, ...).

    Every hook has a working default: the per-event hooks delegate to the
    ``on_event`` catch-all, which no-ops. Override the specific hooks to
    render each event differently, or just ``on_event`` to treat them all
    uniformly. Errors are not swallowed here — ``process`` propagates, and
    each platform keeps its own error envelope (what to reset, where to log).
    """

    def __init__(self, bridge: MessageRouter) -> None:
        self._bridge = bridge

    # --- lifecycle: override what the platform needs ---

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def cleanup(self) -> int:
        """Periodic housekeeping; returns entries removed."""
        return 0

    # --- the shared turn ---

    async def process(
        self, request: BridgeRequest, state: RunStateT
    ) -> Completion | None:
        """Forward one turn to the bridge, dispatching each event to its hook.

        Returns the final ``Completion``, or None when the stream ended
        without one (``on_stream_end`` has then run as the safety net).
        """
        final: Completion | None = None
        async for event in self._bridge.handle_message(
            session_key=request.session_key,
            text=request.text,
            context=request.context,
            system_prompt=request.system_prompt,
            resumable=request.resumable,
        ):
            match event:
                case Processing():
                    await self.on_processing(state, event)
                case TextDelta():
                    await self.on_text_delta(state, event)
                case StatusUpdate():
                    await self.on_status_update(state, event)
                case UserQuestion():
                    await self.on_user_question(state, event)
                case Completion():
                    final = event
                    await self.on_completion(state, event)
        if final is None:
            await self.on_stream_end(state)
        return final

    # --- post-processing hooks: default to the catch-all, then to no-op ---

    async def on_processing(self, state: RunStateT, event: Processing) -> None:
        await self.on_event(state, event)

    async def on_text_delta(self, state: RunStateT, event: TextDelta) -> None:
        await self.on_event(state, event)

    async def on_status_update(self, state: RunStateT, event: StatusUpdate) -> None:
        await self.on_event(state, event)

    async def on_user_question(self, state: RunStateT, event: UserQuestion) -> None:
        await self.on_event(state, event)

    async def on_completion(self, state: RunStateT, event: Completion) -> None:
        await self.on_event(state, event)

    async def on_event(self, state: RunStateT, event: BridgeEvent) -> None:
        """Catch-all for adapters that treat every event uniformly."""

    async def on_stream_end(self, state: RunStateT) -> None:
        """Safety net: called only when the stream ended without Completion."""

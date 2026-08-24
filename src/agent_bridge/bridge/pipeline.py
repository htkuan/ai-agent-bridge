"""The middleware pipeline: one turn flows inward through a chain of
stages to the core, and the event stream flows back out through them.

Contract every stage must honor:

- **Short-circuit** = yield exactly one ``Completion`` and return without
  calling ``call_next``.
- **Forwarding** must neither inject nor swallow ``Completion`` events —
  the controllers' exactly-one-``Completion`` guarantee has to survive the
  whole chain. Decorating events in place (``UsageStage``) is fine.
- **Cleanup goes in ``try``/``finally``.** A platform that stops consuming
  closes the generator chain (``GeneratorExit``); ``finally`` is the only
  block guaranteed to run on every exit path, and on that path a stage
  must only clean up, never yield.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_bridge.bridge.events import BridgeEvent, Processing
from agent_bridge.bridge.protocols import AgentController
from agent_bridge.bridge.request import BridgeRequest


@dataclass
class TurnContext:
    """Mutable per-turn state, enriched as the request travels inward.

    ``agent``/``controller`` are filled by the agent-resolution stage,
    ``session_id``/``is_new`` by the session-resolution stage. They are
    Optional only because the type system can't express "set by the time
    the core runs" — the core guards at runtime instead.
    """

    request: BridgeRequest
    agent: str | None = None
    controller: AgentController | None = None
    session_id: str | None = None
    is_new: bool = False


type Handler = Callable[[TurnContext], AsyncIterator[BridgeEvent]]


class BridgeMiddleware(Protocol):
    def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]: ...


def compose(middlewares: Sequence[BridgeMiddleware], core: Handler) -> Handler:
    """Wrap ``core`` in ``middlewares``; the first element is outermost."""
    handler = core
    for middleware in reversed(middlewares):
        handler = _bind(middleware, handler)
    return handler


def _bind(middleware: BridgeMiddleware, call_next: Handler) -> Handler:
    def handler(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
        return middleware(ctx, call_next)

    return handler


async def run_agent(ctx: TurnContext) -> AsyncIterator[BridgeEvent]:
    """The core: every gate has passed — announce and invoke the agent.

    ``Processing`` is emitted here rather than by the capacity stage so a
    pipeline assembled without one still opens its stream correctly.
    """
    controller = ctx.controller
    session_id = ctx.session_id
    if controller is None or session_id is None:
        raise RuntimeError(
            "Pipeline misassembled: the core needs the resolution stages "
            "to have set ctx.controller and ctx.session_id"
        )
    yield Processing()
    request = ctx.request
    async for event in controller.run(
        session_id,
        request.text,
        ctx.is_new,
        context=request.context,
        system_prompt=request.system_prompt,
    ):
        yield event

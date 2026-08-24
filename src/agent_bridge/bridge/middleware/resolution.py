"""The two mandatory stages: name → controller, key → session.

Both enrich the ``TurnContext`` for everything further in; removing either
leaves the core without a controller or session to run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Mapping

from agent_bridge.bridge.events import BridgeEvent, Completion
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.protocols import AgentController
from agent_bridge.bridge.session import SessionManager

logger = logging.getLogger(__name__)


class AgentResolutionStage:
    """Resolve ``request.agent`` to a controller; unknown names short-circuit.

    Resolution happens *first* — before the dedupe claim, session mint and
    capacity lease — so a misconfigured name can't poison any shared state.
    ``None`` resolves to the configured default *name* when one is set
    (sessions then stick to the actual profile, and a redeploy that flips
    the default abandons them like any remap), otherwise to the env-built
    default controller.
    """

    def __init__(
        self,
        controller: AgentController,
        named_controllers: Mapping[str, AgentController],
        default_agent: str | None,
    ) -> None:
        self._controller = controller
        self._named_controllers = dict(named_controllers)
        self._default_agent = default_agent

    async def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        agent = (
            ctx.request.agent if ctx.request.agent is not None else self._default_agent
        )
        ctx.agent = agent
        if agent is None:
            ctx.controller = self._controller
        else:
            named = self._named_controllers.get(agent)
            if named is None:
                # Startup validation makes this unreachable for env-built
                # configs; the guard covers programmatically assembled ones.
                logger.warning(
                    "Unknown agent %r for session key %s",
                    agent,
                    ctx.request.session_key,
                )
                yield Completion(
                    text=f"Unknown agent {agent!r} — check the server configuration.",
                    is_error=True,
                    metadata={"error_code": "unknown_agent"},
                )
                return
            ctx.controller = named
        async for event in call_next(ctx):
            yield event


class SessionResolutionStage:
    """Resolve the session key to a session id (and whether it's new).

    ``resumable=True`` goes through the ``SessionManager`` — persisted,
    agent-sticky, TTL-bound. ``resumable=False`` mints a fresh ephemeral
    UUID and leaves no trace: every such call is conceptually independent
    (heartbeat ticks, one-shot triggers).
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        request = ctx.request
        if request.resumable:
            ctx.session_id, ctx.is_new = await self._session_manager.get_or_create(
                request.session_key, agent=ctx.agent
            )
        else:
            ctx.session_id = str(uuid.uuid4())
            ctx.is_new = True
        logger.info(
            "Session %s (new=%s, resumable=%s) for key %s",
            ctx.session_id,
            ctx.is_new,
            request.resumable,
            request.session_key,
        )
        async for event in call_next(ctx):
            yield event

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from agent_bridge.bridge.capacity import SemaphoreCapacityLimiter
from agent_bridge.bridge.config import RouterConfig
from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.middleware import (
    AgentResolutionStage,
    CapacityStage,
    DedupeStage,
    SessionResolutionStage,
    UsageStage,
)
from agent_bridge.bridge.pipeline import (
    BridgeMiddleware,
    TurnContext,
    compose,
    run_agent,
)
from agent_bridge.bridge.protocols import (
    AgentController,
    CapacityLimiter,
    DedupeCache,
)
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.bridge.session import SessionManager


class Bridge:
    """``MessageRouter`` implementation: a thin shell over the composed
    default pipeline.

    THE STAGE ORDER ENCODES INVARIANTS — it is fixed here, not
    configurable. Outermost first:

    1. ``AgentResolutionStage`` — an unknown agent name short-circuits
       before any shared state (dedupe claim, session mint, capacity
       lease) is touched.
    2. ``DedupeStage`` (only when a cache is configured) — a duplicate
       short-circuits before a session is minted or ``last_used`` moves;
       every failure flowing out from further in (capacity reject, error
       ``Completion``, exception) releases the claim on its way past.
    3. ``SessionResolutionStage`` — needs ``ctx.agent`` (sessions stick to
       the resolved profile).
    4. ``UsageStage`` — needs ``ctx.session_id``/``is_new``; tracks at mint
       so the running total is trustworthy from the session's first turn.
    5. ``CapacityStage`` — the last gate before the agent actually runs.
    6. ``run_agent`` (core) — yields ``Processing``, invokes the controller.

    What *is* swappable are the ports each stage drinks from: the session
    store behind ``session_manager``, the ``dedupe`` cache, the capacity
    ``limiter``. app.py picks the implementations; ``None`` gets the
    built-in defaults (in-process semaphore) or disables the stage
    (dedupe).
    """

    def __init__(
        self,
        config: RouterConfig,
        session_manager: SessionManager,
        controller: AgentController,
        dedupe: DedupeCache | None = None,
        *,
        named_controllers: Mapping[str, AgentController] | None = None,
        default_agent: str | None = None,
        limiter: CapacityLimiter | None = None,
    ) -> None:
        self._usage = UsageStage()
        if limiter is None:
            limiter = SemaphoreCapacityLimiter(config.max_concurrent_sessions)
        stages: list[BridgeMiddleware] = [
            AgentResolutionStage(controller, named_controllers or {}, default_agent)
        ]
        if dedupe is not None:
            stages.append(DedupeStage(dedupe))
        stages.extend(
            [
                SessionResolutionStage(session_manager),
                self._usage,
                CapacityStage(limiter),
            ]
        )
        self._handler = compose(stages, run_agent)

    async def handle_message(
        self, request: BridgeRequest
    ) -> AsyncIterator[BridgeEvent]:
        """One turn: the request travels inward through the stages, the
        agent's event stream travels back out through them. Behaviour per
        stage is documented on the class."""
        async for event in self._handler(TurnContext(request=request)):
            yield event

    def forget_session_usage(self, session_id: str) -> None:
        """Drop a session's accumulated usage (called on TTL purge)."""
        self._usage.forget(session_id)

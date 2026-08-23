"""The global capacity gate as a pipeline stage.

No queuing: a full limiter is an immediate error ``Completion``, which
flows out through the outer stages (so e.g. dedupe releases its claim).
The lease is given back in ``finally`` — normal end, error, exception and
abandoned stream all return the slot.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.protocols import CapacityLimiter

logger = logging.getLogger(__name__)


class CapacityStage:
    def __init__(self, limiter: CapacityLimiter) -> None:
        self._limiter = limiter

    async def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        lease = await self._limiter.try_acquire()
        if lease is None:
            logger.warning("No available slot for session %s", ctx.request.session_key)
            yield Completion(
                text="Too many requests being processed, please try again later.",
                is_error=True,
                metadata={"error_code": "capacity_full"},
            )
            return
        try:
            async for event in call_next(ctx):
                yield event
        finally:
            await lease.release()

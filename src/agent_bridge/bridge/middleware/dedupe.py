"""Cross-session duplicate suppression as a pipeline stage.

Sits *outside* the session stage (a duplicate must not mint a session or
touch ``last_used``) and *outside* the capacity stage — which is what
makes the release bookkeeping automatic: a capacity reject, a controller
error ``Completion`` and a raised exception all pass this stage on their
way out, and the single ``try``/``finally`` here releases the claim
accordingly. Under the monolithic router this logic was smeared across
three exit paths.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion
from agent_bridge.bridge.pipeline import Handler, TurnContext
from agent_bridge.bridge.protocols import DedupeCache

logger = logging.getLogger(__name__)


class DedupeStage:
    def __init__(self, cache: DedupeCache) -> None:
        self._cache = cache

    async def __call__(
        self, ctx: TurnContext, call_next: Handler
    ) -> AsyncIterator[BridgeEvent]:
        request = ctx.request
        # Skipped for non-resumable triggers (e.g. heartbeat ticks, where the
        # same prompt firing on a schedule is meaningful, not a duplicate),
        # for keys without the `{platform}:{scope}:{id}` shape, and for
        # blank prompts.
        if (
            not request.resumable
            or ":" not in request.session_key
            or not request.text.strip()
        ):
            async for event in call_next(ctx):
                yield event
            return

        # Drop the identifier so cross-thread duplicates collapse.
        scope = request.session_key.rpartition(":")[0]
        decision = await self._cache.lookup_or_claim(
            scope, request.text, first_session_key=request.session_key
        )
        if decision.hit is not None:
            hit = decision.hit
            state = "in_flight" if hit.in_flight else "recent_hit"
            logger.info(
                "dedupe_hit scope=%s state=%s match=%s hamming=%d "
                "first_session=%s matched=%r",
                scope,
                state,
                "exact" if hit.hamming == 0 else "simhash",
                hit.hamming,
                hit.first_session_key,
                hit.matched_text,
            )
            yield Completion(
                text=":repeat: Duplicate detected — skipping.",
                is_error=False,
                metadata={
                    "dedupe": state,
                    "first_session_key": hit.first_session_key,
                },
            )
            return

        # Claimed. Anything but a clean, non-error Completion counts as
        # failed — an error Completion (timeout, non-zero exit, API error),
        # a capacity reject flowing out from further in, an exception, or an
        # abandoned stream — so retries aren't blocked for the full TTL.
        failed = True
        try:
            last_completion_error = False
            async for event in call_next(ctx):
                if isinstance(event, Completion):
                    last_completion_error = event.is_error
                yield event
            failed = last_completion_error
        finally:
            if failed:
                await self._cache.mark_failed(scope, decision.claim_token)
            else:
                await self._cache.mark_completed(scope, decision.claim_token)

"""Contract suite for the ``DedupeCache`` port.

The port is algorithm-neutral: implementations receive raw prompt text
and decide sameness themselves. What every implementation must honor is
the claim lifecycle — first caller claims, duplicates hit (in-flight,
then recent once completed), ``mark_failed`` reopens the slot — and that
``claim_token`` is opaque: whatever ``lookup_or_claim`` hands out is what
``mark_*`` accepts.
"""

from __future__ import annotations

import pytest

from agent_bridge.bridge.config import DedupeConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.protocols import DedupeCache


@pytest.fixture(params=["prompt-cache"])
def cache(request: pytest.FixtureRequest) -> DedupeCache:
    return PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))


async def test_first_lookup_claims(cache: DedupeCache):
    decision = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t1")
    assert decision.hit is None
    assert decision.claim_token  # opaque but non-empty


async def test_duplicate_hits_in_flight_until_completed(cache: DedupeCache):
    claim = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t1")

    dup = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t2")
    assert dup.hit is not None
    assert dup.hit.in_flight is True
    assert dup.hit.first_session_key == "slack:C1:t1"

    await cache.mark_completed("slack:C1", claim.claim_token)
    dup2 = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t3")
    assert dup2.hit is not None
    assert dup2.hit.in_flight is False


async def test_mark_failed_reopens_the_slot(cache: DedupeCache):
    claim = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t1")
    await cache.mark_failed("slack:C1", claim.claim_token)

    retry = await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t2")
    assert retry.hit is None  # claimable again — a failed run must not block


async def test_scopes_are_isolated(cache: DedupeCache):
    await cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t1")
    other = await cache.lookup_or_claim("slack:C2", "alert", "slack:C2:t1")
    assert other.hit is None


async def test_mark_with_unknown_token_is_a_noop(cache: DedupeCache):
    # Releasing a claim that expired (or never existed) must be harmless.
    await cache.mark_completed("slack:C1", "no-such-token")
    await cache.mark_failed("slack:C1", "no-such-token")

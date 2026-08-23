"""Contract suite for the ``CapacityLimiter`` port.

``try_acquire`` must answer immediately — a slot or ``None``, never a
queue — and a lease must release exactly one slot no matter how many
times ``release`` is called. A distributed implementation joins by
adding a fixture param.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agent_bridge.bridge.capacity import SemaphoreCapacityLimiter
from agent_bridge.bridge.protocols import CapacityLimiter

type LimiterFactory = Callable[[int], CapacityLimiter]


@pytest.fixture(params=["semaphore"])
def make_limiter(request: pytest.FixtureRequest) -> LimiterFactory:
    def factory(max_concurrent: int) -> CapacityLimiter:
        return SemaphoreCapacityLimiter(max_concurrent)

    return factory


async def test_acquires_up_to_capacity_then_rejects(make_limiter: LimiterFactory):
    limiter = make_limiter(2)
    first = await limiter.try_acquire()
    second = await limiter.try_acquire()
    assert first is not None
    assert second is not None
    assert await limiter.try_acquire() is None
    await first.release()
    await second.release()


async def test_release_frees_a_slot(make_limiter: LimiterFactory):
    limiter = make_limiter(1)
    lease = await limiter.try_acquire()
    assert lease is not None
    assert await limiter.try_acquire() is None

    await lease.release()
    again = await limiter.try_acquire()
    assert again is not None
    await again.release()


async def test_lease_release_is_idempotent(make_limiter: LimiterFactory):
    limiter = make_limiter(1)
    lease = await limiter.try_acquire()
    assert lease is not None
    await lease.release()
    await lease.release()  # double release must not mint a phantom slot

    one = await limiter.try_acquire()
    assert one is not None
    assert await limiter.try_acquire() is None
    await one.release()


async def test_try_acquire_never_blocks(make_limiter: LimiterFactory):
    limiter = make_limiter(1)
    lease = await limiter.try_acquire()
    assert lease is not None
    # A full limiter must answer None promptly, not wait for the slot.
    async with asyncio.timeout(1.0):
        assert await limiter.try_acquire() is None
    await lease.release()

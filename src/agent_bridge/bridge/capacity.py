"""Built-in ``CapacityLimiter``: an in-process semaphore.

One shared limiter gates every agent run in this process. A distributed
deployment would swap in an implementation whose leases are backed by an
external token (Redis, a database row) — same port, same call sites.
"""

from __future__ import annotations

import asyncio


class _SemaphoreLease:
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    async def release(self) -> None:
        # Idempotent: the holder releases in a finally that can run on any
        # exit path, and double-releasing a semaphore would corrupt the gate.
        if not self._released:
            self._released = True
            self._semaphore.release()


class SemaphoreCapacityLimiter:
    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def try_acquire(self) -> _SemaphoreLease | None:
        if self._semaphore.locked():
            return None
        await self._semaphore.acquire()
        return _SemaphoreLease(self._semaphore)

"""Small helpers shared across the test tree (no doubles, no fixtures)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def wait_until(
    predicate: Callable[[], bool],
    # Deadline for a sync-predicate poll loop; asyncio.timeout can't help here.
    timeout: float = 5.0,  # noqa: ASYNC109
) -> None:
    """Poll until ``predicate()`` holds; raise TimeoutError if it never does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met within timeout")
        await asyncio.sleep(0.01)

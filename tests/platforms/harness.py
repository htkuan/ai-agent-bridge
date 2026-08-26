"""The shape every platform's test harness implements.

An adapter has exactly three seams, and a harness is the thing that exposes
all three so a test never has to reach into the adapter:

    trigger (inbound)  → ``deliver()``   drive it the way production does
    router             → ``requests()``  what reached ``MessageRouter``
    surface (outbound) → ``output()``    what the consumer is left with

``deliver()`` takes no arguments on purpose. What a turn *contains* is
decided when the harness is built (its config, its scripted events), and the
per-platform builders expose richer methods — ``post(conversation_id=…)``,
``send(text, ts=…)`` — for tests that need them. Keeping the shared verb
argument-free is what lets one suite drive every platform without pretending
that, say, a heartbeat tick has a caller-chosen session or prompt.

Concrete harnesses live next to the platform they drive
(``tests/platforms/{name}/harness.py``) and are reused by the e2e rigs with
a real ``Bridge`` swapped in for the fake.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.protocols import PlatformAdapter
from agent_bridge.bridge.request import BridgeRequest


class PlatformHarness[OutputT](Protocol):
    """One platform, wired to test doubles, driveable through its real path."""

    adapter: PlatformAdapter

    async def deliver(self) -> None:
        """Make one turn happen, the way the platform's trigger does, and
        return once the turn is observable (awaited, drained, or polled)."""
        ...

    def requests(self) -> list[BridgeRequest]:
        """Every ``BridgeRequest`` that reached the router, in order."""
        ...

    def output(self) -> OutputT:
        """What the platform's consumer is left looking at."""
        ...


class HarnessFactory(Protocol):
    """Builds a harness. Platform-specific builders add a typed ``config``
    keyword; the knobs here are the ones a platform-agnostic suite needs."""

    def __call__(
        self,
        *,
        events: list[BridgeEvent] | None = None,
        capacity_full: bool = False,
        known_agents: frozenset[str] = frozenset(),
        raises: bool = False,
    ) -> AbstractAsyncContextManager[PlatformHarness[Any]]: ...


__all__ = ["HarnessFactory", "PlatformHarness"]

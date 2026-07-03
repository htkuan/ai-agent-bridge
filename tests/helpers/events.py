from __future__ import annotations

from collections.abc import AsyncIterator

from agent_bridge.events import BridgeEvent


async def collect_events(aiter: AsyncIterator[BridgeEvent]) -> list[BridgeEvent]:
    return [event async for event in aiter]


def event_types(events: list[BridgeEvent]) -> list[type]:
    return [type(event) for event in events]

from __future__ import annotations

from typing import Protocol


class PlatformAdapter(Protocol):
    """Interface for chat platform adapters."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

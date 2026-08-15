from __future__ import annotations


class FakePlatformAdapter:
    """``PlatformAdapter`` double that records its lifecycle."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.cleaned = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def cleanup(self) -> int:
        self.cleaned += 1
        return 0

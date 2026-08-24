from __future__ import annotations

from agent_bridge.bridge.protocols import SessionEntry, SessionStoreError


class InMemorySessionStore:
    """``SessionStore`` double: a dict, plus a switch to make writes fail.

    ``fail_writes=True`` makes every mutation raise ``SessionStoreError``
    without applying — the atomicity the port demands — so policy-level
    rollback behaviour can be tested without touching the filesystem.
    """

    def __init__(self) -> None:
        self.entries: dict[str, SessionEntry] = {}
        self.fail_writes = False

    async def get(self, key: str) -> SessionEntry | None:
        return self.entries.get(key)

    async def put(self, key: str, entry: SessionEntry) -> None:
        if self.fail_writes:
            raise SessionStoreError("Failed to persist session store: fake failure")
        self.entries[key] = entry

    async def delete(self, key: str) -> None:
        if self.fail_writes:
            raise SessionStoreError("Failed to persist session store: fake failure")
        self.entries.pop(key, None)

    async def list_all(self) -> dict[str, SessionEntry]:
        return dict(self.entries)

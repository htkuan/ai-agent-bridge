"""Built-in ``SessionStore`` implementation: one JSON file.

Deliberately stateless: every operation reads the file fresh and every
mutation rewrites it whole. The file is small (one entry per live session)
and the call rate is one read/write per message, so simplicity wins over
caching — and a stateless store means tests and operators can inspect or
edit the file directly and the store always agrees with the disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_bridge.bridge.protocols import SessionEntry, SessionStoreError

logger = logging.getLogger(__name__)


class JsonSessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def get(self, key: str) -> SessionEntry | None:
        return self._read().get(key)

    async def put(self, key: str, entry: SessionEntry) -> None:
        entries = self._read()
        entries[key] = entry
        self._write(entries)

    async def delete(self, key: str) -> None:
        entries = self._read()
        if entries.pop(key, None) is not None:
            self._write(entries)

    async def list_all(self) -> dict[str, SessionEntry]:
        return self._read()

    def _read(self) -> dict[str, SessionEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load session store: %s", e)
            return {}
        return {key: _entry_from(value) for key, value in raw.items()}

    def _write(self, entries: dict[str, SessionEntry]) -> None:
        payload = {key: _entry_to(entry) for key, entry in entries.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, indent=2))
        except OSError as e:
            logger.error("Failed to save session store: %s", e)
            raise SessionStoreError(f"Failed to persist session store: {e}") from e


def _entry_from(value: dict[str, str]) -> SessionEntry:
    return SessionEntry(
        session_id=value.get("session_id", ""),
        created_at=value.get("created_at", ""),
        last_used=value.get("last_used", ""),
        agent=value.get("agent"),
    )


def _entry_to(entry: SessionEntry) -> dict[str, str]:
    # ``agent`` is omitted when None so the file format stays byte-compatible
    # with pre-profile stores (and with what older versions can read back).
    value = {
        "session_id": entry.session_id,
        "created_at": entry.created_at,
        "last_used": entry.last_used,
    }
    if entry.agent is not None:
        value["agent"] = entry.agent
    return value

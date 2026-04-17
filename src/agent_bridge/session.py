from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, store_path: Path, ttl_hours: float = 72.0) -> None:
        self._store_path = store_path
        self._ttl = timedelta(hours=ttl_hours)
        self._sessions: dict[str, dict] = {}
        self._load()
        self._purge_expired()

    def get_or_create(self, key: str) -> tuple[str, bool]:
        """Get existing session or create a new one. Returns (session_id, is_new).

        If the session exists but has expired, it is removed and a new one is created.
        If the file write fails, the in-memory change is rolled back.
        """
        if key in self._sessions:
            if self._is_expired(self._sessions[key]):
                logger.info("Session expired for key %s, creating new one", key)
                del self._sessions[key]
            else:
                old_last_used = self._sessions[key]["last_used"]
                self._sessions[key]["last_used"] = _now_iso()
                if not self._save():
                    self._sessions[key]["last_used"] = old_last_used
                return self._sessions[key]["session_id"], False

        session_id = str(uuid.uuid4())
        new_entry = {
            "session_id": session_id,
            "created_at": _now_iso(),
            "last_used": _now_iso(),
        }
        self._sessions[key] = new_entry
        if not self._save():
            del self._sessions[key]
            raise OSError(f"Failed to persist new session for key {key}")
        logger.info("Created new session %s for key %s", session_id, key)
        return session_id, True

    def get(self, key: str) -> str | None:
        """Get session ID by key, or None if not found or expired."""
        entry = self._sessions.get(key)
        if entry is None:
            return None
        if self._is_expired(entry):
            return None
        return entry["session_id"]

    def delete(self, key: str) -> bool:
        """Delete a session mapping. Returns True if it existed."""
        if key in self._sessions:
            removed = self._sessions.pop(key)
            if not self._save():
                self._sessions[key] = removed
                return False
            return True
        return False

    def list_sessions(self) -> dict[str, dict]:
        """Return a copy of all non-expired session mappings."""
        return {k: v for k, v in self._sessions.items() if not self._is_expired(v)}

    def purge_expired(self) -> list[str]:
        """Remove all expired sessions. Returns session IDs of purged entries."""
        return self._purge_expired()

    def _is_expired(self, entry: dict) -> bool:
        last_used = _parse_iso(entry.get("last_used", ""))
        if last_used is None:
            return True
        return _now() - last_used > self._ttl

    def _purge_expired(self) -> list[str]:
        expired = [
            (k, v["session_id"])
            for k, v in self._sessions.items()
            if self._is_expired(v)
        ]
        for key, _ in expired:
            logger.info("Purging expired session for key %s", key)
            del self._sessions[key]
        if expired:
            self._save()
        return [sid for _, sid in expired]

    def _load(self) -> None:
        if self._store_path.exists():
            try:
                self._sessions = json.loads(self._store_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load session store: %s", e)
                self._sessions = {}
        else:
            self._sessions = {}

    def _save(self) -> bool:
        """Write sessions to disk. Returns True on success, False on failure."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(self._sessions, indent=2))
            return True
        except OSError as e:
            logger.error("Failed to save session store: %s", e)
            return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

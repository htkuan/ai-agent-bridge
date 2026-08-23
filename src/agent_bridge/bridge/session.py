from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_bridge.bridge.config import SessionConfig
from agent_bridge.bridge.protocols import SessionEntry, SessionStore, SessionStoreError
from agent_bridge.bridge.stores import JsonSessionStore

logger = logging.getLogger(__name__)


class SessionManager:
    """Session *policy* on top of a pluggable ``SessionStore``.

    Owns TTL expiry, agent affinity (a remapped key must not resume a
    session created under a different agent), and orphan draining. The
    store owns nothing but persistence; swap it (JSON file → RDBMS) and
    every rule here still holds.
    """

    def __init__(
        self, config: SessionConfig, store: SessionStore | None = None
    ) -> None:
        self._config = config
        self._ttl = timedelta(hours=config.ttl_hours)
        self._store: SessionStore = (
            store if store is not None else JsonSessionStore(config.store_path)
        )
        # Session IDs abandoned by an agent remap; drained by purge_expired()
        # so the app's cleanup loop treats them like TTL-purged sessions.
        # In-memory: a remap observed by this process is drained by this
        # process's cleanup loop.
        self._orphaned: list[str] = []

    async def get_or_create(
        self, key: str, agent: str | None = None
    ) -> tuple[str, bool]:
        """Get existing session or create a new one. Returns (session_id, is_new).

        An expired entry is replaced by a fresh one. The same happens when
        the entry's stored ``agent`` differs from the requested one: a
        remapped key must not resume a session created under a different
        agent's work dir (the CLI stores sessions per project directory —
        resuming elsewhere fails), so the old session is orphaned and a
        fresh one minted. A failed persist of a *touch* keeps the resume
        working with the old ``last_used``; a failed persist of a *new*
        session raises — nothing was recorded, the caller must not proceed.
        """
        entry = await self._store.get(key)
        if entry is not None:
            if self._is_expired(entry):
                logger.info("Session expired for key %s, creating new one", key)
            elif entry.agent != agent:
                logger.info(
                    "Session for key %s belongs to agent %r, now %r — recreating",
                    key,
                    entry.agent,
                    agent,
                )
                self._orphaned.append(entry.session_id)
            else:
                touched = replace(entry, last_used=_now_iso())
                try:
                    await self._store.put(key, touched)
                except SessionStoreError:
                    # Store guarantees nothing changed — resume with the old
                    # last_used rather than failing the turn.
                    logger.warning("Failed to touch session for key %s", key)
                return entry.session_id, False

        session_id = str(uuid.uuid4())
        new_entry = SessionEntry(
            session_id=session_id,
            created_at=_now_iso(),
            last_used=_now_iso(),
            agent=agent,
        )
        try:
            await self._store.put(key, new_entry)
        except SessionStoreError as e:
            raise SessionStoreError(
                f"Failed to persist new session for key {key}"
            ) from e
        logger.info("Created new session %s for key %s", session_id, key)
        return session_id, True

    async def get(self, key: str) -> str | None:
        """Get session ID by key, or None if not found or expired."""
        entry = await self._store.get(key)
        if entry is None or self._is_expired(entry):
            return None
        return entry.session_id

    async def delete(self, key: str) -> bool:
        """Delete a session mapping. Returns True if it existed and the
        removal persisted."""
        if await self._store.get(key) is None:
            return False
        try:
            await self._store.delete(key)
        except SessionStoreError:
            return False
        return True

    async def list_sessions(self) -> dict[str, SessionEntry]:
        """All non-expired session mappings."""
        entries = await self._store.list_all()
        return {k: v for k, v in entries.items() if not self._is_expired(v)}

    async def purge_expired(self) -> list[str]:
        """Remove all expired sessions. Returns session IDs of purged entries,
        plus any sessions orphaned by an agent remap since the last call."""
        entries = await self._store.list_all()
        expired = [(k, v.session_id) for k, v in entries.items() if self._is_expired(v)]
        purged: list[str] = []
        for key, session_id in expired:
            logger.info("Purging expired session for key %s", key)
            try:
                await self._store.delete(key)
            except SessionStoreError:
                # Still on disk — it stays expired, so the next cycle retries.
                logger.warning("Failed to purge expired session for key %s", key)
                continue
            purged.append(session_id)
        orphaned = self._orphaned
        self._orphaned = []
        return orphaned + purged

    def _is_expired(self, entry: SessionEntry) -> bool:
        last_used = _parse_iso(entry.last_used)
        if last_used is None:
            return True
        return _now() - last_used > self._ttl


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

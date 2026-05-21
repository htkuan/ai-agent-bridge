from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ThreadMap:
    """Persistent mapping from bridge session_id → codex thread_id.

    Codex CLI generates its own thread_id at startup; we cannot pass our
    bridge UUID into it. So after the first invocation we capture the
    codex-side id from the `thread.started` event and store it here keyed
    by the bridge session_id. On follow-ups we look it up to build
    `codex exec resume <thread_id>`.
    """

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._map: dict[str, str] = {}
        self._load()

    def get(self, bridge_session_id: str) -> str | None:
        return self._map.get(bridge_session_id)

    def set(self, bridge_session_id: str, thread_id: str) -> None:
        old = self._map.get(bridge_session_id)
        if old == thread_id:
            return
        self._map[bridge_session_id] = thread_id
        if not self._save():
            if old is None:
                self._map.pop(bridge_session_id, None)
            else:
                self._map[bridge_session_id] = old

    def delete(self, bridge_session_id: str) -> bool:
        if bridge_session_id not in self._map:
            return False
        removed = self._map.pop(bridge_session_id)
        if not self._save():
            self._map[bridge_session_id] = removed
            return False
        return True

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
            if isinstance(data, dict):
                self._map = {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load codex thread map: %s", e)
            self._map = {}

    def _save(self) -> bool:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(self._map, indent=2))
            return True
        except OSError as e:
            logger.error("Failed to save codex thread map: %s", e)
            return False

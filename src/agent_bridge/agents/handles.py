"""Persistent ``{bridge session_id: agent-native handle}`` maps.

Shared infrastructure for agents whose CLI mints its own session handle
(codex thread ids, …) instead of accepting the bridge's session id: the
controller records the CLI's handle per bridge session and looks it up on
resume. A lost mapping only degrades to a fresh session, so persistence
failures are logged, never raised.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


class SessionHandleStore:
    """Persistent ``{bridge session_id: agent-native handle}`` map (JSON file).

    Mutations do read-merge-write: two controller instances (profiles sharing
    a work_dir) may share one store file, and everything runs on one event
    loop with no awaits in here — so re-reading before each save is race-free
    in-process and loses no entries across instances.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handles = self._read()

    def get(self, session_id: str) -> str | None:
        return self._handles.get(session_id)

    def put(self, session_id: str, handle: str) -> None:
        self._handles = self._read()
        self._handles[session_id] = handle
        self._save()

    def discard(self, session_id: str) -> None:
        self._handles = self._read()
        if self._handles.pop(session_id, None) is not None:
            self._save()

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data: object = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Failed to load session handle store %s: %s — starting empty",
                self._path,
                e,
            )
            return {}
        entries = cast("dict[str, object]", data) if isinstance(data, dict) else None
        if entries is None or not all(
            isinstance(handle, str) for handle in entries.values()
        ):
            logger.warning(
                "Session handle store %s has an unexpected shape — starting empty",
                self._path,
            )
            return {}
        return cast("dict[str, str]", data)

    def _save(self) -> None:
        # Atomic replace: a crash mid-write must not corrupt the existing map.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._handles, indent=2))
            tmp.replace(self._path)
        except OSError as e:
            logger.error("Failed to save session handle store %s: %s", self._path, e)

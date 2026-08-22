from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


class SessionHandleStore:
    """Persistent {bridge session_id: agent-native handle} map (JSON file)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handles = self._load()

    def get(self, session_id: str) -> str | None:
        self._handles = self._load()
        return self._handles.get(session_id)

    def put(self, session_id: str, handle: str) -> None:
        handles = self._load()
        handles[session_id] = handle
        self._save(handles)
        self._handles = handles

    def discard(self, session_id: str) -> None:
        handles = self._load()
        handles.pop(session_id, None)
        self._save(handles)
        self._handles = handles

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load session handle store: %s", e)
            return {}
        if not isinstance(data, dict):
            logger.warning("Invalid session handle store shape at %s", self._path)
            return {}
        table = cast("Mapping[object, object]", data)
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in table.items()
        ):
            logger.warning("Invalid session handle store shape at %s", self._path)
            return {}
        return cast("dict[str, str]", table)

    def _save(self, handles: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_name(f".{self._path.name}.tmp")
            tmp_path.write_text(json.dumps(handles, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError as e:
            logger.error("Failed to save session handle store: %s", e)

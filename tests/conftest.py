from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_bridge.session import SessionManager


@pytest.fixture
def session_manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "sessions.json")


@pytest.fixture
def prepend_path(monkeypatch):
    """Prepend a directory to PATH so fake CLI scripts shadow real binaries."""

    def _prepend(bin_dir: Path) -> None:
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    return _prepend


@pytest.fixture
def clean_agent_bridge_env(monkeypatch):
    """Scrub ambient AGENT_BRIDGE_* vars so a developer's shell/.env can't
    leak into tests that read live os.environ (e.g. via ConfigSource)."""
    for key in list(os.environ):
        if key.startswith("AGENT_BRIDGE_"):
            monkeypatch.delenv(key)

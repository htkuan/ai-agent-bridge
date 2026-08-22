"""App wiring: _build_dedupe and _build_adapters turn an AppConfig into components.

Both are pure functions of the config now — no environment variables involved.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_bridge import app
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.config import DedupeConfig, RouterConfig, SessionConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.config import AppConfig
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.adapter import SlackAdapter
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.platforms.webhook.adapter import WebhookAdapter
from agent_bridge.platforms.webhook.config import WebhookConfig
from agent_bridge.server.config import HttpConfig
from agent_bridge.server.http_server import HttpServer
from tests.fakes import FakeAgentController

# --- _build_dedupe ---


def test_build_dedupe_disabled_by_default():
    assert app._build_dedupe(DedupeConfig()) is None


def test_build_dedupe_enabled_returns_cache():
    config = DedupeConfig(ttl_seconds=60.0, max_entries=32, simhash_threshold=4)
    assert isinstance(app._build_dedupe(config), PromptDedupeCache)


# --- _build_adapters ---


@pytest.fixture
def wiring(tmp_path: Path) -> tuple[Bridge, SessionManager]:
    session_manager = SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=1.0)
    )
    bridge = Bridge(RouterConfig(), session_manager, FakeAgentController())
    return bridge, session_manager


def _config(
    tmp_path: Path,
    *,
    slack: bool = False,
    heartbeat: bool = False,
    webhook: bool = False,
    http: bool = False,
) -> AppConfig:
    return AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        slack=SlackConfig(bot_token="xoxb-x", app_token="xapp-x") if slack else None,
        heartbeat=(
            HeartbeatConfig(interval_minutes=5, prompt="tick") if heartbeat else None
        ),
        webhook=WebhookConfig(token="t") if webhook else None,
        http=HttpConfig(port=0) if http else None,
    )


def test_no_adapter_configured_raises(
    wiring: tuple[Bridge, SessionManager], tmp_path: Path
):
    with pytest.raises(ValueError, match="No platform adapter configured"):
        app._build_adapters(_config(tmp_path), *wiring)


def test_slack_only(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(_config(tmp_path, slack=True), *wiring)

    assert len(adapters) == 1
    assert isinstance(adapters[0], SlackAdapter)


def test_heartbeat_only(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(_config(tmp_path, heartbeat=True), *wiring)

    assert len(adapters) == 1
    assert isinstance(adapters[0], HeartbeatAdapter)


def test_slack_and_heartbeat(wiring: tuple[Bridge, SessionManager], tmp_path: Path):
    adapters = app._build_adapters(
        _config(tmp_path, slack=True, heartbeat=True), *wiring
    )

    assert len(adapters) == 2
    assert isinstance(adapters[0], SlackAdapter)
    assert isinstance(adapters[1], HeartbeatAdapter)


# --- named controllers + startup logging: one entry per profile ---


def _profiled_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        claude_profiles={"backend": ClaudeConfig(work_dir=tmp_path)},
        pi_profiles={"fast": PiConfig(work_dir=tmp_path)},
        codex_profiles={"reviewer": CodexConfig(work_dir=tmp_path)},
    )


def test_build_named_controllers_covers_every_agent_type(tmp_path: Path):
    named = app._build_named_controllers(_profiled_config(tmp_path))
    assert isinstance(named["backend"], ClaudeController)
    assert isinstance(named["fast"], PiController)
    assert isinstance(named["reviewer"], CodexController)


def test_log_startup_config_names_every_profile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("INFO", logger="agent_bridge.app"):
        app._log_startup_config(_profiled_config(tmp_path))
    assert "Claude profile backend" in caplog.text
    assert "Pi profile fast" in caplog.text
    assert "Codex profile reviewer" in caplog.text


# --- the HTTP server and the webhook platform ---


def test_build_http_server_none_when_not_configured(tmp_path: Path):
    assert app._build_http_server(_config(tmp_path)) is None


def test_build_http_server_returns_server(tmp_path: Path):
    server = app._build_http_server(_config(tmp_path, http=True))
    assert isinstance(server, HttpServer)


async def test_webhook_mounts_router_on_http_server(
    wiring: tuple[Bridge, SessionManager], tmp_path: Path
):
    server = HttpServer(HttpConfig(port=0))
    adapters = app._build_adapters(
        _config(tmp_path, webhook=True, http=True), *wiring, http_server=server
    )

    assert len(adapters) == 1
    assert isinstance(adapters[0], WebhookAdapter)
    # The route answers (401: unauthenticated) instead of 404 — it's mounted.
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/platforms/webhook/v1/messages",
            json={"conversation_id": "c", "text": "hi"},
        )
    assert response.status_code == 401


def test_webhook_without_http_server_raises(
    wiring: tuple[Bridge, SessionManager], tmp_path: Path
):
    config = _config(tmp_path, webhook=True, http=True)
    with pytest.raises(ValueError, match="HTTP server"):
        app._build_adapters(config, *wiring, http_server=None)

"""Console routes, driven in-process through the ASGI app — no socket."""

from __future__ import annotations

import httpx
import pytest

from agent_bridge.server import console
from agent_bridge.server.config import HttpConfig
from agent_bridge.server.http_server import HttpServer


def _client() -> httpx.AsyncClient:
    server = HttpServer(HttpConfig(port=0))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    )


async def test_console_page_served_at_root():
    async with _client() as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Agent Bridge" in response.text


async def test_health_reports_ok_and_version():
    async with _client() as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_version_falls_back_when_package_not_installed(
    monkeypatch: pytest.MonkeyPatch,
):
    def _raise(name: str) -> str:
        raise console.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(console.metadata, "version", _raise)
    assert console._version() == "unknown"

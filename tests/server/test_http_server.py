"""HttpServer lifecycle against a real socket (port 0 → OS-assigned)."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi import APIRouter

from agent_bridge.server import http_server as http_server_module
from agent_bridge.server.config import HttpConfig
from agent_bridge.server.http_server import HttpServer


async def test_start_serves_routes_then_stops_cleanly():
    server = HttpServer(HttpConfig(port=0))
    await server.start()
    try:
        port = server.port
        assert port != 0
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        await server.stop()

    # The socket is released once stop() returns.
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/api/health")


async def test_include_router_mounts_component_routes():
    server = HttpServer(HttpConfig(port=0))
    router = APIRouter(prefix="/platforms/dummy")

    @router.get("/ping")
    async def ping() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"pong": "yes"}

    server.include_router(router)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/platforms/dummy/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": "yes"}


async def test_start_twice_raises():
    server = HttpServer(HttpConfig(port=0))
    await server.start()
    try:
        with pytest.raises(RuntimeError, match="called twice"):
            await server.start()
    finally:
        await server.stop()


async def test_bind_conflict_raises_with_address_in_message():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        server = HttpServer(HttpConfig(port=port))
        with pytest.raises(RuntimeError, match=f"could not bind 127.0.0.1:{port}"):
            await server.start()
    finally:
        blocker.close()


async def test_stop_without_start_is_a_noop():
    server = HttpServer(HttpConfig(port=0))
    await server.stop()


async def test_serve_task_dying_during_startup_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _explode(self: object, sockets: object = None) -> None:
        raise OSError("lifespan blew up")

    monkeypatch.setattr(http_server_module._NoSignalServer, "serve", _explode)
    server = HttpServer(HttpConfig(port=0))
    try:
        with pytest.raises(RuntimeError, match="failed to start") as excinfo:
            await server.start()
        assert isinstance(excinfo.value.__cause__, OSError)
    finally:
        await server.stop()


async def test_stop_logs_a_serve_task_error_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
):
    async def _fail() -> None:
        raise OSError("died mid-flight")

    server = HttpServer(HttpConfig(port=0))
    server._serve_task = asyncio.create_task(_fail())
    await asyncio.sleep(0)
    with caplog.at_level("ERROR"):
        await server.stop()
    assert any("exited with an error" in message for message in caplog.messages)


async def test_port_before_start_raises():
    server = HttpServer(HttpConfig(port=0))
    with pytest.raises(RuntimeError, match="not started"):
        _ = server.port

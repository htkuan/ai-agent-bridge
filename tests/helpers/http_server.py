from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

# A handler receives the parsed JSON body (or {} for empty/non-JSON bodies)
# and returns either a JSON-serializable response payload or a full
# aiohttp ``web.Response`` (for non-200 statuses).
type JsonHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class RecordedRequest:
    method: str
    path: str
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class FakeApiServer:
    """Minimal aiohttp server for faking HTTP APIs in integration tests.

    Register a ``JsonHandler`` per (method, path); every request is recorded
    in ``requests`` (in arrival order). ``start()`` binds an ephemeral
    localhost port and returns the base URL to point the component under
    test at. Unregistered paths return 404.
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], JsonHandler] = {}
        self.requests: list[RecordedRequest] = []
        self._runner: web.AppRunner | None = None
        self.base_url: str | None = None

    def route(self, method: str, path: str, handler: JsonHandler) -> None:
        self._handlers[(method.upper(), path)] = handler

    def requests_for(self, path: str) -> list[RecordedRequest]:
        return [req for req in self.requests if req.path == path]

    async def _dispatch(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_body": payload}
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                payload=payload,
                headers=dict(request.headers),
            )
        )
        handler = self._handlers.get((request.method, request.path))
        if handler is None:
            return web.json_response({"error": "no handler"}, status=404)
        result = await handler(payload)
        if isinstance(result, web.StreamResponse):
            return result
        return web.json_response(result)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

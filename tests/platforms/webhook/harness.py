"""Shared builder: a WebhookAdapter behind an in-process ASGI transport.

Both HTTP edges are faked at their outermost point and nothing else is:
inbound rides ``httpx.ASGITransport`` (no socket), outbound callbacks are
captured by an ``httpx.MockTransport``. The adapter, its router, its request
validation and its background-turn machinery are all real.

``tests/e2e/test_live_platforms.py`` is the same wiring with real sockets on
both edges — use this one for behaviour, that one for transport.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.protocols import MessageRouter
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.platforms.webhook.adapter import WebhookAdapter
from agent_bridge.platforms.webhook.config import WebhookConfig
from tests.fakes import FakeBridge

TOKEN = "test-webhook-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/platforms/webhook/v1/messages"
CALLBACK_URL = "http://callbacks.test/result"


@dataclass
class WebhookHarness:
    adapter: WebhookAdapter
    router: MessageRouter
    callbacks: list[httpx.Request]
    client: httpx.AsyncClient

    # --- the shared PlatformHarness shape ---

    async def deliver(self) -> None:
        """One POST, then wait out the background turn it started."""
        await self.post()
        await self.adapter.drain()

    def requests(self) -> list[BridgeRequest]:
        assert isinstance(self.router, FakeBridge)
        return self.router.calls

    def output(self) -> list[dict[str, object]]:
        """The callback payloads a caller would have received, oldest first."""
        return [json.loads(request.content) for request in self.callbacks]

    # --- richer, webhook-specific driving ---

    async def post(
        self, headers: dict[str, str] = AUTH, **overrides: object
    ) -> httpx.Response:
        body: dict[str, object] = {
            "conversation_id": "conv-1",
            "text": "hello",
            "callback_url": CALLBACK_URL,
            **overrides,
        }
        return await self.client.post(
            URL, json={k: v for k, v in body.items() if v is not None}, headers=headers
        )


@contextlib.asynccontextmanager
async def webhook_harness(
    router: MessageRouter | None = None,
    *,
    events: list[BridgeEvent] | None = None,
    capacity_full: bool = False,
    known_agents: frozenset[str] = frozenset(),
    raises: bool = False,
    config: WebhookConfig | None = None,
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
) -> AsyncGenerator[WebhookHarness]:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return respond(request) if respond is not None else httpx.Response(200)

    if router is None:
        router = FakeBridge(
            events,
            capacity_full=capacity_full,
            known_agents=known_agents,
            raises=raises,
        )
    adapter = WebhookAdapter(
        config or WebhookConfig(token=TOKEN, callback_retry_delays=()),
        router,
        callback_transport=httpx.MockTransport(handler),
    )
    app = FastAPI()
    app.include_router(adapter.router)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge.test"
    )
    await adapter.start()
    try:
        yield WebhookHarness(adapter, router, received, client)
    finally:
        await adapter.stop()
        await client.aclose()

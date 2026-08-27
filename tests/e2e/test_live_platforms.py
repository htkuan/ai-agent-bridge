"""Platforms over their real transport — no bridge, no agent, no tokens.

The mirror image of ``test_live_controllers.py``. That file drives each
agent's bare controller with nothing in front of it; this one drives each
platform's bare adapter with nothing behind it:

    real transport  →  real Adapter  →  FakeBridge (scripted events)

The router seam stays the same fake used one tier down, deliberately: if a
real ``Bridge`` sat there, a red test could mean the transport broke, the
session policy broke, or the agent broke. Holding it fixed means a failure
here has one possible cause — reality diverged from what the fakes claim.
That is the point of the tier: ``tests/contracts/`` pins *our* fakes to
*our* implementations, and nothing else pins a fake of somebody else's API.

**Webhook** is self-contained: its "external platform" is HTTP, so both
edges are hosted locally. Inbound is a real ``HttpServer`` (embedded
uvicorn, OS-assigned port) reached by a real ``httpx.AsyncClient`` over a
real socket; outbound is the adapter's *production* httpx client — no
``callback_transport`` injection — POSTing to a second real server. Nothing
in this file is mocked except the router.

Everything the behaviour tests cover is deliberately *not* repeated here
(see ``tests/platforms/webhook/``). These scenarios exist to prove the parts
``ASGITransport``/``MockTransport`` cannot: that the routes bind, the auth
header survives a real request, the 202-then-callback split works across two
processes' worth of sockets, and the retry loop drives a real connection.

Platforms whose transport needs third-party credentials (Slack) join later
and skip themselves when the credentials file is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Response

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.platforms.webhook.adapter import WebhookAdapter
from agent_bridge.platforms.webhook.config import WebhookConfig
from agent_bridge.server.config import HttpConfig
from agent_bridge.server.http_server import HttpServer
from tests.fakes import FakeBridge
from tests.support import wait_until

pytestmark = pytest.mark.live_platform

TOKEN = "live-platform-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/platforms/webhook/v1/messages"


@dataclass
class _CallbackReceiver:
    """A real HTTP endpoint the adapter delivers to, over a real socket.

    ``respond`` maps the attempt number (1-based) to the status to answer
    with, so a delivery can be made to fail the way a caller's endpoint
    would.
    """

    respond: Callable[[int], int] | None = None
    payloads: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/result")
        # Never called by name: the decorator registers it with the router.
        async def result(  # pyright: ignore[reportUnusedFunction]
            payload: dict[str, Any],
        ) -> Response:
            self.payloads.append(payload)
            status = 200 if self.respond is None else self.respond(len(self.payloads))
            return Response(status_code=status)

        return router


@dataclass
class LiveWebhook:
    adapter: WebhookAdapter
    bridge: FakeBridge
    client: httpx.AsyncClient
    receiver: _CallbackReceiver
    callback_url: str

    async def post(
        self, headers: dict[str, str] | None = None, **overrides: object
    ) -> httpx.Response:
        body: dict[str, object] = {
            "conversation_id": "conv-1",
            "text": "hello",
            "callback_url": self.callback_url,
            **overrides,
        }
        return await self.client.post(
            URL,
            json={k: v for k, v in body.items() if v is not None},
            headers=AUTH if headers is None else headers,
        )

    async def deliver(self) -> None:
        await self.post()
        await self.adapter.drain()

    def output(self) -> list[dict[str, Any]]:
        return self.receiver.payloads


@contextlib.asynccontextmanager
async def live_webhook(
    *,
    events: list[BridgeEvent] | None = None,
    gate: asyncio.Event | None = None,
    config: WebhookConfig | None = None,
    respond: Callable[[int], int] | None = None,
) -> AsyncGenerator[LiveWebhook]:
    bridge = FakeBridge(events, gate=gate)
    # No callback_transport: the adapter builds its production httpx client.
    adapter = WebhookAdapter(
        config or WebhookConfig(token=TOKEN, callback_retry_delays=()), bridge
    )

    inbound = HttpServer(HttpConfig(port=0))
    inbound.include_router(adapter.router)
    receiver = _CallbackReceiver(respond)
    outbound = HttpServer(HttpConfig(port=0))
    outbound.include_router(receiver.build_router())

    await adapter.start()
    await inbound.start()
    await outbound.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{inbound.port}")
    try:
        yield LiveWebhook(
            adapter=adapter,
            bridge=bridge,
            client=client,
            receiver=receiver,
            callback_url=f"http://127.0.0.1:{outbound.port}/result",
        )
    finally:
        await client.aclose()
        await adapter.stop()
        await inbound.stop()
        await outbound.stop()


async def test_webhook_turn_crosses_two_real_sockets():
    """202 in over one socket, the completion out over another."""
    async with live_webhook() as live:
        response = await live.post()
        assert response.status_code == 202
        assert response.json()["conversation_id"] == "conv-1"
        await live.adapter.drain()

        assert live.output() == [
            {
                "conversation_id": "conv-1",
                "text": "ok",
                "is_error": False,
                "cost_usd": 0.0,
                "duration_ms": 0,
            }
        ]
        # The turn really went through the adapter's pre-processing.
        assert live.bridge.calls[0].session_key == "webhook:default:conv-1"


async def test_webhook_rejects_a_bad_token_on_a_real_request():
    """The bearer check runs against a header that survived the wire, not one
    handed straight to the endpoint function."""
    async with live_webhook() as live:
        response = await live.post(headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert live.bridge.calls == []

        missing = await live.client.post(
            URL, json={"conversation_id": "c", "text": "x"}
        )
        assert missing.status_code == 401


async def test_webhook_validation_rejects_a_bad_body_on_the_wire():
    async with live_webhook() as live:
        response = await live.post(conversation_id="bad id!")
        assert response.status_code == 422
        assert live.bridge.calls == []


async def test_webhook_serialises_one_conversation_across_real_requests():
    """The in-flight guard holds when the two POSTs are genuinely concurrent
    requests on a live server rather than sequential ASGI calls."""
    gate = asyncio.Event()
    async with live_webhook(gate=gate) as live:
        first = await live.post()
        assert first.status_code == 202
        await wait_until(lambda: len(live.bridge.calls) == 1)

        conflict = await live.post()
        assert conflict.status_code == 409

        other = await live.post(conversation_id="conv-2")
        assert other.status_code == 202

        gate.set()
        await live.adapter.drain()
        assert len(live.output()) == 2


async def test_webhook_callback_retries_a_real_failed_delivery():
    """A 500 from a real endpoint drives the production httpx client's retry
    loop — the path MockTransport can only approximate."""
    config = WebhookConfig(token=TOKEN, callback_retry_delays=(0.0,))
    async with live_webhook(
        config=config, respond=lambda n: 500 if n == 1 else 200
    ) as live:
        await live.deliver()
        # Both attempts really reached the receiver; the second one stuck.
        assert len(live.output()) == 2
        assert live.output()[0] == live.output()[1]


async def test_webhook_survives_a_callback_endpoint_that_never_accepts(
    caplog: pytest.LogCaptureFixture,
):
    config = WebhookConfig(token=TOKEN, callback_retry_delays=(0.0,))
    async with live_webhook(config=config, respond=lambda _n: 500) as live:
        with caplog.at_level("ERROR"):
            await live.deliver()
        assert len(live.output()) == 2
    assert any("failed after 2 attempts" in message for message in caplog.messages)

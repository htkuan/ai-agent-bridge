"""WebhookAdapter: auth, 202 + background turn, callback delivery, state."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.bridge.protocols import MessageRouter
from agent_bridge.platforms.webhook.adapter import WebhookAdapter
from agent_bridge.platforms.webhook.config import WebhookConfig
from tests.fakes import FakeBridge

TOKEN = "test-webhook-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/platforms/webhook/v1/messages"
CALLBACK_URL = "http://callbacks.test/result"


@dataclass
class Harness:
    adapter: WebhookAdapter
    callbacks: list[httpx.Request]
    client: httpx.AsyncClient

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

    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.callbacks]


@contextlib.asynccontextmanager
async def webhook_harness(
    bridge: MessageRouter | None = None,
    *,
    config: WebhookConfig | None = None,
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
) -> AsyncIterator[Harness]:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return respond(request) if respond is not None else httpx.Response(200)

    adapter = WebhookAdapter(
        config or WebhookConfig(token=TOKEN, callback_retry_delays=()),
        bridge if bridge is not None else FakeBridge(),
        callback_transport=httpx.MockTransport(handler),
    )
    app = FastAPI()
    app.include_router(adapter.router)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge.test"
    )
    await adapter.start()
    try:
        yield Harness(adapter, received, client)
    finally:
        await adapter.stop()
        await client.aclose()


async def _wait_until(
    predicate: Callable[[], bool],
    # Deadline for a sync-predicate poll loop; asyncio.timeout can't help here.
    timeout: float = 5.0,  # noqa: ASYNC109
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met within timeout")
        await asyncio.sleep(0.01)


class _BlockingBridge:
    """Router whose turn parks until released — for in-flight assertions."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0

    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        self.started += 1
        yield Processing()
        await self.release.wait()
        yield Completion(text="done")


class _RaisingBridge:
    async def handle_message(
        self,
        session_key: str,
        text: str,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
        resumable: bool = True,
    ) -> AsyncIterator[BridgeEvent]:
        yield Processing()
        raise RuntimeError("controller exploded")


# --- auth ---


async def test_missing_auth_rejected():
    async with webhook_harness(bridge := FakeBridge()) as h:
        response = await h.client.post(URL, json={"conversation_id": "c", "text": "hi"})
    assert response.status_code == 401
    assert bridge.calls == []


async def test_wrong_token_rejected():
    async with webhook_harness(bridge := FakeBridge()) as h:
        response = await h.post(headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert bridge.calls == []


# --- the happy path: 202, background turn, callback ---


async def test_accepted_turn_delivers_completion_to_callback():
    async with webhook_harness(bridge := FakeBridge()) as h:
        response = await h.post()
        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "conversation_id": "conv-1",
            "resumable": True,
        }
        await h.adapter.drain()

        assert [str(r.url) for r in h.callbacks] == [CALLBACK_URL]
        assert h.payloads() == [
            {
                "conversation_id": "conv-1",
                "text": "ok",
                "is_error": False,
                "cost_usd": 0.0,
                "duration_ms": 0,
            }
        ]

    call = bridge.calls[0]
    assert call.session_key == "webhook:default:conv-1"
    assert call.text == "hello"
    assert call.resumable is True
    assert call.context == {"source": "webhook", "conversation_id": "conv-1"}
    assert call.system_prompt is not None
    assert "webhook" in call.system_prompt


async def test_sender_is_pretagged_into_text():
    async with webhook_harness(bridge := FakeBridge()) as h:
        await h.post(sender="kuan")
        await h.adapter.drain()
    assert bridge.calls[0].text == "[kuan]: hello"
    assert bridge.calls[0].context == {
        "source": "webhook",
        "conversation_id": "conv-1",
        "sender": "kuan",
    }


async def test_resumable_false_is_forwarded():
    async with webhook_harness(bridge := FakeBridge()) as h:
        response = await h.post(resumable=False)
        assert response.json()["resumable"] is False
        await h.adapter.drain()
    assert bridge.calls[0].resumable is False


async def test_no_callback_url_is_fire_and_forget():
    async with webhook_harness(bridge := FakeBridge()) as h:
        response = await h.post(callback_url=None)
        assert response.status_code == 202
        await h.adapter.drain()
        assert h.callbacks == []
    assert len(bridge.calls) == 1


# --- request validation ---


async def test_invalid_conversation_id_rejected():
    async with webhook_harness() as h:
        response = await h.post(conversation_id="bad id!")
    assert response.status_code == 422


async def test_empty_text_rejected():
    async with webhook_harness() as h:
        response = await h.post(text="")
    assert response.status_code == 422


# --- per-conversation serialization ---


async def test_second_turn_for_same_conversation_conflicts():
    blocking = _BlockingBridge()
    async with webhook_harness(blocking) as h:
        first = await h.post()
        assert first.status_code == 202
        await _wait_until(lambda: blocking.started == 1)

        conflict = await h.post()
        assert conflict.status_code == 409
        assert "conv-1" in conflict.json()["detail"]

        other = await h.post(conversation_id="conv-2")
        assert other.status_code == 202

        blocking.release.set()
        await h.adapter.drain()
        assert len(h.payloads()) == 2


async def test_conversation_is_free_again_after_turn_completes():
    async with webhook_harness() as h:
        await h.post()
        await h.adapter.drain()
        again = await h.post()
        assert again.status_code == 202
        await h.adapter.drain()
        assert len(h.payloads()) == 2


async def test_stop_cancels_inflight_turn_and_frees_conversation():
    blocking = _BlockingBridge()
    async with webhook_harness(blocking) as h:
        await h.post()
        await _wait_until(lambda: blocking.started == 1)
    # Harness exit ran adapter.stop(): the turn was cancelled, no callback
    # went out, and the conversation was not left wedged in `running`.
    assert h.callbacks == []
    assert h.adapter._conversations["conv-1"].running is False


# --- error envelopes on the callback ---


async def test_capacity_rejection_is_reported_via_callback():
    async with webhook_harness(FakeBridge(capacity_full=True)) as h:
        await h.post()
        await h.adapter.drain()
        payload = h.payloads()[0]
    assert payload["is_error"] is True
    assert payload["error_code"] == "capacity_full"


async def test_stream_without_completion_reports_no_completion():
    events: list[BridgeEvent] = [
        Processing(),
        TextDelta(text="partial"),
        StatusUpdate(status="tool", detail="Bash"),
    ]
    async with webhook_harness(FakeBridge(events)) as h:
        await h.post()
        await h.adapter.drain()
        payload = h.payloads()[0]
    assert payload["is_error"] is True
    assert payload["error_code"] == "no_completion"


async def test_bridge_error_reports_internal_error_and_recovers():
    async with webhook_harness(_RaisingBridge()) as h:
        await h.post()
        await h.adapter.drain()
        payload = h.payloads()[0]
        assert payload["is_error"] is True
        assert payload["error_code"] == "internal_error"

        # The failure must not wedge the conversation.
        again = await h.post()
        assert again.status_code == 202
        await h.adapter.drain()


async def test_agent_questions_are_logged_loudly(caplog: pytest.LogCaptureFixture):
    events: list[BridgeEvent] = [
        Processing(),
        UserQuestion(questions=[{"question": "Which env?"}]),
        Completion(text="done"),
    ]
    async with webhook_harness(FakeBridge(events)) as h:
        with caplog.at_level("WARNING"):
            await h.post()
            await h.adapter.drain()
    assert any("no one can answer" in message for message in caplog.messages)


# --- callback delivery retries ---


def _fail_first(times: int) -> Callable[[httpx.Request], httpx.Response]:
    remaining = [times]

    def respond(request: httpx.Request) -> httpx.Response:
        if remaining[0] > 0:
            remaining[0] -= 1
            return httpx.Response(500)
        return httpx.Response(200)

    return respond


async def test_callback_retries_until_delivered():
    config = WebhookConfig(token=TOKEN, callback_retry_delays=(0.0,))
    async with webhook_harness(config=config, respond=_fail_first(1)) as h:
        await h.post()
        await h.adapter.drain()
    assert len(h.callbacks) == 2  # first attempt 500, retry 200


async def test_retry_waits_its_configured_delay():
    config = WebhookConfig(token=TOKEN, callback_retry_delays=(0.001,))
    async with webhook_harness(config=config, respond=_fail_first(1)) as h:
        await h.post()
        await h.adapter.drain()
    assert len(h.callbacks) == 2


async def test_callback_gives_up_after_all_retries(caplog: pytest.LogCaptureFixture):
    config = WebhookConfig(token=TOKEN, callback_retry_delays=(0.0,))
    async with webhook_harness(config=config, respond=_fail_first(99)) as h:
        with caplog.at_level("ERROR"):
            await h.post()
            await h.adapter.drain()
    assert len(h.callbacks) == 2
    assert any("failed after 2 attempts" in message for message in caplog.messages)


async def test_callback_without_started_client_is_dropped(
    caplog: pytest.LogCaptureFixture,
):
    adapter = WebhookAdapter(WebhookConfig(token=TOKEN), FakeBridge())
    with caplog.at_level("ERROR"):
        await adapter._deliver_callback(CALLBACK_URL, {"is_error": False})
    assert any("not started" in message for message in caplog.messages)


# --- housekeeping ---


async def test_cleanup_purges_idle_conversations():
    config = WebhookConfig(
        token=TOKEN, callback_retry_delays=(), idle_state_seconds=0.001
    )
    async with webhook_harness(config=config) as h:
        await h.post()
        await h.adapter.drain()
        await asyncio.sleep(0.01)
        assert await h.adapter.cleanup() == 1
        assert await h.adapter.cleanup() == 0


async def test_cleanup_keeps_running_conversations():
    blocking = _BlockingBridge()
    config = WebhookConfig(
        token=TOKEN, callback_retry_delays=(), idle_state_seconds=0.001
    )
    async with webhook_harness(blocking, config=config) as h:
        await h.post()
        await _wait_until(lambda: blocking.started == 1)
        await asyncio.sleep(0.01)
        assert await h.adapter.cleanup() == 0
        blocking.release.set()
        await h.adapter.drain()

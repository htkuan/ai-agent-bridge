"""Real HTTP POSTs against a real LineAdapter webhook server → real Bridge →
FakeAgentController, with a fake Messaging API server capturing the outbound
reply/push calls (signature verification included)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import aiohttp
import pytest
from aiohttp import web

from agent_bridge.bridge import Bridge
from agent_bridge.events import Completion, TextDelta
from agent_bridge.platforms.line.adapter import LineAdapter
from agent_bridge.platforms.line.config import LineConfig
from tests.helpers import FakeAgentController, FakeApiServer

pytestmark = pytest.mark.integration

CHANNEL_SECRET = "test-channel-secret"
ACCESS_TOKEN = "test-access-token"
REPLY_PATH = "/v2/bot/message/reply"
PUSH_PATH = "/v2/bot/message/push"


def _sign(body: bytes, secret: str = CHANNEL_SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _text_event(text: str, *, reply_token: str = "rt-1") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": "U123"},
        "message": {"id": "m1", "type": "text", "text": text},
    }


def _line_api_server(*, reply_status: int = 200) -> FakeApiServer:
    server = FakeApiServer()

    async def reply(_payload: dict):
        if reply_status != 200:
            return web.json_response({"message": "Invalid reply token"}, status=reply_status)
        return {}

    async def push(_payload: dict):
        return {}

    server.route("POST", REPLY_PATH, reply)
    server.route("POST", PUSH_PATH, push)
    return server


async def _wait_for(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.02)


@pytest.fixture
async def line_stack(session_manager):
    """Factory: fake Messaging API + real webhook adapter + real bridge."""
    started: list = []

    async def _start(*, reply_status: int = 200, agent_events=None):
        api = _line_api_server(reply_status=reply_status)
        api_base = await api.start()
        controller = FakeAgentController(events=agent_events)
        bridge = Bridge(session_manager, controller, max_concurrent=2)
        config = LineConfig(
            channel_secret=CHANNEL_SECRET,
            channel_access_token=ACCESS_TOKEN,
            webhook_host="127.0.0.1",
            webhook_port=0,  # ephemeral — read back via adapter.bound_port
            api_base_url=api_base,
        )
        adapter = LineAdapter(config, bridge, session_manager=session_manager)
        await adapter.start()
        started.append((adapter, api))
        return adapter, api, controller

    yield _start

    for adapter, api in started:
        await adapter.stop()
        await api.stop()


async def _post_webhook(
    adapter: LineAdapter, events: list[dict], *, signature: str | None = None
) -> int:
    body = json.dumps({"destination": "Uxxxbot", "events": events}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Line-Signature": signature if signature is not None else _sign(body),
    }
    url = f"http://127.0.0.1:{adapter.bound_port}/line/webhook"
    async with aiohttp.ClientSession() as client:
        async with client.post(url, data=body, headers=headers) as resp:
            return resp.status


async def test_valid_signature_full_cycle(line_stack, session_manager):
    adapter, api, controller = await line_stack(
        agent_events=[TextDelta(text="hi there"), Completion(text="hi there")],
    )

    status = await _post_webhook(adapter, [_text_event("hello")])
    # Fast ack: 200 immediately, processing happens in the background.
    assert status == 200

    await _wait_for(lambda: api.requests_for(REPLY_PATH))

    # Reply API got the final text on the event's reply token, authorized
    # with the channel access token.
    reply = api.requests_for(REPLY_PATH)[0]
    assert reply.payload["replyToken"] == "rt-1"
    assert reply.payload["messages"] == [{"type": "text", "text": "hi there"}]
    assert reply.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert api.requests_for(PUSH_PATH) == []

    # The agent saw the tagged prompt and opaque line context.
    assert controller.calls == ["[U123]: hello"]
    assert controller.last_context == {
        "platform": "line",
        "source_type": "user",
        "chat_id": "U123",
        "user_id": "U123",
    }
    assert "LINE" in controller.last_system_prompt

    # Session persisted under the documented key.
    assert session_manager.get("line:user:U123") is not None


async def test_invalid_signature_rejected_without_processing(line_stack, session_manager):
    adapter, api, controller = await line_stack()

    status = await _post_webhook(adapter, [_text_event("hello")], signature="bad-signature")
    assert status == 403

    # Nothing reached the bridge or the Messaging API.
    await asyncio.sleep(0.05)
    assert controller.runs == []
    assert api.requests == []
    assert session_manager.get("line:user:U123") is None


async def test_reply_failure_falls_back_to_push(line_stack):
    adapter, api, controller = await line_stack(
        reply_status=400,
        agent_events=[Completion(text="pushed answer")],
    )

    status = await _post_webhook(adapter, [_text_event("hello")])
    assert status == 200

    await _wait_for(lambda: api.requests_for(PUSH_PATH))

    # Reply was attempted first (and got the 400), then push delivered.
    assert len(api.requests_for(REPLY_PATH)) == 1
    push = api.requests_for(PUSH_PATH)[0]
    assert push.payload["to"] == "U123"
    assert push.payload["messages"] == [{"type": "text", "text": "pushed answer"}]
    assert controller.calls == ["[U123]: hello"]

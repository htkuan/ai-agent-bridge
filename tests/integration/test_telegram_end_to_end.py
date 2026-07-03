"""Fake Telegram Bot API server → real TelegramAdapter long-poll → real Bridge
→ FakeAgentController, asserting the outbound Bot API calls and offset state."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_bridge.bridge import Bridge
from agent_bridge.events import Completion, TextDelta
from agent_bridge.platforms.telegram.adapter import TelegramAdapter
from agent_bridge.platforms.telegram.config import TelegramConfig
from tests.helpers import FakeAgentController, FakeApiServer

pytestmark = pytest.mark.integration

BOT_TOKEN = "123:abc"
API_PREFIX = f"/bot{BOT_TOKEN}"


def _private_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "text": text,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7, "first_name": "Alice", "is_bot": False},
        },
    }


def _group_update(update_id: int, text: str, message_id: int = 20) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "text": text,
            "chat": {"id": -100123, "type": "supergroup", "title": "Ops"},
            "from": {"id": 8, "first_name": "Bob", "is_bot": False},
        },
    }


def _telegram_server(update_batches: list[list[dict]]) -> FakeApiServer:
    """Fake Bot API: serves each update batch once, then empty batches."""
    server = FakeApiServer()
    batches = list(update_batches)
    next_message_id = {"value": 100}

    async def get_me(_payload: dict) -> Any:
        return {
            "ok": True,
            "result": {"id": 999, "is_bot": True, "username": "bridge_bot"},
        }

    async def get_updates(payload: dict) -> Any:
        if batches:
            return {"ok": True, "result": batches.pop(0)}
        # Honour long-poll semantics just enough to avoid a hot loop.
        await asyncio.sleep(min(float(payload.get("timeout", 0)), 0.05))
        return {"ok": True, "result": []}

    async def send_message(_payload: dict) -> Any:
        next_message_id["value"] += 1
        return {"ok": True, "result": {"message_id": next_message_id["value"]}}

    async def edit_message(payload: dict) -> Any:
        return {"ok": True, "result": {"message_id": payload.get("message_id")}}

    server.route("POST", f"{API_PREFIX}/getMe", get_me)
    server.route("POST", f"{API_PREFIX}/getUpdates", get_updates)
    server.route("POST", f"{API_PREFIX}/sendMessage", send_message)
    server.route("POST", f"{API_PREFIX}/editMessageText", edit_message)
    return server


async def _wait_for(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.02)


@pytest.fixture
async def run_cycle(tmp_path, session_manager):
    """Start fake server + adapter, wait until the reply lands, yield everything."""
    started: list = []

    async def _run(update_batches: list[list[dict]], agent_events=None):
        server = _telegram_server(update_batches)
        base_url = await server.start()
        controller = FakeAgentController(events=agent_events)
        bridge = Bridge(session_manager, controller, max_concurrent=2)
        config = TelegramConfig(
            bot_token=BOT_TOKEN,
            poll_timeout_seconds=1,
            state_path=tmp_path / "telegram.json",
            api_base_url=base_url,
        )
        adapter = TelegramAdapter(config, bridge, session_manager=session_manager)
        await adapter.start()
        started.append((adapter, server))
        await _wait_for(lambda: server.requests_for(f"{API_PREFIX}/editMessageText"))
        return adapter, server, controller, config

    yield _run

    for adapter, server in started:
        await adapter.stop()
        await server.stop()


async def test_private_message_full_cycle(run_cycle, session_manager, tmp_path):
    _, server, controller, _ = await run_cycle(
        [[_private_update(500, "hello")]],
        agent_events=[TextDelta(text="hi there"), Completion(text="hi there")],
    )

    # Placeholder posted as a reply to the source message.
    sends = server.requests_for(f"{API_PREFIX}/sendMessage")
    assert sends[0].payload["chat_id"] == 42
    assert sends[0].payload["reply_to_message_id"] == 10
    assert "Processing" in sends[0].payload["text"]

    # Placeholder edited into the final agent reply.
    edits = server.requests_for(f"{API_PREFIX}/editMessageText")
    assert edits[-1].payload["text"] == "hi there"
    assert edits[-1].payload["message_id"] == 101  # first fake-server message id

    # The agent saw the tagged prompt and opaque telegram context.
    assert controller.calls == ["[Alice (7)]: hello"]
    assert controller.last_context["platform"] == "telegram"
    assert controller.last_context["chat_id"] == "42"
    assert "Telegram" in controller.last_system_prompt

    # Session persisted under the documented key.
    assert session_manager.get("telegram:42:0") is not None

    # Offset persisted to the state file after the batch.
    state = json.loads((tmp_path / "telegram.json").read_text())
    assert state == {"last_update_id": 500}

    # Subsequent polls resume from the next update.
    await _wait_for(
        lambda: any(
            req.payload.get("offset") == 501
            for req in server.requests_for(f"{API_PREFIX}/getUpdates")
        )
    )


async def test_group_messages_require_mention(run_cycle):
    _, server, controller, _ = await run_cycle(
        [
            [
                _group_update(600, "just chatting", message_id=20),
                _group_update(601, "@bridge_bot deploy please", message_id=21),
            ]
        ],
    )

    # Only the mentioned message reached the agent, mention stripped.
    assert controller.calls == ["[Bob (8)]: deploy please"]

    # The single placeholder replies to the mentioned message, not the first.
    sends = server.requests_for(f"{API_PREFIX}/sendMessage")
    assert len(sends) == 1
    assert sends[0].payload["reply_to_message_id"] == 21
    assert sends[0].payload["chat_id"] == -100123


async def test_restart_resumes_from_persisted_offset(tmp_path, session_manager):
    state_path = tmp_path / "telegram.json"
    state_path.write_text(json.dumps({"last_update_id": 700}))

    server = _telegram_server([])
    base_url = await server.start()
    bridge = Bridge(session_manager, FakeAgentController(), max_concurrent=2)
    config = TelegramConfig(
        bot_token=BOT_TOKEN,
        poll_timeout_seconds=1,
        state_path=state_path,
        api_base_url=base_url,
    )
    adapter = TelegramAdapter(config, bridge, session_manager=session_manager)
    await adapter.start()
    try:
        await _wait_for(lambda: server.requests_for(f"{API_PREFIX}/getUpdates"))
        first_poll = server.requests_for(f"{API_PREFIX}/getUpdates")[0]
        assert first_poll.payload["offset"] == 701
        assert first_poll.payload["allowed_updates"] == ["message"]
    finally:
        await adapter.stop()
        await server.stop()

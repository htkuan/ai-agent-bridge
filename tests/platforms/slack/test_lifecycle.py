"""Adapter lifecycle: construction, start (socket + identity + notify), stop."""

from __future__ import annotations

import pytest
from slack_bolt.async_app import AsyncApp

import agent_bridge.platforms.slack.adapter as slack_adapter
from agent_bridge.platforms.slack.adapter import SlackAdapter
from agent_bridge.platforms.slack.config import SlackConfig
from tests.fakes import FakeBridge
from tests.platforms.slack.harness import SlackHarness, build_harness


class _FakeSocketModeHandler:
    """Stands in for bolt's AsyncSocketModeHandler — records connect/close."""

    def __init__(self, app: object, app_token: str) -> None:
        self.app = app
        self.app_token = app_token
        self.connected = False
        self.closed = False

    async def connect_async(self) -> None:
        self.connected = True

    async def close_async(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_socket_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_adapter, "AsyncSocketModeHandler", _FakeSocketModeHandler)


def _notify_harness() -> SlackHarness:
    return build_harness(
        config=SlackConfig(
            bot_token="xoxb-x",
            app_token="xapp-x",
            startup_notify_channel="C9",
            startup_notify_message="bridge is up",
        )
    )


def test_init_builds_real_bolt_app():
    adapter = SlackAdapter(
        SlackConfig(bot_token="xoxb-x", app_token="xapp-x"), FakeBridge()
    )
    assert isinstance(adapter._app, AsyncApp)
    assert adapter._sessions == {}
    assert adapter._handler is None


async def test_start_connects_and_resolves_bot_identity():
    harness = build_harness()

    await harness.adapter.start()

    handler = harness.adapter._handler
    assert isinstance(handler, _FakeSocketModeHandler)
    assert handler.connected is True
    assert handler.app_token == "xapp-x"
    assert harness.adapter._bot_user_id == "UBOT"


async def test_start_survives_auth_failure():
    harness = build_harness()
    harness.client.fail_next["auth_test"] = "not_authed"

    await harness.adapter.start()

    assert harness.adapter._bot_user_id is None


async def test_start_sends_startup_notification():
    harness = _notify_harness()

    await harness.adapter.start()

    posts = harness.client.calls_to("chat_postMessage")
    assert len(posts) == 1
    assert posts[0].kwargs == {"channel": "C9", "text": "bridge is up"}


async def test_start_survives_notification_failure():
    harness = _notify_harness()
    harness.client.fail_next["chat_postMessage"] = "channel_not_found"

    await harness.adapter.start()  # must not raise

    assert harness.client.messages == {}


async def test_start_without_notify_config_posts_nothing():
    harness = build_harness()

    await harness.adapter.start()

    assert harness.client.calls_to("chat_postMessage") == []


async def test_stop_closes_handler():
    harness = build_harness()
    await harness.adapter.start()

    await harness.adapter.stop()

    handler = harness.adapter._handler
    assert isinstance(handler, _FakeSocketModeHandler)
    assert handler.closed is True


async def test_stop_before_start_is_noop():
    harness = build_harness()
    await harness.adapter.stop()  # must not raise

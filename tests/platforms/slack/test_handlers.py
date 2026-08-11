"""Bolt handler registration: which events reach _process_message."""

from __future__ import annotations

from tests.fakes import dm_event, mention_event
from tests.platforms.slack.harness import build_harness


def test_registers_mention_and_message_handlers():
    harness = build_harness()
    assert set(harness.app.handlers) == {"app_mention", "message"}


async def test_mention_event_reaches_bridge():
    harness = build_harness()
    await harness.app.handlers["app_mention"](
        event=mention_event(), say=harness.client.say_for("C123"), client=harness.client
    )
    assert len(harness.bridge.calls) == 1
    assert harness.bridge.calls[0].session_key == "slack:C123:1700000000.000100"


async def test_dm_event_reaches_bridge():
    harness = build_harness()
    await harness.app.handlers["message"](
        event=dm_event(), say=harness.client.say_for("D123"), client=harness.client
    )
    assert len(harness.bridge.calls) == 1
    assert harness.bridge.calls[0].session_key == "slack:D123:1700000000.000100"


async def test_non_dm_message_ignored():
    harness = build_harness()
    await harness.app.handlers["message"](
        event=dm_event(channel_type="channel"),
        say=harness.client.say_for("C123"),
        client=harness.client,
    )
    assert harness.bridge.calls == []


async def test_bot_message_ignored():
    harness = build_harness()
    await harness.app.handlers["message"](
        event=dm_event(bot_id="B1"),
        say=harness.client.say_for("D123"),
        client=harness.client,
    )
    assert harness.bridge.calls == []


async def test_message_with_subtype_ignored():
    harness = build_harness()
    await harness.app.handlers["message"](
        event=dm_event(subtype="message_changed"),
        say=harness.client.say_for("D123"),
        client=harness.client,
    )
    assert harness.bridge.calls == []

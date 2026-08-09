"""Self-tests for the Slack fakes: the behaviour PR-level tests rely on."""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from tests.fakes import FakeBoltApp, FakeSlackClient, dm_event, mention_event


async def test_post_mints_increasing_ts_and_tracks_message_state():
    client = FakeSlackClient()
    first = await client.chat_postMessage(channel="C1", text="one")
    second = await client.chat_postMessage(channel="C1", text="two")
    assert second["ts"] > first["ts"]
    assert client.messages[("C1", first["ts"])] == "one"

    await client.chat_update(channel="C1", ts=first["ts"], text="edited")
    assert client.messages[("C1", first["ts"])] == "edited"

    await client.chat_delete(channel="C1", ts=first["ts"])
    assert ("C1", first["ts"]) not in client.messages


async def test_fail_next_raises_slack_api_error_once():
    client = FakeSlackClient()
    client.fail_next["chat_update"] = "ratelimited"
    with pytest.raises(SlackApiError) as exc:
        await client.chat_update(channel="C1", ts="1.0", text="x")
    assert exc.value.response["error"] == "ratelimited"
    # Only the next call fails; afterwards the method recovers.
    resp = await client.chat_update(channel="C1", ts="1.0", text="x")
    assert resp["ok"] is True


async def test_info_endpoints_serve_configured_names():
    client = FakeSlackClient(
        workspace="wonka",
        channel_names={"C1": "general"},
        user_names={"U1": "alice"},
    )
    conv = await client.conversations_info(channel="C1")
    assert conv["channel"]["name"] == "general"
    team = await client.team_info()
    assert team["team"]["name"] == "wonka"
    user = await client.users_info(user="U1")
    assert user["user"]["profile"]["display_name"] == "alice"
    auth = await client.auth_test()
    assert auth["user_id"] == "UBOT"


async def test_say_for_binds_channel_and_records_call():
    client = FakeSlackClient()
    say = client.say_for("C9")
    resp = await say(text="hi", thread_ts="1.0")
    assert client.messages[("C9", resp["ts"])] == "hi"
    (call,) = client.calls_to("chat_postMessage")
    assert call.kwargs["channel"] == "C9"
    assert call.kwargs["thread_ts"] == "1.0"


def test_fake_bolt_app_captures_handlers_by_event_name():
    app = FakeBoltApp()

    @app.event("app_mention")
    async def handler(**_kwargs: object) -> None:
        return None

    assert app.handlers["app_mention"] is handler
    assert isinstance(app.client, FakeSlackClient)


def test_event_builders_produce_bolt_shaped_payloads():
    mention = mention_event("<@UBOT> do it", thread_ts="42.0")
    assert mention["text"] == "<@UBOT> do it"
    assert mention["thread_ts"] == "42.0"
    assert "channel_type" not in mention

    dm = dm_event("hi", bot_id="B1", subtype="bot_message")
    assert dm["channel_type"] == "im"
    assert dm["bot_id"] == "B1"
    assert dm["subtype"] == "bot_message"

    plain_dm = dm_event("hi")
    assert "bot_id" not in plain_dm
    assert "subtype" not in plain_dm

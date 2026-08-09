"""SlackInfoCache: name resolution, caching, and API-error fallbacks."""

from __future__ import annotations

from agent_bridge.platforms.slack.adapter import SlackInfoCache
from tests.fakes import FakeSlackClient


def _client() -> FakeSlackClient:
    return FakeSlackClient(
        workspace="acme",
        channel_names={"C1": "general"},
        user_names={"U1": "alice"},
    )


# --- resolve_channel ---


async def test_resolve_channel_fetches_once_then_caches():
    cache = SlackInfoCache()
    client = _client()
    assert await cache.resolve_channel("C1", client) == "general"
    assert await cache.resolve_channel("C1", client) == "general"
    assert len(client.calls_to("conversations_info")) == 1


async def test_resolve_channel_dm_without_name_falls_back_to_id():
    cache = SlackInfoCache()
    # "D9" is not in channel_names → the fake serves name=None, like a DM.
    assert await cache.resolve_channel("D9", _client()) == "D9"


async def test_resolve_channel_api_error_falls_back_to_id_and_caches():
    cache = SlackInfoCache()
    client = _client()
    client.fail_next["conversations_info"] = "channel_not_found"
    assert await cache.resolve_channel("C1", client) == "C1"
    # The fallback is cached: no retry on the next lookup.
    assert await cache.resolve_channel("C1", client) == "C1"
    assert len(client.calls_to("conversations_info")) == 1


# --- resolve ---


async def test_resolve_returns_names_and_caches():
    cache = SlackInfoCache()
    client = _client()
    assert await cache.resolve("C1", "U1", client) == ("acme", "general", "alice")

    before = len(client.calls)
    assert await cache.resolve("C1", "U1", client) == ("acme", "general", "alice")
    assert len(client.calls) == before


async def test_resolve_team_info_error_leaves_workspace_blank():
    cache = SlackInfoCache()
    client = _client()
    client.fail_next["team_info"] = "ratelimited"
    workspace, channel, user = await cache.resolve("C1", "U1", client)
    assert workspace == ""
    assert (channel, user) == ("general", "alice")
    # Workspace was not cached on failure → retried (and now cached).
    assert (await cache.resolve("C1", "U1", client))[0] == "acme"


async def test_resolve_users_info_error_falls_back_to_user_id():
    cache = SlackInfoCache()
    client = _client()
    client.fail_next["users_info"] = "user_not_found"
    assert (await cache.resolve("C1", "U1", client))[2] == "U1"
    # The fallback is cached: no retry on the next lookup.
    assert (await cache.resolve("C1", "U1", client))[2] == "U1"
    assert len(client.calls_to("users_info")) == 1


async def test_resolve_user_without_profile_names_falls_back_to_user_id():
    cache = SlackInfoCache()
    # "U9" is not in user_names → display_name and real_name are both None.
    assert (await cache.resolve("C1", "U9", _client()))[2] == "U9"

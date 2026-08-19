"""Shared builder: a SlackAdapter wired to the typed fakes.

Mirrors ``SlackAdapter.__init__`` but swaps the real bolt ``AsyncApp`` for
``FakeBoltApp`` (the real constructor builds an ``AsyncApp`` eagerly), so
tests can invoke captured handlers and inspect the visible Slack state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.slack.adapter import SlackAdapter, SlackInfoCache
from agent_bridge.platforms.slack.config import SlackConfig
from tests.fakes import FakeBoltApp, FakeBridge, FakeSlackClient


@dataclass
class SlackHarness:
    adapter: SlackAdapter
    app: FakeBoltApp
    client: FakeSlackClient
    bridge: FakeBridge


def build_harness(
    *,
    events: list[BridgeEvent] | None = None,
    capacity_full: bool = False,
    known_agents: frozenset[str] = frozenset(),
    config: SlackConfig | None = None,
    client: FakeSlackClient | None = None,
    session_manager: SessionManager | None = None,
) -> SlackHarness:
    client = client or FakeSlackClient(
        channel_names={"C123": "general"}, user_names={"U123": "alice"}
    )
    app = FakeBoltApp(client)
    bridge = FakeBridge(events, capacity_full=capacity_full, known_agents=known_agents)
    adapter: Any = SlackAdapter.__new__(SlackAdapter)
    adapter._config = config or SlackConfig(bot_token="xoxb-x", app_token="xapp-x")
    adapter._bridge = bridge
    adapter._session_manager = session_manager
    adapter._app = app
    adapter._handler = None
    adapter._sessions = {}
    adapter._name_cache = SlackInfoCache()
    adapter._bot_user_id = None
    adapter._register_handlers()
    return SlackHarness(adapter=adapter, app=app, client=client, bridge=bridge)

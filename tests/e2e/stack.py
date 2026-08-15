"""Full-stack test rig: real components, faked outermost boundaries.

Only the edges are doubled — the Slack Web API (``FakeSlackClient`` behind
``FakeBoltApp``, so no socket is opened) and the claude CLI (the scripted
subprocess from ``tests/fakes/claude_cli.py``). Everything in between —
``SlackAdapter``, ``Bridge``, ``SessionManager``, ``ClaudeController`` — is
real, so scenarios assert what a Slack user would actually see in
``client.messages`` / ``client.calls_to``.

``tests/e2e/live.py`` reuses the same wiring with the *real* claude CLI in
place of the scripted one — hence the split between ``SlackStack`` (agent
agnostic) and ``E2EStack`` (adds the scripted CLI handle).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge.config import RouterConfig, SessionConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.slack.adapter import SlackAdapter, SlackInfoCache
from agent_bridge.platforms.slack.config import SlackConfig
from tests.fakes import FakeBoltApp, FakeClaudeCLI, FakeSlackClient, mention_event
from tests.fakes.claude_cli import Step, install


async def wait_until(
    predicate: Callable[[], bool],
    # Deadline for a sync-predicate poll loop; asyncio.timeout can't help here.
    timeout: float = 5.0,  # noqa: ASYNC109
) -> None:
    """Poll until ``predicate()`` holds; fail the test on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met within timeout")
        await asyncio.sleep(0.01)


@dataclass
class SlackStack:
    """Slack adapter → Bridge → ClaudeController, with Slack itself faked.

    Agent agnostic: ``controller`` may be driving the scripted CLI
    (``build_stack``) or the real one (``tests/e2e/live.py``).
    """

    adapter: SlackAdapter
    app: FakeBoltApp
    client: FakeSlackClient
    bridge: Bridge
    session_manager: SessionManager
    controller: ClaudeController

    async def send(
        self,
        text: str,
        *,
        ts: str,
        thread_ts: str | None = None,
        channel: str = "C123",
        user: str = "U123",
    ) -> None:
        """Deliver an app_mention as bolt would; returns when handling ends."""
        await self.app.handlers["app_mention"](
            event=mention_event(
                text=f"<@UBOT> {text}",
                channel=channel,
                user=user,
                ts=ts,
                thread_ts=thread_ts,
            ),
            say=self.client.say_for(channel),
            client=self.client,
        )

    def replies(self) -> list[str]:
        """The message texts a Slack user is left looking at, oldest first."""
        return list(self.client.messages.values())


@dataclass
class E2EStack(SlackStack):
    cli: FakeClaudeCLI

    def swap_scenario(self, steps: list[Step]) -> None:
        """Replace the fake CLI's script — takes effect on its next spawn."""
        scenario = Path(self.cli.config.cli_path).parent / "scenario.json"
        scenario.write_text(
            json.dumps({"steps": steps, "record_args": str(self.cli.args_file)})
        )


class SlackWiring(NamedTuple):
    adapter: SlackAdapter
    app: FakeBoltApp
    client: FakeSlackClient
    bridge: Bridge


def session_manager_for(tmp_path: Path) -> SessionManager:
    return SessionManager(
        SessionConfig(store_path=tmp_path / "sessions.json", ttl_hours=1.0)
    )


def wire_slack(
    controller: ClaudeController,
    session_manager: SessionManager,
    *,
    max_concurrent: int = 5,
    dedupe: PromptDedupeCache | None = None,
    # Shrink the update throttle so a turn doesn't idle out the default 1.5s.
    update_throttle_seconds: float = 0.05,
) -> SlackWiring:
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=max_concurrent),
        session_manager,
        controller,
        dedupe=dedupe,
    )
    client = FakeSlackClient(
        channel_names={"C123": "general"}, user_names={"U123": "alice"}
    )
    app = FakeBoltApp(client)
    # Same manual wiring as tests/platforms/slack/harness.py: mirror
    # SlackAdapter.__init__ but swap the eager real AsyncApp for FakeBoltApp.
    adapter: Any = SlackAdapter.__new__(SlackAdapter)
    adapter._config = SlackConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        update_throttle_seconds=update_throttle_seconds,
    )
    adapter._bridge = bridge
    adapter._session_manager = session_manager
    adapter._app = app
    adapter._handler = None
    adapter._sessions = {}
    adapter._name_cache = SlackInfoCache()
    adapter._bot_user_id = "UBOT"
    adapter._register_handlers()
    return SlackWiring(adapter=adapter, app=app, client=client, bridge=bridge)


def build_stack(
    tmp_path: Path,
    steps: list[Step],
    *,
    max_concurrent: int = 5,
    dedupe: PromptDedupeCache | None = None,
    timeout_seconds: float = 600.0,
) -> E2EStack:
    cli = install(tmp_path / "fake-claude", steps, timeout_seconds=timeout_seconds)
    controller = ClaudeController(cli.config)
    session_manager = session_manager_for(tmp_path)
    wiring = wire_slack(
        controller, session_manager, max_concurrent=max_concurrent, dedupe=dedupe
    )
    return E2EStack(
        adapter=wiring.adapter,
        app=wiring.app,
        client=wiring.client,
        bridge=wiring.bridge,
        session_manager=session_manager,
        controller=controller,
        cli=cli,
    )

"""Full-stack test rig: real components, faked outermost boundaries.

Only the edges are doubled — the Slack Web API (``FakeSlackClient`` behind
``FakeBoltApp``, so no socket is opened) and the claude CLI (the scripted
subprocess from ``tests/fakes/claude_cli.py``). Everything in between —
``SlackAdapter``, ``Bridge``, ``SessionManager``, ``ClaudeController`` — is
real, so scenarios assert what a Slack user would actually see in
``client.messages`` / ``client.calls_to``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge import Bridge
from agent_bridge.dedupe import PromptDedupeCache
from agent_bridge.platforms.slack.adapter import SlackAdapter, SlackInfoCache
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.session import SessionManager
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
class E2EStack:
    adapter: SlackAdapter
    app: FakeBoltApp
    client: FakeSlackClient
    bridge: Bridge
    session_manager: SessionManager
    controller: ClaudeController
    cli: FakeClaudeCLI

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

    def swap_scenario(self, steps: list[Step]) -> None:
        """Replace the fake CLI's script — takes effect on its next spawn."""
        scenario = Path(self.cli.config.cli_path).parent / "scenario.json"
        scenario.write_text(
            json.dumps({"steps": steps, "record_args": str(self.cli.args_file)})
        )


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
    session_manager = SessionManager(
        store_path=tmp_path / "sessions.json", ttl_hours=1.0
    )
    bridge = Bridge(
        session_manager, controller, max_concurrent=max_concurrent, dedupe=dedupe
    )
    client = FakeSlackClient(
        channel_names={"C123": "general"}, user_names={"U123": "alice"}
    )
    app = FakeBoltApp(client)
    # Same manual wiring as tests/platforms/slack/harness.py: mirror
    # SlackAdapter.__init__ but swap the eager real AsyncApp for FakeBoltApp.
    adapter: Any = SlackAdapter.__new__(SlackAdapter)
    adapter._config = SlackConfig(bot_token="xoxb-x", app_token="xapp-x")
    adapter._bridge = bridge
    adapter._session_manager = session_manager
    adapter._app = app
    adapter._handler = None
    adapter._sessions = {}
    adapter._name_cache = SlackInfoCache()
    adapter._bot_user_id = "UBOT"
    adapter._register_handlers()
    return E2EStack(
        adapter=adapter,
        app=app,
        client=client,
        bridge=bridge,
        session_manager=session_manager,
        controller=controller,
        cli=cli,
    )

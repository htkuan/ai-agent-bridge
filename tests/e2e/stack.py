"""Full-stack test rigs: real components, faked outermost boundaries.

Only the edges are doubled. For the Slack rig that's the Slack Web API
(``FakeSlackClient`` behind ``FakeBoltApp``, so no socket is opened) and the
claude CLI (the scripted subprocess from ``tests/fakes/claude_cli.py``);
for the webhook rig it's both HTTP edges — the inbound POST rides an
in-process ASGI transport and the outbound callback is captured by an
``httpx.MockTransport``. Everything in between — adapter, ``Bridge``,
``SessionManager``, controller — is real, so scenarios assert what the
platform's consumer would actually see (``client.messages`` for Slack, the
callback payload for webhook).

``tests/e2e/conftest.py`` reuses the same wiring with the *real* agent CLIs
in place of the scripted one — hence the split between ``SlackStack`` /
``WebhookStack`` (agent agnostic) and ``E2EStack`` (adds the scripted CLI
handle).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from fastapi import FastAPI

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge.config import RouterConfig, SessionConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.protocols import AgentController
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.slack.adapter import SlackAdapter, SlackInfoCache
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.platforms.webhook.adapter import WebhookAdapter
from agent_bridge.platforms.webhook.config import WebhookConfig
from tests.fakes import FakeBoltApp, FakeClaudeCLI, FakeSlackClient, mention_event
from tests.fakes.claude_cli import Step, install
from tests.support import wait_until

__all__ = [
    "E2EStack",
    "SlackStack",
    "WebhookStack",
    "build_stack",
    "session_manager_for",
    "wait_until",
    "wire_slack",
    "wire_webhook",
]

WEBHOOK_URL = "/platforms/webhook/v1/messages"
WEBHOOK_TOKEN = "e2e-webhook-token"
WEBHOOK_CALLBACK_URL = "http://callbacks.test/result"


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


@dataclass
class WebhookStack:
    """Webhook adapter → Bridge → agent controller, with both HTTP edges faked.

    Agent agnostic like ``SlackStack``: ``bridge``'s default controller may
    drive the scripted CLI or a real one (``tests/e2e/conftest.py``).
    ``work_dir`` is that controller's sandbox, for asserting real tool use.
    """

    adapter: WebhookAdapter
    http: httpx.AsyncClient
    bridge: Bridge
    session_manager: SessionManager
    callbacks: list[httpx.Request]
    work_dir: Path

    async def send(
        self,
        text: str,
        *,
        conversation_id: str = "conv-1",
        sender: str | None = None,
        resumable: bool = True,
    ) -> dict[str, Any]:
        """POST one message, wait out the background turn, return the
        callback payload the caller would receive."""
        body: dict[str, object] = {
            "conversation_id": conversation_id,
            "text": text,
            "resumable": resumable,
            "callback_url": WEBHOOK_CALLBACK_URL,
        }
        if sender is not None:
            body["sender"] = sender
        response = await self.http.post(
            WEBHOOK_URL,
            json=body,
            headers={"Authorization": f"Bearer {WEBHOOK_TOKEN}"},
        )
        response.raise_for_status()  # the endpoint answers 202 before the turn
        await self.adapter.drain()
        return self.payloads()[-1]

    def payloads(self) -> list[dict[str, Any]]:
        """Every callback delivery so far, oldest first."""
        return [json.loads(request.content) for request in self.callbacks]


class WebhookWiring(NamedTuple):
    adapter: WebhookAdapter
    http: httpx.AsyncClient
    bridge: Bridge
    callbacks: list[httpx.Request]


def wire_webhook(
    controller: AgentController,
    session_manager: SessionManager,
    *,
    max_concurrent: int = 5,
) -> WebhookWiring:
    """Real ``WebhookAdapter`` + ``Bridge`` around ``controller``; the caller
    owns the lifecycle (``adapter.start()``/``stop()``, ``http.aclose()``)."""
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=max_concurrent),
        session_manager,
        controller,
    )
    callbacks: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        callbacks.append(request)
        return httpx.Response(200)

    adapter = WebhookAdapter(
        WebhookConfig(token=WEBHOOK_TOKEN, callback_retry_delays=()),
        bridge,
        callback_transport=httpx.MockTransport(record),
    )
    app = FastAPI()
    app.include_router(adapter.router)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge.test"
    )
    return WebhookWiring(adapter=adapter, http=http, bridge=bridge, callbacks=callbacks)


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

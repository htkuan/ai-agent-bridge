"""Webhook platform: a machine-to-machine HTTP POST endpoint.

Accepts a message with 202 immediately, runs the agent turn in the
background, and delivers the final result as a single JSON POST to the
caller's ``callback_url``. The caller defines session semantics by choosing
``conversation_id`` — reusing one resumes the same agent session (when
``resumable``), and concurrent turns for one conversation are rejected
with 409 rather than queued.

The adapter only contributes an ``APIRouter``; the socket belongs to the
shared ``HttpServer`` (``server/http_server.py``), so ``start``/``stop``
here manage just the callback HTTP client and in-flight turns.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated

try:
    import httpx
    from fastapi import APIRouter, Header, HTTPException
    from pydantic import BaseModel, Field, HttpUrl
except ImportError:
    raise ImportError(
        "HTTP dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[http]"
    ) from None

from agent_bridge.bridge.events import BridgeEvent, Completion, UserQuestion
from agent_bridge.bridge.protocols import MessageRouter
from agent_bridge.platforms.base import (
    BasePlatformAdapter,
    BridgeRequest,
    make_session_key,
)
from agent_bridge.platforms.webhook.config import WebhookConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "This request arrived through the agent-bridge webhook platform: a "
    "machine-to-machine HTTP API. There is no interactive user on the other "
    "end, so never ask follow-up questions — make reasonable assumptions and "
    "state them. Only your final reply is delivered, as a single JSON payload "
    "POSTed to the caller's callback URL. Callers may continue the "
    "conversation by sending another message with the same conversation id."
)


class WebhookMessage(BaseModel):
    """Body of ``POST /platforms/webhook/v1/messages``."""

    # Constrained so session keys stay canonical `{platform}:{scope}:{id}`.
    conversation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    text: str = Field(min_length=1)
    sender: str | None = Field(default=None, max_length=128)
    resumable: bool = True
    # Optional named agent profile; None = the bridge's default. An unknown
    # name isn't a 4xx — the turn is accepted and the callback carries the
    # bridge's error Completion (error_code "unknown_agent"). Reusing a
    # conversation_id under a different agent abandons the old session and
    # starts fresh.
    agent: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_-]+$")
    # Optional: no callback means fire-and-forget (result only logged).
    callback_url: HttpUrl | None = None


@dataclass
class _ConversationState:
    """Per-conversation guard: one turn in flight, plus an idle timestamp
    so cleanup() can drop long-unused entries."""

    running: bool = False
    last_used: float = field(default_factory=time.monotonic)


class WebhookAdapter(BasePlatformAdapter[str]):
    """Per-turn run state is just the session key — events are logged, not
    rendered; the callback delivers only the final ``Completion``."""

    def __init__(
        self,
        config: WebhookConfig,
        bridge: MessageRouter,
        callback_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(bridge)
        self._config = config
        # Tests inject an httpx.MockTransport to capture callback deliveries.
        self._callback_transport = callback_transport
        self._client: httpx.AsyncClient | None = None
        self._conversations: dict[str, _ConversationState] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._router = self._build_router()

    @property
    def router(self) -> APIRouter:
        """Mounted onto the shared HttpServer by ``app.py``."""
        return self._router

    # --- lifecycle ---

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self._config.callback_timeout_seconds,
            transport=self._callback_transport,
        )

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def drain(self) -> None:
        """Wait for in-flight turns to finish (stop() cancels them instead)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)  # let done-callbacks empty the set

    async def cleanup(self) -> int:
        now = time.monotonic()
        stale = [
            conversation_id
            for conversation_id, state in self._conversations.items()
            if not state.running
            and (now - state.last_used) >= self._config.idle_state_seconds
        ]
        for conversation_id in stale:
            del self._conversations[conversation_id]
        if stale:
            logger.info("Cleaned up %d idle webhook conversations", len(stale))
        return len(stale)

    # --- HTTP surface ---

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/platforms/webhook")

        @router.post("/v1/messages", status_code=202)
        # Never called by name: the decorator registers it with the router.
        async def post_message(  # pyright: ignore[reportUnusedFunction]
            message: WebhookMessage,
            authorization: Annotated[str | None, Header()] = None,
        ) -> dict[str, str | bool]:
            return self._accept(message, authorization)

        return router

    def _accept(
        self, message: WebhookMessage, authorization: str | None
    ) -> dict[str, str | bool]:
        self._check_auth(authorization)
        state = self._conversations.setdefault(
            message.conversation_id, _ConversationState()
        )
        if state.running:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A turn for conversation {message.conversation_id!r} "
                    "is already in flight"
                ),
            )
        # No await between the check above and this flip, so two concurrent
        # POSTs for one conversation can't both pass the gate.
        state.running = True
        state.last_used = time.monotonic()
        task = asyncio.create_task(self._run_turn(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {
            "status": "accepted",
            "conversation_id": message.conversation_id,
            "resumable": message.resumable,
        }

    def _check_auth(self, authorization: str | None) -> None:
        expected = f"Bearer {self._config.token}"
        if not secrets.compare_digest(
            (authorization or "").encode(), expected.encode()
        ):
            raise HTTPException(
                status_code=401, detail="Invalid or missing bearer token"
            )

    # --- the background turn ---

    async def _run_turn(self, message: WebhookMessage) -> None:
        session_key = make_session_key("webhook", "default", message.conversation_id)
        try:
            completion = await self.process(
                self._build_request(message, session_key), state=session_key
            )
            payload = _callback_payload(message.conversation_id, completion)
        except Exception:
            logger.exception(
                "Webhook turn failed for conversation %s", message.conversation_id
            )
            payload = _error_payload(
                message.conversation_id,
                "Internal error while processing the message.",
                "internal_error",
            )
        finally:
            # Also runs on cancellation (shutdown) — never leave a
            # conversation wedged in `running`.
            state = self._conversations.get(message.conversation_id)
            if state is not None:
                state.running = False
                state.last_used = time.monotonic()
        if message.callback_url is None:
            logger.info(
                "Webhook %s: no callback_url, result dropped (is_error=%s)",
                session_key,
                payload["is_error"],
            )
        else:
            await self._deliver_callback(str(message.callback_url), payload)

    def _build_request(
        self, message: WebhookMessage, session_key: str
    ) -> BridgeRequest:
        text = f"[{message.sender}]: {message.text}" if message.sender else message.text
        context = {"source": "webhook", "conversation_id": message.conversation_id}
        if message.sender:
            context["sender"] = message.sender
        if message.agent:
            context["agent"] = message.agent
        return BridgeRequest(
            session_key=session_key,
            text=text,
            context=context,
            system_prompt=_SYSTEM_PROMPT,
            resumable=message.resumable,
            agent=message.agent,
        )

    async def _deliver_callback(self, url: str, payload: dict[str, object]) -> None:
        if self._client is None:
            logger.error("Webhook callback dropped — adapter not started: %s", url)
            return
        # First attempt fires immediately; each retry waits its delay.
        delays = (0.0, *self._config.callback_retry_delays)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, json=payload)
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                logger.warning(
                    "Webhook callback attempt %d/%d to %s failed: %s",
                    attempt,
                    len(delays),
                    url,
                    exc,
                )
        logger.error(
            "Webhook callback to %s failed after %d attempts — result dropped",
            url,
            len(delays),
        )

    # --- event hooks: log only; the callback carries the final result ---

    async def on_user_question(self, state: str, event: UserQuestion) -> None:
        # No human is on the other end of a webhook — surface loudly.
        logger.warning(
            "Webhook %s: agent asked %d question(s) but no one can answer: %s",
            state,
            len(event.questions),
            event.questions,
        )

    async def on_event(self, state: str, event: BridgeEvent) -> None:
        logger.debug("Webhook %s: %s", state, type(event).__name__)


def _callback_payload(
    conversation_id: str, completion: Completion | None
) -> dict[str, object]:
    if completion is None:
        return _error_payload(
            conversation_id,
            "The agent stream ended without a completion.",
            "no_completion",
        )
    payload: dict[str, object] = {
        "conversation_id": conversation_id,
        "text": completion.text,
        "is_error": completion.is_error,
        "cost_usd": completion.cost_usd,
        "duration_ms": completion.duration_ms,
    }
    error_code = completion.metadata.get("error_code")
    if error_code:
        payload["error_code"] = error_code
    return payload


def _error_payload(
    conversation_id: str, text: str, error_code: str
) -> dict[str, object]:
    return {
        "conversation_id": conversation_id,
        "text": text,
        "is_error": True,
        "error_code": error_code,
    }

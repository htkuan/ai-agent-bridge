from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

try:
    from aiohttp import web
except ImportError:
    raise ImportError(
        "API dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[api]"
    ) from None

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    Usage,
    UserQuestion,
)
from agent_bridge.platforms.api.config import ApiConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

MESSAGES_PATH = "/v1/messages"
HEALTHZ_PATH = "/healthz"

# stop() lets aiohttp wait this long for in-flight requests before cancelling.
STOP_GRACE_SECONDS = 5.0

# Sent when the client provides no system_prompt: generic programmatic-trigger
# framing (the API has no chat UI and no sender identity).
DEFAULT_SYSTEM_PROMPT = (
    "This request arrived via the Agent Bridge HTTP API — a programmatic "
    "entry point, not a chat interface. The caller is a program or script "
    "receiving your output verbatim.\n"
    "Respond with the final answer as plain text; there is no human chat "
    "context beyond the prompt itself."
)


# --- Pure request/response logic (unit-testable without I/O) ---


class ApiRequestError(ValueError):
    """Invalid request body — rendered as a 400 with the message."""


@dataclass
class ApiRequest:
    text: str
    session: str | None = None
    system_prompt: str | None = None
    context: dict[str, str] | None = None
    stream: bool = False


def parse_request(payload: Any) -> ApiRequest:
    if not isinstance(payload, dict):
        raise ApiRequestError("request body must be a JSON object")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiRequestError("'text' is required and must be a non-empty string")

    session = payload.get("session")
    if session is not None and (not isinstance(session, str) or not session.strip()):
        raise ApiRequestError("'session' must be a non-empty string or null")

    system_prompt = payload.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ApiRequestError("'system_prompt' must be a string or null")

    context = payload.get("context")
    if context is not None:
        if not isinstance(context, dict) or not all(
            isinstance(value, str) for value in context.values()
        ):
            raise ApiRequestError(
                "'context' must be an object mapping strings to strings, or null"
            )

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise ApiRequestError("'stream' must be a boolean")

    return ApiRequest(
        text=text,
        session=session,
        system_prompt=system_prompt,
        context=context,
        stream=stream,
    )


def resolve_session(session: str | None) -> tuple[str, bool]:
    """Map the client's session field to (session_key, resumable)."""
    if session is not None:
        return f"api:client:{session}", True
    # One-shot: a fresh untracked session per call, nothing persisted.
    return f"api:oneshot:{uuid.uuid4()}", False


def merge_context(client_context: dict[str, str] | None) -> dict[str, str]:
    # The adapter's platform tag wins over a client-supplied one — the
    # breadcrumb must be trustworthy, a caller cannot masquerade as another
    # platform. All other client keys pass through untouched.
    return {**(client_context or {}), "platform": "api"}


def usage_to_dict(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    data = asdict(usage)
    data["total_tokens"] = usage.total_tokens
    return data


def status_line(event: StatusUpdate) -> str:
    return f"{event.status}: {event.detail}" if event.detail else event.status


def is_capacity_full(completion: Completion) -> bool:
    return completion.metadata.get("error_code") == "capacity_full"


def completion_body(
    session: str | None,
    completion: Completion,
    *,
    accumulated: str = "",
    status_updates: list[str] | None = None,
    questions: list[dict] | None = None,
) -> dict[str, Any]:
    """Buffered-mode response body. ``Completion.text`` is the authoritative
    final text (accumulated deltas are only a fallback when it is empty)."""
    body: dict[str, Any] = {
        "session": session,
        "text": completion.text or accumulated,
        "is_error": completion.is_error,
        "usage": usage_to_dict(completion.usage),
        "session_usage": usage_to_dict(completion.session_usage),
        "status_updates": status_updates or [],
    }
    if questions:
        body["questions"] = questions
    return body


_SSE_NAMES = {
    Processing: "processing",
    TextDelta: "text_delta",
    StatusUpdate: "status",
    UserQuestion: "question",
    Completion: "completion",
}


def sse_payload(event: BridgeEvent) -> dict[str, Any]:
    match event:
        case Processing():
            return {}
        case TextDelta(text=text):
            return {"text": text}
        case StatusUpdate(status=status, detail=detail):
            return {"status": status, "detail": detail}
        case UserQuestion(questions=questions):
            return {"questions": questions}
        case Completion() as completion:
            return {
                "text": completion.text,
                "is_error": completion.is_error,
                "usage": usage_to_dict(completion.usage),
                "session_usage": usage_to_dict(completion.session_usage),
            }
    raise ValueError(f"Unknown bridge event: {event!r}")


def format_sse(event: BridgeEvent) -> str:
    # json.dumps never emits raw newlines, so a single data: line is safe.
    data = json.dumps(sse_payload(event), ensure_ascii=False)
    return f"event: {_SSE_NAMES[type(event)]}\ndata: {data}\n\n"


# --- Adapter ---


class ApiAdapter:
    def __init__(
        self,
        config: ApiConfig,
        bridge: Bridge,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._session_manager = session_manager
        self._runner: web.AppRunner | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def bound_port(self) -> int | None:
        """The actual listening port (differs from config when it is 0)."""
        if self._runner is None or not self._runner.addresses:
            return None
        return self._runner.addresses[0][1]

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(HEALTHZ_PATH, self._handle_healthz)
        app.router.add_post(MESSAGES_PATH, self._handle_messages)
        # Unlike LINE's fire-and-forget webhook tasks, all work here happens
        # inside the request handler — aiohttp's graceful shutdown waits for
        # in-flight handlers up to this grace window.
        self._runner = web.AppRunner(app, shutdown_timeout=STOP_GRACE_SECONDS)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "API server listening on %s:%s (auth %s)",
            self._config.host,
            self.bound_port,
            "on" if self._config.auth_token else "off",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def cleanup_stale_sessions(self) -> int:
        """Remove locks for expired client sessions. Returns count removed."""
        if self._session_manager is None:
            return 0
        stale = [
            key
            for key, lock in self._locks.items()
            if not lock.locked() and self._session_manager.get(key) is None
        ]
        for key in stale:
            del self._locks[key]
        if stale:
            logger.info("API: cleaned up %d stale session locks", len(stale))
        return len(stale)

    # --- Handlers ---

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    def _authorized(self, request: web.Request) -> bool:
        if not self._config.auth_token:
            return True
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme != "Bearer" or not token:
            return False
        return hmac.compare_digest(token, self._config.auth_token)

    async def _handle_messages(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.json_response(
                {"error": "unauthorized"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"error": "request body must be valid JSON"}, status=400
            )
        try:
            api_request = parse_request(payload)
        except ApiRequestError as e:
            return web.json_response({"error": str(e)}, status=400)

        session_key, resumable = resolve_session(api_request.session)
        events = self._bridge.handle_message(
            session_key=session_key,
            text=api_request.text,
            context=merge_context(api_request.context),
            system_prompt=api_request.system_prompt or DEFAULT_SYSTEM_PROMPT,
            resumable=resumable,
        )

        if api_request.session is None:
            # One-shot: no lock — every call is an independent session, and the
            # bridge's global semaphore already caps overall concurrency.
            return await self._respond(request, api_request, events)
        # Client session: serialize turns so a second POST for the same
        # session waits for the current run instead of racing the resume.
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await self._respond(request, api_request, events)

    async def _respond(
        self,
        request: web.Request,
        api_request: ApiRequest,
        events: AsyncIterator[BridgeEvent],
    ) -> web.StreamResponse:
        if api_request.stream:
            return await self._respond_stream(request, api_request, events)
        return await self._respond_buffered(api_request, events)

    async def _respond_buffered(
        self, api_request: ApiRequest, events: AsyncIterator[BridgeEvent]
    ) -> web.Response:
        accumulated = ""
        status_updates: list[str] = []
        questions: list[dict] = []
        async for event in events:
            match event:
                case Processing():
                    pass
                case TextDelta(text=chunk):
                    if accumulated:
                        accumulated += "\n\n"
                    accumulated += chunk
                case StatusUpdate() as status:
                    status_updates.append(status_line(status))
                case UserQuestion(questions=asked):
                    questions.extend(asked)
                case Completion() as completion:
                    body = completion_body(
                        api_request.session,
                        completion,
                        accumulated=accumulated,
                        status_updates=status_updates,
                        questions=questions,
                    )
                    status_code = 503 if is_capacity_full(completion) else 200
                    return web.json_response(body, status=status_code)
        # The bridge guarantees a terminal Completion; reaching here means the
        # contract was broken upstream.
        logger.error("API: event stream ended without a Completion")
        return web.json_response(
            {"error": "agent produced no completion"}, status=500
        )

    async def _respond_stream(
        self,
        request: web.Request,
        api_request: ApiRequest,
        events: AsyncIterator[BridgeEvent],
    ) -> web.StreamResponse:
        # Peek at the first event: bridge short-circuits (capacity full) yield
        # a lone error Completion before any Processing — surface those as a
        # plain 503 instead of opening a one-event stream.
        first = await anext(events, None)
        if (
            isinstance(first, Completion)
            and first.is_error
            and is_capacity_full(first)
        ):
            return web.json_response(
                completion_body(api_request.session, first), status=503
            )

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
            }
        )
        await response.prepare(request)

        disconnected = False

        async def _write(event: BridgeEvent) -> None:
            nonlocal disconnected
            if disconnected:
                return
            try:
                await response.write(format_sse(event).encode())
            except ConnectionError:
                # Client went away. Keep draining bridge events silently so
                # the agent run finishes and session state stays consistent.
                disconnected = True
                logger.info("API: SSE client disconnected mid-stream")

        if first is not None:
            await _write(first)
        async for event in events:
            await _write(event)
        if not disconnected:
            await response.write_eof()
        return response

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from typing import Any

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    raise ImportError(
        "LINE dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[line]"
    ) from None

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.line.config import LineConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# LINE text message hard limit (characters).
LINE_MSG_MAX_CHARS = 5000

# Reply and Push accept at most this many message objects per API call.
MESSAGES_PER_CALL = 5

# stop() waits this long for in-flight webhook event tasks before cancelling.
STOP_GRACE_SECONDS = 5.0

_API_TIMEOUT = 30.0

# LINE source type → the field carrying the chat identifier.
_SOURCE_ID_KEYS = {"user": "userId", "group": "groupId", "room": "roomId"}


# --- Pure webhook/message logic (unit-testable without I/O) ---


def verify_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    """X-Line-Signature = base64(HMAC-SHA256(channel_secret, raw body))."""
    if not signature:
        return False
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def chat_id(source: dict) -> str | None:
    """The id reply-less messages are pushed to: userId/groupId/roomId."""
    id_key = _SOURCE_ID_KEYS.get(source.get("type", ""))
    if id_key is None:
        return None
    value = source.get(id_key)
    if isinstance(value, str) and value:
        return value
    return None


def session_key(source: dict) -> str | None:
    target = chat_id(source)
    if target is None:
        return None
    return f"line:{source['type']}:{target}"


def extract_text(event: dict) -> str | None:
    """Return the message text if this webhook event should reach the agent.

    Only ``message`` events carrying a non-empty ``text`` message from a
    resolvable chat qualify; everything else (stickers, images, joins,
    follows, postbacks, ...) is ignored.
    """
    if event.get("type") != "message":
        return None
    message = event.get("message") or {}
    if message.get("type") != "text":
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if chat_id(event.get("source") or {}) is None:
        return None
    return text.strip()


def tag_prompt(text: str, source: dict) -> str:
    # userId only — resolving a display name would cost an extra profile-API
    # round-trip per message. Group members who haven't added the bot as a
    # friend may come without a userId at all.
    user_id = source.get("userId") or "unknown"
    return f"[{user_id}]: {text}"


def split_message(text: str, limit: int = LINE_MSG_MAX_CHARS) -> list[str]:
    """Split text into chunks of at most ``limit`` chars, preferring newline
    boundaries so paragraphs are not cut mid-line."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 1, limit + 1)
        if cut <= 0:
            cut = limit
        chunk = rest[:cut].rstrip("\n")
        if chunk:
            chunks.append(chunk)
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def plan_delivery(
    chunks: list[str], per_call: int = MESSAGES_PER_CALL
) -> tuple[list[str], list[str]]:
    """Split chunks between the single Reply call (at most ``per_call``
    messages) and the Push overflow."""
    return chunks[:per_call], chunks[per_call:]


def format_questions(questions: list[dict]) -> str:
    lines = ["❓ The agent needs your input\n"]
    multi = len(questions) > 1
    for i, q in enumerate(questions, 1):
        question_text = q.get("question", "")
        lines.append(f"{i}. {question_text}" if multi else question_text)
        for opt in q.get("options", []):
            if isinstance(opt, str):
                lines.append(f"  • {opt}")
            else:
                label = opt.get("label", opt.get("value", ""))
                desc = opt.get("description", "")
                lines.append(f"  • {label} — {desc}" if desc else f"  • {label}")
        if q.get("multiSelect"):
            lines.append("(You can select multiple.)")
    lines.append("\nReply in this chat to answer.")
    return "\n".join(lines)


class LineAdapter:
    def __init__(
        self,
        config: LineConfig,
        bridge: Bridge,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._session_manager = session_manager
        self._client: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def bound_port(self) -> int | None:
        """The actual listening port (differs from config when it is 0)."""
        if self._runner is None or not self._runner.addresses:
            return None
        return self._runner.addresses[0][1]

    async def start(self) -> None:
        self._client = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_post(self._config.webhook_path, self._handle_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self._config.webhook_host, self._config.webhook_port
        )
        await site.start()
        logger.info(
            "LINE webhook listening on %s:%s%s",
            self._config.webhook_host,
            self.bound_port,
            self._config.webhook_path,
        )

    async def stop(self) -> None:
        # Stop accepting new webhooks first, give in-flight event tasks a
        # bounded window to converge, then cancel whatever is still running.
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._tasks:
            _done, pending = await asyncio.wait(
                self._tasks, timeout=STOP_GRACE_SECONDS
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._client is not None:
            await self._client.close()
            self._client = None

    def cleanup_stale_sessions(self) -> int:
        """Remove locks for expired sessions. Returns count removed."""
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
            logger.info("LINE: cleaned up %d stale session locks", len(stale))
        return len(stale)

    # --- Webhook handling ---

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get("X-Line-Signature")
        if not verify_signature(self._config.channel_secret, body, signature):
            logger.warning("LINE webhook: rejected request with invalid signature")
            return web.Response(status=403, text="invalid signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        events = payload.get("events") if isinstance(payload, dict) else None
        # Ack immediately — LINE requires a fast 200. Real work happens in
        # background tasks (references kept so they aren't GC'd mid-run).
        for event in events or []:
            if isinstance(event, dict):
                self._dispatch(event)
        return web.Response(text="OK")

    def _dispatch(self, event: dict) -> None:
        text = extract_text(event)
        if text is None:
            return
        task = asyncio.create_task(self._handle_event(event, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_event(self, event: dict, text: str) -> None:
        key = session_key(event.get("source") or {})
        if key is None:
            return
        # Per-session lock: messages in the same chat run one at a time;
        # different chats process concurrently.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await self._process_event(event, text, key)
            except Exception:
                logger.exception("Error processing LINE session %s", key)

    def _build_context(self, source: dict) -> dict[str, str]:
        return {
            "platform": "line",
            "source_type": source.get("type", ""),
            "chat_id": chat_id(source) or "",
            "user_id": source.get("userId") or "",
        }

    def _build_system_prompt(self, context: dict[str, str]) -> str:
        source_type = context.get("source_type") or "unknown"
        return (
            "This conversation is from LINE. "
            "Each message is prefixed with [user_id] to identify the speaker.\n"
            "Reply in plain text only: LINE does not render Markdown, and "
            "replies over 5000 characters are split into multiple messages.\n"
            "The user sees a single reply when you finish — you cannot send "
            "intermediate updates.\n"
            f"Chat: {source_type} {context.get('chat_id', '')}"
        )

    async def _process_event(self, event: dict, text: str, key: str) -> None:
        source = event.get("source") or {}
        context = self._build_context(source)
        accumulated = ""
        pending_questions: list[dict] = []

        async for bridge_event in self._bridge.handle_message(
            session_key=key,
            text=tag_prompt(text, source),
            context=context,
            system_prompt=self._build_system_prompt(context),
        ):
            match bridge_event:
                case Processing():
                    logger.info("LINE session %s: agent starting", key)
                case TextDelta(text=chunk):
                    # Buffered: the reply token is single-use, so everything
                    # is delivered at Completion in one reply.
                    if accumulated:
                        accumulated += "\n\n"
                    accumulated += chunk
                case StatusUpdate(status=status):
                    # LINE has no cheap way to render progress — log only.
                    logger.info("LINE session %s: %s", key, status)
                case UserQuestion(questions=questions):
                    pending_questions = questions
                case Completion(text=final_text, is_error=is_error):
                    final = accumulated or final_text
                    if pending_questions:
                        final = format_questions(pending_questions)
                    if is_error:
                        final = f"❌ {final or 'Agent error.'}"
                    if not final:
                        final = "No response from agent."
                    await self._deliver_final(event, final)

    async def _deliver_final(self, event: dict, text: str) -> None:
        chunks = split_message(text)
        reply_chunks, push_chunks = plan_delivery(chunks)
        reply_token = event.get("replyToken")
        replied = False
        if reply_token:
            replied = await self._reply(reply_token, reply_chunks)
        if not replied:
            # No token, or the reply failed (expired/already-used token, …):
            # deliver everything via Push instead.
            push_chunks = chunks
        if not push_chunks:
            return
        target = chat_id(event.get("source") or {})
        if target is None:
            logger.error(
                "LINE: cannot push %d message(s) — no chat id in source",
                len(push_chunks),
            )
            return
        for start in range(0, len(push_chunks), MESSAGES_PER_CALL):
            await self._push(target, push_chunks[start : start + MESSAGES_PER_CALL])

    # --- Messaging API I/O ---

    @staticmethod
    def _messages(chunks: list[str]) -> list[dict[str, str]]:
        return [{"type": "text", "text": chunk} for chunk in chunks]

    async def _reply(self, reply_token: str, chunks: list[str]) -> bool:
        return await self._api_post(
            "/v2/bot/message/reply",
            {"replyToken": reply_token, "messages": self._messages(chunks)},
        )

    async def _push(self, to: str, chunks: list[str]) -> bool:
        return await self._api_post(
            "/v2/bot/message/push",
            {"to": to, "messages": self._messages(chunks)},
        )

    async def _api_post(self, path: str, payload: dict[str, Any]) -> bool:
        """POST a Messaging API call. Returns success; errors are logged,
        never raised."""
        assert self._client is not None
        url = f"{self._config.api_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._config.channel_access_token}",
        }
        try:
            async with self._client.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    return True
                detail = (await resp.text())[:200]
                logger.warning(
                    "LINE API %s returned %d: %s", path, resp.status, detail
                )
                return False
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("LINE API %s failed: %s", path, e)
            return False

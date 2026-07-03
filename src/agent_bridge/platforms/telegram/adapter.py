from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

try:
    import aiohttp
except ImportError:
    raise ImportError(
        "Telegram dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[telegram]"
    ) from None

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.telegram.config import TelegramConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# Telegram sendMessage/editMessageText hard limit (characters).
TELEGRAM_MSG_MAX_CHARS = 4096

# Minimum interval between edits of the placeholder message (Telegram
# rate-limits edits aggressively).
EDIT_THROTTLE_SECONDS = 1.5

# Long-poll error backoff bounds (exponential, reset on success).
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

# Non-getUpdates API calls get this total timeout.
_DEFAULT_API_TIMEOUT = 30.0

PROCESSING_TEXT = "⏳ Processing..."

_GROUP_CHAT_TYPES = {"group", "supergroup"}


# --- Pure message logic (unit-testable without I/O) ---


def session_key(chat_id: int | str, thread_id: int | None) -> str:
    return f"telegram:{chat_id}:{thread_id or 0}"


def display_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or (user.get("username") or "unknown")


def tag_prompt(text: str, user: dict) -> str:
    user_id = user.get("id")
    name = display_name(user)
    tag = f"{name} ({user_id})" if user_id is not None else name
    return f"[{tag}]: {text}"


def strip_mention(text: str, bot_username: str) -> tuple[str, bool]:
    """Remove @bot_username mentions (case-insensitive, Telegram usernames are).

    Returns (cleaned text, whether a mention was present).
    """
    if not bot_username:
        return text.strip(), False
    pattern = re.compile(rf"@{re.escape(bot_username)}\b", re.IGNORECASE)
    cleaned, count = pattern.subn("", text)
    return cleaned.strip(), count > 0


def message_thread_id(message: dict) -> int | None:
    # Only forum-topic messages define a thread scope; plain replies in a
    # regular group also carry message_thread_id but are not topics.
    if message.get("is_topic_message"):
        return message.get("message_thread_id")
    return None


def extract_prompt(
    message: dict,
    *,
    bot_username: str,
    bot_id: int | None,
    allow_chats: frozenset[str],
) -> str | None:
    """Return the prompt text if the bot should answer this message, else None.

    Rules: text messages only; allow-list gates every chat; private chats are
    always answered; groups/supergroups only on @mention or a reply to the bot.
    """
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    if chat_id is None:
        return None
    if allow_chats and str(chat_id) not in allow_chats:
        return None
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return None

    cleaned, mentioned = strip_mention(text, bot_username)
    if chat_type == "private":
        return cleaned or None
    if chat_type not in _GROUP_CHAT_TYPES:
        return None
    reply_from = (message.get("reply_to_message") or {}).get("from") or {}
    replied_to_bot = bot_id is not None and reply_from.get("id") == bot_id
    if not mentioned and not replied_to_bot:
        return None
    return cleaned or None


def split_message(text: str, limit: int = TELEGRAM_MSG_MAX_CHARS) -> list[str]:
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


class TelegramAdapter:
    def __init__(
        self,
        config: TelegramConfig,
        bridge: Bridge,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._session_manager = session_manager
        self._client: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._bot_username: str | None = None
        self._bot_id: int | None = None
        self._last_update_id: int | None = None

    async def start(self) -> None:
        self._client = aiohttp.ClientSession()
        self._last_update_id = self._read_offset()
        logger.info(
            "Starting Telegram adapter (long polling, offset=%s)",
            self._last_update_id,
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.close()

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
            logger.info("Telegram: cleaned up %d stale session locks", len(stale))
        return len(stale)

    # --- Long-poll loop ---

    async def _poll_loop(self) -> None:
        backoff = BACKOFF_INITIAL_SECONDS
        while not self._stopping.is_set():
            if self._bot_username is None:
                me = await self._api_call("getMe", {})
                if me is None:
                    backoff = await self._backoff(backoff)
                    continue
                self._bot_username = me.get("username") or ""
                self._bot_id = me.get("id")
                logger.info(
                    "Resolved Telegram bot identity: @%s (%s)",
                    self._bot_username,
                    self._bot_id,
                )

            payload: dict[str, Any] = {
                "timeout": self._config.poll_timeout_seconds,
                "allowed_updates": ["message"],
            }
            if self._last_update_id is not None:
                payload["offset"] = self._last_update_id + 1
            updates = await self._api_call(
                "getUpdates",
                payload,
                timeout=self._config.poll_timeout_seconds + 15,
            )
            if updates is None:
                backoff = await self._backoff(backoff)
                continue
            backoff = BACKOFF_INITIAL_SECONDS

            advanced = False
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int) and (
                    self._last_update_id is None or update_id > self._last_update_id
                ):
                    self._last_update_id = update_id
                    advanced = True
                message = update.get("message")
                if isinstance(message, dict):
                    self._dispatch(message)
            # Persist right after dispatching each batch: a restart resumes
            # from the next update and never re-feeds a dispatched prompt.
            if advanced:
                self._write_offset()

    async def _backoff(self, backoff: float) -> float:
        """Wait out a poll error (or return early on stop). Returns the next delay."""
        logger.info("Telegram: retrying poll in %.1fs", backoff)
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        return min(backoff * 2, BACKOFF_MAX_SECONDS)

    # --- Message handling ---

    def _dispatch(self, message: dict) -> None:
        prompt = extract_prompt(
            message,
            bot_username=self._bot_username or "",
            bot_id=self._bot_id,
            allow_chats=self._config.allow_chats,
        )
        if prompt is None:
            return
        task = asyncio.create_task(self._handle_message(message, prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_message(self, message: dict, prompt: str) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        thread_id = message_thread_id(message)
        key = session_key(chat_id, thread_id)
        # Per-session lock: messages in the same chat/topic run one at a time;
        # different sessions process concurrently.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await self._stream_response(message, prompt, key, chat_id, thread_id)
            except Exception:
                logger.exception("Error processing Telegram session %s", key)

    def _build_context(self, message: dict, thread_id: int | None) -> dict[str, str]:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        ctx = {
            "platform": "telegram",
            "chat_id": str(chat.get("id", "")),
            "chat_type": chat.get("type", ""),
            "user_id": str(sender.get("id", "")),
            "user_name": display_name(sender),
            "message_id": str(message.get("message_id", "")),
        }
        if chat.get("title"):
            ctx["chat_title"] = str(chat["title"])
        if thread_id is not None:
            ctx["message_thread_id"] = str(thread_id)
        return ctx

    def _build_system_prompt(self, context: dict[str, str]) -> str:
        parts: list[str] = []
        chat_id = context.get("chat_id", "")
        chat_type = context.get("chat_type", "") or "unknown"
        title = context.get("chat_title", "")
        if title:
            parts.append(f"Chat: {title} (id {chat_id}, {chat_type})")
        else:
            parts.append(f"Chat id: {chat_id} ({chat_type})")
        if context.get("message_thread_id"):
            parts.append(f"Topic thread: {context['message_thread_id']}")
        if self._bot_username:
            parts.append(f"Your Telegram mention: @{self._bot_username}")
        return (
            "This conversation is from Telegram. "
            "Each message is prefixed with [display_name (user_id)] to identify "
            "the speaker.\n"
            "Prefer plain text: Telegram supports only limited Markdown (no "
            "headings or tables), and replies over 4096 characters are split "
            "into multiple messages.\n" + "\n".join(parts)
        )

    async def _stream_response(
        self,
        message: dict,
        prompt: str,
        key: str,
        chat_id: int | str,
        thread_id: int | None,
    ) -> None:
        context = self._build_context(message, thread_id)
        reply_to = message.get("message_id")
        placeholder: int | None = None
        accumulated = ""
        pending_questions: list[dict] = []
        last_edit = 0.0

        async for event in self._bridge.handle_message(
            session_key=key,
            text=tag_prompt(prompt, message.get("from") or {}),
            context=context,
            system_prompt=self._build_system_prompt(context),
        ):
            match event:
                case Processing():
                    placeholder = await self._send_message(
                        chat_id,
                        PROCESSING_TEXT,
                        reply_to=reply_to,
                        thread_id=thread_id,
                        markdown=False,
                    )
                case TextDelta(text=chunk):
                    # Buffered: Telegram rate-limits message edits hard, so
                    # deltas are collected and rendered once at Completion.
                    if accumulated:
                        accumulated += "\n\n"
                    accumulated += chunk
                case StatusUpdate(status=status):
                    now = time.monotonic()
                    if (
                        placeholder is not None
                        and now - last_edit >= EDIT_THROTTLE_SECONDS
                    ):
                        await self._edit_message(
                            chat_id, placeholder, f"⏳ {status}", markdown=False
                        )
                        last_edit = now
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
                    await self._deliver_final(
                        chat_id,
                        placeholder,
                        final,
                        reply_to=reply_to,
                        thread_id=thread_id,
                    )

    async def _deliver_final(
        self,
        chat_id: int | str,
        placeholder: int | None,
        text: str,
        *,
        reply_to: int | None,
        thread_id: int | None,
    ) -> None:
        chunks = split_message(text)
        delivered = False
        if placeholder is not None:
            delivered = await self._edit_message(chat_id, placeholder, chunks[0])
        if not delivered:
            # No placeholder (e.g. lone error Completion) or the edit failed —
            # deliver the first chunk as a fresh reply instead.
            await self._send_message(
                chat_id, chunks[0], reply_to=reply_to, thread_id=thread_id
            )
        for chunk in chunks[1:]:
            await self._send_message(chat_id, chunk, thread_id=thread_id)

    # --- Bot API I/O ---

    async def _send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
        markdown: bool = True,
    ) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        result = await self._call_with_fallback("sendMessage", payload, markdown)
        if isinstance(result, dict):
            return result.get("message_id")
        return None

    async def _edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        markdown: bool = True,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        result = await self._call_with_fallback("editMessageText", payload, markdown)
        return result is not None

    async def _call_with_fallback(
        self, method: str, payload: dict[str, Any], markdown: bool
    ) -> Any | None:
        # Telegram rejects the whole message when Markdown fails to parse —
        # retry without parse_mode so the reply always gets through.
        if markdown:
            result = await self._api_call(method, {**payload, "parse_mode": "Markdown"})
            if result is not None:
                return result
        return await self._api_call(method, payload)

    async def _api_call(
        self, method: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any | None:
        """POST a Bot API method. Returns the ``result`` payload, or None on
        any transport/API error (logged, never raised)."""
        assert self._client is not None
        url = f"{self._config.api_base_url}/bot{self._config.bot_token}/{method}"
        try:
            async with self._client.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout or _DEFAULT_API_TIMEOUT),
            ) as resp:
                data = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("Telegram API %s failed: %s", method, e)
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            description = (
                data.get("description") if isinstance(data, dict) else repr(data)
            )
            logger.warning("Telegram API %s error: %s", method, description)
            return None
        return data.get("result")

    # --- Offset persistence (heartbeat state-file pattern) ---

    def _read_offset(self) -> int | None:
        if not self._config.state_path.exists():
            return None
        try:
            data = json.loads(self._config.state_path.read_text())
            value = data.get("last_update_id")
            return int(value) if value is not None else None
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning("Telegram: failed to read state file: %s", e)
            return None

    def _write_offset(self) -> None:
        try:
            self._config.state_path.parent.mkdir(parents=True, exist_ok=True)
            self._config.state_path.write_text(
                json.dumps({"last_update_id": self._last_update_id}, indent=2)
            )
        except OSError as e:
            logger.error("Telegram: failed to write state file: %s", e)

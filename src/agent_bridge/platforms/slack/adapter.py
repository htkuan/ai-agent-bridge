from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError(
        "Slack dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[slack]"
    ) from None

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# Minimum interval between Slack message updates (seconds)
UPDATE_THROTTLE_SECONDS = 1.5

# Slack chat_update/chat_postMessage effective ceiling. Empirically ~4000
# UTF-8 bytes (not characters) — a CJK char is 3 bytes, so a char-based
# check lets long CJK messages slip past and hit msg_too_long.
SLACK_MSG_MAX_BYTES = 3_900


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    # errors="ignore" drops any trailing partial multi-byte sequence
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _fit_with_suffix(text: str, max_bytes: int, suffix: str) -> str:
    if _utf8_len(text) <= max_bytes:
        return text
    budget = max(0, max_bytes - _utf8_len(suffix))
    return _truncate_to_bytes(text, budget) + suffix


@dataclass
class _PendingMessage:
    """A queued user message waiting to be processed."""

    text: str
    context: dict[str, str]
    message_ts: str
    channel: str
    thread_ts: str


@dataclass
class _SessionState:
    """Per-session state: a lock serialises all mutations, plus a single pending slot."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    processing: bool = False
    pending: _PendingMessage | None = None
    waiting_for_answer: bool = False


class SlackInfoCache:
    """Cache for Slack workspace, channel, and user display names."""

    def __init__(self) -> None:
        self.workspace: str | None = None
        self.channels: dict[str, str] = {}
        self.users: dict[str, str] = {}

    async def resolve(self, channel: str, user_id: str, client) -> tuple[str, str, str]:
        """Return (workspace_name, channel_name, user_name), fetching only on cache miss."""
        if self.workspace is None:
            try:
                team_info = await client.team_info()
                self.workspace = team_info["team"].get("name", "")
            except SlackApiError as e:
                logger.warning("Failed to resolve workspace name: %s", e.response["error"])

        if channel not in self.channels:
            try:
                conv_info = await client.conversations_info(channel=channel)
                self.channels[channel] = conv_info["channel"].get("name") or channel
            except SlackApiError as e:
                logger.warning(
                    "Failed to resolve channel name for %s: %s",
                    channel,
                    e.response["error"],
                )
                self.channels[channel] = channel

        if user_id not in self.users:
            try:
                user_info = await client.users_info(user=user_id)
                profile = user_info["user"]["profile"]
                self.users[user_id] = (
                    profile.get("display_name") or profile.get("real_name") or user_id
                )
            except SlackApiError as e:
                logger.warning(
                    "Failed to resolve user name for %s: %s",
                    user_id,
                    e.response["error"],
                )
                self.users[user_id] = user_id

        return (self.workspace or "", self.channels[channel], self.users[user_id])


class SlackAdapter:
    def __init__(
        self,
        config: SlackConfig,
        bridge: Bridge,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._session_manager = session_manager
        self._app = AsyncApp(token=config.bot_token)
        self._handler: AsyncSocketModeHandler | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._name_cache = SlackInfoCache()
        self._register_handlers()

    # --- Session key: Slack defines thread = session ---

    @staticmethod
    def _session_key(channel: str, thread_ts: str) -> str:
        return f"slack:{channel}:{thread_ts}"

    def _get_state(self, session_key: str) -> _SessionState:
        return self._sessions.setdefault(session_key, _SessionState())

    def cleanup_stale_sessions(self) -> int:
        """Remove state for expired sessions. Returns count removed."""
        if self._session_manager is None:
            return 0
        stale = [
            key
            for key, state in self._sessions.items()
            if not state.processing
            and not state.waiting_for_answer
            and state.pending is None
            and not state.lock.locked()
            and self._session_manager.get(key) is None
        ]
        for key in stale:
            del self._sessions[key]
        if stale:
            logger.info("Cleaned up %d stale session entries", len(stale))
        return len(stale)

    # --- Event handlers ---

    def _register_handlers(self) -> None:
        @self._app.event("app_mention")
        async def handle_mention(event: dict, say, client) -> None:
            await self._process_message(event, say, client)

        @self._app.event("message")
        async def handle_dm(event: dict, say, client) -> None:
            # Only handle DMs (channel type "im"), skip bot messages
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            await self._process_message(event, say, client)

    async def _resolve_context(
        self, channel: str, user_id: str, thread_ts: str, client
    ) -> dict[str, str]:
        """Resolve display names via cache and build context dict."""
        workspace, channel_name, user_name = await self._name_cache.resolve(
            channel, user_id, client
        )
        return {
            "platform": "slack",
            "workspace": workspace,
            "channel_id": channel,
            "channel_name": channel_name,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "user_name": user_name,
        }

    @staticmethod
    def _tag_prompt(text: str, context: dict[str, str]) -> str:
        # Slack-flavored sender identity prefix. The agent stays platform-agnostic;
        # the platform owns how it names speakers.
        user_name = context.get("user_name", "unknown")
        user_id = context.get("user_id", "")
        tag = f"{user_name} ({user_id})" if user_id else user_name
        return f"[{tag}]: {text}"

    @staticmethod
    def _build_system_prompt(context: dict[str, str]) -> str:
        parts = [f"Platform: {context.get('platform', 'slack')}"]
        if context.get("workspace"):
            parts.append(f"Workspace: {context['workspace']}")
        channel_name = context.get("channel_name", "")
        channel_id = context.get("channel_id", "")
        if channel_name and channel_id:
            parts.append(f"Channel: #{channel_name} ({channel_id})")
        elif channel_id:
            parts.append(f"Channel: {channel_id}")
        if context.get("thread_ts"):
            parts.append(f"Thread: {context['thread_ts']}")

        return (
            "This conversation is from a chat platform. "
            "Each message is prefixed with [user_name (user_id)] to identify the speaker.\n"
            + "\n".join(parts)
        )

    async def _process_message(self, event: dict, say, client) -> None:
        channel = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # Strip bot mention from text (e.g., "<@U12345> do something" → "do something")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        # Append file/image info so the agent can decide whether to fetch them
        files = event.get("files") or []
        if files:
            parts = []
            for f in files:
                name = f.get("name", "unknown")
                mimetype = f.get("mimetype", "unknown")
                url = (
                    f.get("url_private_download")
                    or f.get("url_private")
                    or ""
                )
                parts.append(f"- {name} ({mimetype}): {url}")
            token = self._config.bot_token
            hint = (
                "[Slack attachments — download with: "
                f'curl -H "Authorization: Bearer {token}" '
                '"<url>" -o /tmp/<filename>]'
            )
            text = f"{text}\n\n{hint}\n" + "\n".join(parts)
            text = text.strip()

        if not text:
            return

        session_key = self._session_key(channel, thread_ts)
        state = self._get_state(session_key)

        # --- Gate 1: per-session serialisation ---
        # Lock guards ALL reads/writes to this session's state
        async with state.lock:
            if state.waiting_for_answer:
                # User is answering a question — resume the session
                logger.info("Session %s: received answer, resuming", session_key)
                state.waiting_for_answer = False
                state.processing = True
            elif state.processing:
                # Session busy — replace the pending slot (keep only latest)
                context = await self._resolve_context(
                    channel, user_id, thread_ts, client
                )
                result = await say(
                    text=":hourglass: Waiting for previous task to finish...",
                    thread_ts=thread_ts,
                )
                if state.pending is not None:
                    await self._delete_message(
                        state.pending.channel, state.pending.message_ts
                    )
                state.pending = _PendingMessage(
                    text=text,
                    context=context,
                    message_ts=result["ts"],
                    channel=channel,
                    thread_ts=thread_ts,
                )
                return
            else:
                # Session idle — mark as processing, then release lock to do real work
                state.processing = True

        # --- Processing happens outside the lock so new messages can queue ---
        try:
            context = await self._resolve_context(
                channel, user_id, thread_ts, client
            )
            status = await self._stream_response(
                channel, thread_ts, session_key, text, context, say
            )

            if status == "waiting_for_answer":
                async with state.lock:
                    state.waiting_for_answer = True
                    state.processing = False
                return

            # Drain pending (re-acquire lock each iteration to read state safely)
            while True:
                async with state.lock:
                    if state.pending is None:
                        state.processing = False
                        return
                    pending = state.pending
                    state.pending = None

                status = await self._stream_response(
                    pending.channel,
                    pending.thread_ts,
                    session_key,
                    pending.text,
                    pending.context,
                    say=None,
                    existing_message_ts=pending.message_ts,
                )

                if status == "waiting_for_answer":
                    async with state.lock:
                        state.waiting_for_answer = True
                        state.processing = False
                    return
        except Exception:
            logger.exception("Error processing session %s", session_key)
            async with state.lock:
                remaining = state.pending
                state.pending = None
                state.processing = False
            if remaining is not None:
                await self._delete_message(remaining.channel, remaining.message_ts)

    async def _stream_response(
        self,
        channel: str,
        thread_ts: str,
        session_key: str,
        text: str,
        context: dict[str, str],
        say=None,
        existing_message_ts: str | None = None,
    ) -> str | None:
        """Stream agent events and update the Slack message.

        For new messages, ``say`` is used to post the initial reply.
        For pending (drained) messages, ``existing_message_ts`` points
        to the already-posted placeholder.

        Returns ``"waiting_for_answer"`` when the agent asked the user a
        question and the session should wait for a reply; ``None`` otherwise.
        """
        message_ts = existing_message_ts
        accumulated_text = ""
        tool_status = ""
        last_update_time = 0.0
        pending_user_questions: list[dict] = []
        completed = False

        async for event_obj in self._bridge.handle_message(
            session_key=session_key,
            text=self._tag_prompt(text, context),
            context=context,
            system_prompt=self._build_system_prompt(context),
        ):
            match event_obj:
                case Processing():
                    logger.debug("Session %s: Processing → posting initial message", session_key)
                    if message_ts is None and say is not None:
                        result = await say(
                            text=":hourglass_flowing_sand: Processing...",
                            thread_ts=thread_ts,
                        )
                        message_ts = result["ts"]
                    elif message_ts is not None:
                        await self._update_message(
                            channel,
                            message_ts,
                            ":hourglass_flowing_sand: Processing...",
                        )

                case TextDelta(text=chunk):
                    if accumulated_text:
                        accumulated_text += "\n\n"
                    accumulated_text += chunk
                    now = time.monotonic()
                    if now - last_update_time >= UPDATE_THROTTLE_SECONDS and message_ts:
                        logger.debug(
                            "Session %s: TextDelta → updating message (%d chars)",
                            session_key,
                            len(accumulated_text),
                        )
                        await self._update_message(
                            channel, message_ts, accumulated_text + tool_status,
                        )
                        last_update_time = now

                case StatusUpdate(status=status):
                    tool_status = f"\n\n_{status}_"
                    now = time.monotonic()
                    if now - last_update_time >= UPDATE_THROTTLE_SECONDS and message_ts:
                        logger.debug("Session %s: StatusUpdate → %s", session_key, status)
                        display = (
                            accumulated_text + tool_status
                            if accumulated_text
                            else tool_status
                        )
                        await self._update_message(
                            channel, message_ts, display
                        )
                        last_update_time = now

                case UserQuestion(questions=questions):
                    logger.info(
                        "Session %s: agent asked %d question(s), entering waiting_for_answer",
                        session_key,
                        len(questions),
                    )
                    pending_user_questions = questions

                case Completion(text=final_text, is_error=is_error):
                    completed = True
                    if pending_user_questions:
                        logger.debug(
                            "Session %s: Completion with pending questions → posting questions to Slack",
                            session_key,
                        )
                        formatted = self._format_questions_for_slack(
                            pending_user_questions
                        )
                        if message_ts:
                            await self._update_message(
                                channel, message_ts, formatted,
                            )
                        elif say is not None:
                            await say(text=formatted, thread_ts=thread_ts)
                        return "waiting_for_answer"

                    logger.debug(
                        "Session %s: Completion → final message (is_error=%s)",
                        session_key,
                        is_error,
                    )
                    # Ensure minimum gap since last Slack update to avoid rate limits
                    elapsed = time.monotonic() - last_update_time
                    if last_update_time and elapsed < UPDATE_THROTTLE_SECONDS:
                        await asyncio.sleep(UPDATE_THROTTLE_SECONDS - elapsed)

                    final = accumulated_text or final_text
                    if is_error:
                        if existing_message_ts is not None:
                            # Pending message rejected by Bridge
                            final = (
                                ":x: Your queued message could not be "
                                "processed — please try again shortly."
                            )
                        else:
                            final = (
                                ":no_entry: Too many requests being "
                                "processed, please try again later."
                            )
                    if not final:
                        final = "_No response from agent._"
                    if _utf8_len(final) > SLACK_MSG_MAX_BYTES:
                        uploaded = await self._upload_snippet(
                            channel, thread_ts, final
                        )
                        notice = (
                            "\n\n_… Full response uploaded as file below._"
                            if uploaded
                            else "\n\n_… (response too long; upload failed — please retry)_"
                        )
                        preview_budget = min(
                            1000, SLACK_MSG_MAX_BYTES - _utf8_len(notice)
                        )
                        short = _truncate_to_bytes(final, preview_budget) + notice
                        if message_ts:
                            await self._update_message(channel, message_ts, short)
                        elif say is not None:
                            await say(text=short, thread_ts=thread_ts)
                    elif message_ts:
                        await self._update_message(channel, message_ts, final)
                    elif say is not None:
                        await say(text=final, thread_ts=thread_ts)

        # Safety net: if stream ended without Completion, strip residual tool_status
        if not completed and message_ts and accumulated_text:
            logger.warning(
                "Session %s: stream ended without Completion — cleaning up message",
                session_key,
            )
            if _utf8_len(accumulated_text) > SLACK_MSG_MAX_BYTES:
                uploaded = await self._upload_snippet(
                    channel, thread_ts, accumulated_text
                )
                notice = (
                    "\n\n_… Full response uploaded as file below._"
                    if uploaded
                    else "\n\n_… (response too long; upload failed — please retry)_"
                )
                preview_budget = min(
                    1000, SLACK_MSG_MAX_BYTES - _utf8_len(notice)
                )
                short = _truncate_to_bytes(accumulated_text, preview_budget) + notice
                await self._update_message(channel, message_ts, short)
            else:
                await self._update_message(channel, message_ts, accumulated_text)

        return None

    @staticmethod
    def _format_questions_for_slack(questions: list[dict]) -> str:
        """Format AskUserQuestion questions for Slack display."""
        lines = [":question: *Claude needs your input*\n"]
        multi = len(questions) > 1
        for i, q in enumerate(questions, 1):
            question_text = q.get("question", "")
            if multi:
                lines.append(f"*{i}.* {question_text}")
            else:
                lines.append(question_text)

            options = q.get("options", [])
            for opt in options:
                if isinstance(opt, str):
                    lines.append(f"  • `{opt}`")
                else:
                    label = opt.get("label", opt.get("value", ""))
                    desc = opt.get("description", "")
                    if desc:
                        lines.append(f"  • `{label}` — {desc}")
                    else:
                        lines.append(f"  • `{label}`")

            if q.get("multiSelect"):
                lines.append("_You can select multiple._")

        lines.append("\nReply in this thread to answer.")
        return "\n".join(lines)

    async def _delete_message(self, channel: str, ts: str) -> None:
        try:
            await self._app.client.chat_delete(channel=channel, ts=ts)
        except SlackApiError as e:
            logger.warning(
                "Failed to delete Slack message %s: %s", ts, e.response["error"]
            )

    async def _update_message(self, channel: str, ts: str, text: str) -> None:
        text = _fit_with_suffix(
            text, SLACK_MSG_MAX_BYTES, "\n\n_… (generating response…)_"
        )
        try:
            await self._app.client.chat_update(
                channel=channel,
                ts=ts,
                text=text,
            )
            return
        except SlackApiError as e:
            if e.response["error"] != "msg_too_long":
                logger.warning(
                    "Failed to update Slack message %s: %s",
                    ts,
                    e.response["error"],
                )
                return

        # Slack still rejected the byte-trimmed payload. Fall back progressively
        # rather than the old hard 500-char cut, which caused a 528-char stuck
        # message for CJK-heavy replies.
        for budget in (
            SLACK_MSG_MAX_BYTES * 3 // 4,
            SLACK_MSG_MAX_BYTES // 2,
            SLACK_MSG_MAX_BYTES // 4,
        ):
            short = _fit_with_suffix(
                text, budget, "\n\n_… (response truncated)_"
            )
            try:
                await self._app.client.chat_update(
                    channel=channel, ts=ts, text=short
                )
                return
            except SlackApiError as retry_err:
                if retry_err.response["error"] != "msg_too_long":
                    logger.warning(
                        "Failed to update Slack message %s: %s",
                        ts,
                        retry_err.response["error"],
                    )
                    return
        logger.warning(
            "Slack rejected update to %s at all fallback sizes", ts
        )

    async def _upload_snippet(
        self, channel: str, thread_ts: str, content: str
    ) -> bool:
        try:
            await self._app.client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                content=content,
                filename="response.md",
                title="Full Response",
            )
            return True
        except SlackApiError as e:
            logger.warning("Failed to upload snippet: %s", e.response["error"])
            return False

    async def start(self) -> None:
        self._handler = AsyncSocketModeHandler(
            self._app, self._config.app_token
        )
        logger.info("Starting Slack adapter (Socket Mode)")
        await self._handler.connect_async()

        if self._config.startup_notify_channel and self._config.startup_notify_message:
            try:
                await self._app.client.chat_postMessage(
                    channel=self._config.startup_notify_channel,
                    text=self._config.startup_notify_message,
                )
                logger.info(
                    "Startup notification sent to %s",
                    self._config.startup_notify_channel,
                )
            except SlackApiError as e:
                logger.warning(
                    "Failed to send startup notification: %s",
                    e.response["error"],
                )

    async def stop(self) -> None:
        if self._handler:
            await self._handler.close_async()

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError(
        "Slack dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[slack]"
    ) from None

from agent_bridge.bridge.events import (
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    Usage,
    UserQuestion,
)
from agent_bridge.bridge.protocols import MessageRouter
from agent_bridge.bridge.session import SessionManager
from agent_bridge.platforms.slack.config import SlackConfig, normalize_channel

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


class _SafeDict(dict[str, object]):
    """Leaves unknown {placeholders} blank instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return ""


def _usage_fields(usage: Usage, prefix: str = "") -> dict[str, object]:
    return {
        f"{prefix}cost_usd": f"{usage.cost_usd:.4f}",
        f"{prefix}input_tokens": usage.input_tokens,
        f"{prefix}output_tokens": usage.output_tokens,
        f"{prefix}cache_read_tokens": usage.cache_read_tokens,
        f"{prefix}cache_creation_tokens": usage.cache_creation_tokens,
        f"{prefix}total_tokens": usage.total_tokens,
        f"{prefix}num_turns": usage.num_turns,
        f"{prefix}duration_ms": usage.duration_ms,
        f"{prefix}duration_s": f"{usage.duration_ms / 1000:.1f}",
        f"{prefix}duration_api_ms": usage.duration_api_ms,
    }


def _render_usage_template(template: str, turn: Usage, session: Usage | None) -> str:
    """Substitute {placeholders} in a user-supplied template. Session fields
    fall back to zeros when the session total isn't tracked. Malformed
    templates degrade to the raw string rather than crashing the reply.
    """
    values = _usage_fields(turn)
    values.update(_usage_fields(session or Usage(), "session_"))
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        logger.warning("Invalid AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE: %r", template)
        return template


def _default_usage_footer(turn: Usage, session: Usage | None) -> str:
    lines = [
        f"💰 ${turn.cost_usd:.4f} · "
        f"🔢 {turn.input_tokens} in / {turn.output_tokens} out · "
        f"📦 {turn.cache_read_tokens} cached · "
        f"⏱️ {turn.duration_ms / 1000:.1f}s"
    ]
    # Option B: only show the session line when we have a trustworthy total.
    if session is not None:
        lines.append(
            f"📈 session: ${session.cost_usd:.4f} · "
            f"{session.total_tokens} tokens · "
            f"{session.num_turns} turns"
        )
    return "\n".join(lines)


# Slack message text has no real horizontal rule, so fake a labelled one with
# box-drawing chars — the "cost" label centred in the rule marks everything
# below it as usage/cost info.
_USAGE_DIVIDER = "─────cost─────"


def _as_footnote(body: str) -> str:
    """Render text as an italic footnote beneath a labelled divider. Applied to
    both the default layout and a custom template so the footer reads as a note.
    """
    lines = [_USAGE_DIVIDER]
    for line in body.split("\n"):
        lines.append(f"_{line}_" if line.strip() else "")
    return "\n".join(lines)


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
    """Per-session state: a lock serialises all mutations, plus a single
    pending slot."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    processing: bool = False
    pending: _PendingMessage | None = None
    waiting_for_answer: bool = False


@dataclass
class _RenderState:
    """Mutable state for rendering one agent event stream into a Slack
    message. ``message_ts`` starts as ``existing_message_ts`` (drained
    pending placeholder) and is otherwise set by the first Processing
    event; ``existing_message_ts`` stays as-was to mark drained runs."""

    channel: str
    thread_ts: str
    session_key: str
    say: Any
    existing_message_ts: str | None
    message_ts: str | None
    accumulated_text: str = ""
    tool_status: str = ""
    last_update_time: float = 0.0
    pending_user_questions: list[dict[str, Any]] = field(
        default_factory=list[dict[str, Any]]
    )
    completed: bool = False


def _api_error_detail(e: SlackApiError) -> str:
    """Slack names the missing OAuth scope in ``needed`` — surface it so the
    operator knows which scope to add instead of just seeing `missing_scope`."""
    error = str(e.response["error"])
    needed = e.response.get("needed")
    return f"{error} (add bot token scope: {needed})" if needed else error


class SlackInfoCache:
    """Cache for Slack workspace, channel, and user display names."""

    def __init__(self) -> None:
        self.workspace: str | None = None
        self.channels: dict[str, str] = {}
        self.users: dict[str, str] = {}

    async def resolve_channel(self, channel: str, client: Any) -> str:
        """Return the channel name, fetching only on cache miss.

        DMs/group-DMs have no name; falls back to the channel id.
        """
        if channel not in self.channels:
            try:
                conv_info = await client.conversations_info(channel=channel)
                self.channels[channel] = conv_info["channel"].get("name") or channel
            except SlackApiError as e:
                logger.warning(
                    "Failed to resolve channel name for %s: %s",
                    channel,
                    _api_error_detail(e),
                )
                self.channels[channel] = channel
        return self.channels[channel]

    async def resolve(
        self, channel: str, user_id: str, client: Any
    ) -> tuple[str, str, str]:
        """Return (workspace_name, channel_name, user_name), fetching only
        on cache miss."""
        if self.workspace is None:
            try:
                team_info = await client.team_info()
                self.workspace = team_info["team"].get("name", "")
            except SlackApiError as e:
                logger.warning(
                    "Failed to resolve workspace name: %s", _api_error_detail(e)
                )

        channel_name = await self.resolve_channel(channel, client)

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
                    _api_error_detail(e),
                )
                self.users[user_id] = user_id

        return (self.workspace or "", channel_name, self.users[user_id])


class SlackAdapter:
    def __init__(
        self,
        config: SlackConfig,
        bridge: MessageRouter,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._session_manager = session_manager
        self._app = AsyncApp(token=config.bot_token)
        self._handler: AsyncSocketModeHandler | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._name_cache = SlackInfoCache()
        self._bot_user_id: str | None = None
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
        # Never called by name: the decorator registers it with bolt.
        async def handle_mention(  # pyright: ignore[reportUnusedFunction]
            event: dict[str, Any], say: Any, client: Any
        ) -> None:
            await self._process_message(event, say, client)

        @self._app.event("message")
        # Never called by name: the decorator registers it with bolt.
        async def handle_dm(  # pyright: ignore[reportUnusedFunction]
            event: dict[str, Any], say: Any, client: Any
        ) -> None:
            # Only handle DMs (channel type "im"), skip bot messages
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            await self._process_message(event, say, client)

    async def _channel_allowed(self, channel: str, client: Any) -> bool:
        """Gate by the configured channel allow-list.

        Empty allow-list = allow everything. Otherwise only channels whose
        resolved name matches reach the agent; DMs (no name) never match.
        """
        if not self._config.allow_channels:
            return True
        channel_name = await self._name_cache.resolve_channel(channel, client)
        return normalize_channel(channel_name) in self._config.allow_channels

    async def _resolve_context(
        self, channel: str, user_id: str, thread_ts: str, client: Any
    ) -> dict[str, str]:
        """Resolve display names via cache and build context dict."""
        workspace, channel_name, user_name = await self._name_cache.resolve(
            channel, user_id, client
        )
        ctx = {
            "workspace": workspace,
            "channel_id": channel,
            "channel_name": channel_name,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "user_name": user_name,
        }
        if self._bot_user_id:
            ctx["bot_user_id"] = self._bot_user_id
        return ctx

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
        parts: list[str] = []
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
        bot_user_id = context.get("bot_user_id", "")
        if bot_user_id:
            parts.append(f"Your Slack mention: <@{bot_user_id}>")

        return (
            "This conversation is from Slack. "
            "Each message is prefixed with [user_name (user_id)] "
            "to identify the speaker.\n" + "\n".join(parts)
        )

    def _prepare_text(self, text: str, event: dict[str, Any]) -> str:
        """Strip the bot mention and append attachment info so the agent can
        decide whether to fetch the files."""
        # e.g., "<@U12345> do something" → "do something"
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        files = event.get("files") or []
        if not files:
            return text
        parts = []
        for f in files:
            name = f.get("name", "unknown")
            mimetype = f.get("mimetype", "unknown")
            url = f.get("url_private_download") or f.get("url_private") or ""
            parts.append(f"- {name} ({mimetype}): {url}")
        token = self._config.bot_token
        hint = (
            "[Slack attachments — download with: "
            f'curl -H "Authorization: Bearer {token}" '
            '"<url>" -o /tmp/<filename>]'
        )
        text = f"{text}\n\n{hint}\n" + "\n".join(parts)
        return text.strip()

    async def _park_pending(
        self,
        state: _SessionState,
        channel: str,
        user_id: str,
        thread_ts: str,
        text: str,
        say: Any,
        client: Any,
    ) -> None:
        """Park the message in the single pending slot (keep only latest),
        deleting the placeholder of any message it replaces. Caller holds
        the session lock."""
        context = await self._resolve_context(channel, user_id, thread_ts, client)
        result = await say(
            text=":hourglass: Waiting for previous task to finish...",
            thread_ts=thread_ts,
        )
        if state.pending is not None:
            await self._delete_message(state.pending.channel, state.pending.message_ts)
        state.pending = _PendingMessage(
            text=text,
            context=context,
            message_ts=result["ts"],
            channel=channel,
            thread_ts=thread_ts,
        )

    async def _run_and_drain(
        self,
        state: _SessionState,
        session_key: str,
        channel: str,
        thread_ts: str,
        text: str,
        context: dict[str, str],
        say: Any,
    ) -> None:
        """Stream the reply, then keep draining the pending slot (re-acquiring
        the lock each iteration to read state safely) until it's empty or the
        agent asks a question."""
        status = await self._stream_response(
            channel, thread_ts, session_key, text, context, say
        )
        while status != "waiting_for_answer":
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
        async with state.lock:
            state.waiting_for_answer = True
            state.processing = False

    async def _process_message(
        self, event: dict[str, Any], say: Any, client: Any
    ) -> None:
        channel = event.get("channel", "")
        user_id = event.get("user", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # Gate 0: channel allow-list. Reply with a fixed notice, then stop.
        if not await self._channel_allowed(channel, client):
            logger.info("Rejecting message from non-allowed channel %s", channel)
            await say(
                text=self._config.channel_not_allowed_message,
                thread_ts=thread_ts,
            )
            return

        text = self._prepare_text(event.get("text", ""), event)
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
                await self._park_pending(
                    state, channel, user_id, thread_ts, text, say, client
                )
                return
            else:
                # Session idle — mark as processing, then release lock to do real work
                state.processing = True

        # --- Processing happens outside the lock so new messages can queue ---
        try:
            context = await self._resolve_context(channel, user_id, thread_ts, client)
            await self._run_and_drain(
                state, session_key, channel, thread_ts, text, context, say
            )
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
        say: Any = None,
        existing_message_ts: str | None = None,
    ) -> str | None:
        """Stream agent events and update the Slack message.

        For new messages, ``say`` is used to post the initial reply.
        For pending (drained) messages, ``existing_message_ts`` points
        to the already-posted placeholder.

        Returns ``"waiting_for_answer"`` when the agent asked the user a
        question and the session should wait for a reply; ``None`` otherwise.
        """
        st = _RenderState(
            channel=channel,
            thread_ts=thread_ts,
            session_key=session_key,
            say=say,
            existing_message_ts=existing_message_ts,
            message_ts=existing_message_ts,
        )

        async for event_obj in self._bridge.handle_message(
            session_key=session_key,
            text=self._tag_prompt(text, context),
            context=context,
            system_prompt=self._build_system_prompt(context),
        ):
            match event_obj:
                case Processing():
                    await self._render_processing(st)
                case TextDelta(text=chunk):
                    await self._render_text_delta(st, chunk)
                case StatusUpdate(status=status):
                    await self._render_status(st, status)
                case UserQuestion(questions=questions):
                    logger.info(
                        "Session %s: agent asked %d question(s), "
                        "entering waiting_for_answer",
                        session_key,
                        len(questions),
                    )
                    st.pending_user_questions = questions
                case Completion(
                    text=final_text,
                    is_error=is_error,
                    usage=usage,
                    session_usage=session_usage,
                ):
                    result = await self._render_completion(
                        st, final_text, is_error, usage, session_usage
                    )
                    if result is not None:
                        return result

        await self._render_incomplete_tail(st)
        return None

    async def _render_processing(self, st: _RenderState) -> None:
        """Post (or reset) the placeholder message the stream renders into."""
        logger.debug("Session %s: Processing → posting initial message", st.session_key)
        if st.message_ts is None and st.say is not None:
            result = await st.say(
                text=":hourglass_flowing_sand: Processing...",
                thread_ts=st.thread_ts,
            )
            st.message_ts = result["ts"]
        elif st.message_ts is not None:
            await self._update_message(
                st.channel,
                st.message_ts,
                ":hourglass_flowing_sand: Processing...",
            )

    async def _render_text_delta(self, st: _RenderState, chunk: str) -> None:
        if st.accumulated_text:
            st.accumulated_text += "\n\n"
        st.accumulated_text += chunk
        await self._render_throttled(st, st.accumulated_text + st.tool_status)

    async def _render_status(self, st: _RenderState, status: str) -> None:
        st.tool_status = f"\n\n_{status}_"
        display = (
            st.accumulated_text + st.tool_status
            if st.accumulated_text
            else st.tool_status
        )
        await self._render_throttled(st, display)

    async def _render_throttled(self, st: _RenderState, display: str) -> None:
        """Update the placeholder at most once per throttle window."""
        now = time.monotonic()
        if now - st.last_update_time >= UPDATE_THROTTLE_SECONDS and st.message_ts:
            logger.debug(
                "Session %s: updating message (%d chars)",
                st.session_key,
                len(display),
            )
            await self._update_message(st.channel, st.message_ts, display)
            st.last_update_time = now

    async def _render_completion(
        self,
        st: _RenderState,
        final_text: str,
        is_error: bool,
        usage: Usage | None,
        session_usage: Usage | None,
    ) -> str | None:
        """Render the final reply (or the agent's questions). Returns
        ``"waiting_for_answer"`` when the session should wait for a reply."""
        st.completed = True
        if st.pending_user_questions:
            await self._post_questions(st)
            return "waiting_for_answer"

        logger.debug(
            "Session %s: Completion → final message (is_error=%s)",
            st.session_key,
            is_error,
        )
        # Ensure minimum gap since last Slack update to avoid rate limits
        elapsed = time.monotonic() - st.last_update_time
        if st.last_update_time and elapsed < UPDATE_THROTTLE_SECONDS:
            await asyncio.sleep(UPDATE_THROTTLE_SECONDS - elapsed)

        final = st.accumulated_text or final_text
        if is_error:
            if st.existing_message_ts is not None:
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
        # The usage footer is metadata about the reply, not part of it. Keep
        # it out of both the upload-size decision and the uploaded file, and
        # always render it inline (it's tiny) so it survives even when the
        # body is truncated to a snippet.
        footer = "" if is_error else self._build_usage_footer(usage, session_usage)
        await self._post_final(st, final, footer)
        return None

    async def _post_questions(self, st: _RenderState) -> None:
        logger.debug(
            "Session %s: Completion with pending questions "
            "→ posting questions to Slack",
            st.session_key,
        )
        formatted = self._format_questions_for_slack(st.pending_user_questions)
        if st.message_ts:
            await self._update_message(st.channel, st.message_ts, formatted)
        elif st.say is not None:
            await st.say(text=formatted, thread_ts=st.thread_ts)

    async def _post_final(self, st: _RenderState, text: str, footer: str = "") -> None:
        """Deliver the final body, uploading it as a snippet with a short
        inline preview when it exceeds the Slack message ceiling."""
        if _utf8_len(text) > SLACK_MSG_MAX_BYTES:
            uploaded = await self._upload_snippet(st.channel, st.thread_ts, text)
            notice = (
                "\n\n_… Full response uploaded as file below._"
                if uploaded
                else "\n\n_… (response too long; upload failed — please retry)_"
            )
            preview_budget = min(
                1000,
                SLACK_MSG_MAX_BYTES - _utf8_len(notice) - _utf8_len(footer),
            )
            text = _truncate_to_bytes(text, preview_budget) + notice
        text += footer
        if st.message_ts:
            await self._update_message(st.channel, st.message_ts, text)
        elif st.say is not None:
            await st.say(text=text, thread_ts=st.thread_ts)

    async def _render_incomplete_tail(self, st: _RenderState) -> None:
        """Safety net: if the stream ended without Completion, strip residual
        tool status from the message."""
        if st.completed or not st.message_ts or not st.accumulated_text:
            return
        logger.warning(
            "Session %s: stream ended without Completion — cleaning up message",
            st.session_key,
        )
        await self._post_final(st, st.accumulated_text)

    def _build_usage_footer(
        self, usage: Usage | None, session_usage: Usage | None
    ) -> str:
        """Compose the usage footer (with a leading blank line), or '' when the
        feature is off or no usage was reported. Template wins over the default.
        """
        if not self._config.usage_report_enabled or usage is None:
            return ""
        if self._config.usage_report_template:
            body = _render_usage_template(
                self._config.usage_report_template, usage, session_usage
            )
        else:
            body = _default_usage_footer(usage, session_usage)
        return f"\n\n{_as_footnote(body)}" if body else ""

    @staticmethod
    def _format_questions_for_slack(questions: list[dict[str, Any]]) -> str:
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
            short = _fit_with_suffix(text, budget, "\n\n_… (response truncated)_")
            try:
                await self._app.client.chat_update(channel=channel, ts=ts, text=short)
                return
            except SlackApiError as retry_err:
                if retry_err.response["error"] != "msg_too_long":
                    logger.warning(
                        "Failed to update Slack message %s: %s",
                        ts,
                        retry_err.response["error"],
                    )
                    return
        logger.warning("Slack rejected update to %s at all fallback sizes", ts)

    async def _upload_snippet(self, channel: str, thread_ts: str, content: str) -> bool:
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
        self._handler = AsyncSocketModeHandler(self._app, self._config.app_token)
        logger.info("Starting Slack adapter (Socket Mode)")
        await self._handler.connect_async()

        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info(
                "Resolved bot identity: %s (%s)",
                auth.get("user"),
                self._bot_user_id,
            )
        except SlackApiError as e:
            logger.warning("Failed to resolve bot identity: %s", e.response["error"])

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

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
from agent_bridge.events import Completion, StatusUpdate, TextDelta
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# Minimum interval between Slack message updates (seconds)
UPDATE_THROTTLE_SECONDS = 1.5


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

    async def _process_message(self, event: dict, say, client) -> None:
        channel = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # Strip bot mention from text (e.g., "<@U12345> do something" → "do something")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return

        session_key = self._session_key(channel, thread_ts)
        state = self._get_state(session_key)

        # Lock guards ALL reads/writes to this session's state
        async with state.lock:
            if state.processing:
                # Session busy — replace the pending slot (keep only latest)
                context = await self._resolve_context(
                    channel, user_id, thread_ts, client
                )
                result = await say(
                    text=":hourglass: Queued — waiting for the previous task to finish...",
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

            # Session idle — mark as processing, then release lock to do real work
            state.processing = True

        # --- Processing happens outside the lock so new messages can queue ---
        try:
            context = await self._resolve_context(
                channel, user_id, thread_ts, client
            )
            result = await say(
                text=":hourglass_flowing_sand: Processing...",
                thread_ts=thread_ts,
            )
            message_ts = result["ts"]
            await self._stream_response(
                channel, message_ts, session_key, text, context
            )

            # Drain pending (re-acquire lock each iteration to read state safely)
            while True:
                async with state.lock:
                    if state.pending is None:
                        state.processing = False
                        return
                    pending = state.pending
                    state.pending = None

                await self._update_message(
                    pending.channel,
                    pending.message_ts,
                    ":hourglass_flowing_sand: Processing...",
                )
                await self._stream_response(
                    pending.channel,
                    pending.message_ts,
                    session_key,
                    pending.text,
                    pending.context,
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
        message_ts: str,
        session_key: str,
        text: str,
        context: dict[str, str],
    ) -> None:
        """Stream agent events and update the Slack message."""
        accumulated_text = ""
        tool_status = ""
        last_update_time = 0.0

        async for event_obj in self._bridge.handle_message(
            session_key=session_key,
            text=text,
            context=context,
        ):
            match event_obj:
                case TextDelta(text=chunk):
                    accumulated_text += chunk
                    now = time.monotonic()
                    if now - last_update_time >= UPDATE_THROTTLE_SECONDS:
                        await self._update_message(
                            channel, message_ts, accumulated_text + tool_status
                        )
                        last_update_time = now

                case StatusUpdate(status=status):
                    tool_status = f"\n\n_{status}_"
                    now = time.monotonic()
                    if now - last_update_time >= UPDATE_THROTTLE_SECONDS:
                        display = (
                            accumulated_text + tool_status
                            if accumulated_text
                            else tool_status
                        )
                        await self._update_message(channel, message_ts, display)
                        last_update_time = now

                case Completion(text=final_text, is_error=is_error):
                    final = final_text or accumulated_text
                    if is_error:
                        final = f":x: Error: {final}"
                    if not final:
                        final = "_No response from agent._"
                    await self._update_message(channel, message_ts, final)

    async def _delete_message(self, channel: str, ts: str) -> None:
        try:
            await self._app.client.chat_delete(channel=channel, ts=ts)
        except SlackApiError as e:
            logger.warning(
                "Failed to delete Slack message %s: %s", ts, e.response["error"]
            )

    async def _update_message(self, channel: str, ts: str, text: str) -> None:
        try:
            await self._app.client.chat_update(
                channel=channel,
                ts=ts,
                text=text,
            )
        except SlackApiError as e:
            logger.warning(
                "Failed to update Slack message %s: %s", ts, e.response["error"]
            )

    async def start(self) -> None:
        self._handler = AsyncSocketModeHandler(
            self._app, self._config.app_token
        )
        logger.info("Starting Slack adapter (Socket Mode)")
        await self._handler.connect_async()

    async def stop(self) -> None:
        if self._handler:
            await self._handler.close_async()

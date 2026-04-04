from __future__ import annotations

import asyncio
import logging
import re
import time

try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError(
        "Slack dependencies are not installed. "
        "Install them with: pip install agent-bridge[slack]"
    ) from None

from agent_bridge.bridge import Bridge
from agent_bridge.events import Completion, StatusUpdate, TextDelta
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# Minimum interval between Slack message updates (seconds)
UPDATE_THROTTLE_SECONDS = 1.5


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
        self._locks: dict[str, asyncio.Lock] = {}
        self._register_handlers()

    # --- Session key: Slack defines thread = session ---

    @staticmethod
    def _session_key(channel: str, thread_ts: str) -> str:
        return f"slack:{channel}:{thread_ts}"

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    def cleanup_stale_locks(self) -> int:
        """Remove locks for sessions that no longer exist. Returns count removed."""
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
            logger.info("Cleaned up %d stale session locks", len(stale))
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

    async def _process_message(self, event: dict, say, client) -> None:
        channel = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # Resolve user display name and channel name for context
        user_name = user_id
        try:
            user_info = await client.users_info(user=user_id)
            profile = user_info["user"]["profile"]
            user_name = (
                profile.get("display_name") or profile.get("real_name") or user_id
            )
        except SlackApiError as e:
            logger.warning(
                "Failed to resolve user name for %s: %s", user_id, e.response["error"]
            )

        channel_name = channel
        workspace_name = ""
        try:
            conv_info = await client.conversations_info(channel=channel)
            channel_name = conv_info["channel"].get("name") or channel
        except SlackApiError as e:
            logger.warning(
                "Failed to resolve channel name for %s: %s",
                channel,
                e.response["error"],
            )
        try:
            team_info = await client.team_info()
            workspace_name = team_info["team"].get("name", "")
        except SlackApiError as e:
            logger.warning(
                "Failed to resolve workspace name: %s", e.response["error"]
            )

        # Strip bot mention from text (e.g., "<@U12345> do something" → "do something")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return

        # Build context with both IDs and display names
        context = {
            "platform": "slack",
            "workspace": workspace_name,
            "channel_id": channel,
            "channel_name": channel_name,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "user_name": user_name,
        }

        session_key = self._session_key(channel, thread_ts)
        lock = self._get_lock(session_key)

        # Check if session is busy and show appropriate status
        if lock.locked():
            result = await say(
                text=":hourglass: Queued — waiting for the previous task to finish...",
                thread_ts=thread_ts,
            )
            message_ts = result["ts"]
            async with lock:
                await self._update_message(
                    channel, message_ts, ":hourglass_flowing_sand: Processing..."
                )
                await self._stream_response(
                    channel, message_ts, session_key, text, context
                )
        else:
            result = await say(
                text=":hourglass_flowing_sand: Processing...",
                thread_ts=thread_ts,
            )
            message_ts = result["ts"]
            async with lock:
                await self._stream_response(
                    channel, message_ts, session_key, text, context
                )

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

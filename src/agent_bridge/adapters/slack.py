from __future__ import annotations

import asyncio
import logging
import re
import time

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from agent_bridge.bridge import Bridge
from agent_bridge.claude.events import (
    AssistantTextEvent,
    ResultEvent,
    ToolUseEvent,
)
from agent_bridge.config import Config

logger = logging.getLogger(__name__)

# Minimum interval between Slack message updates (seconds)
UPDATE_THROTTLE_SECONDS = 1.5


class SlackAdapter:
    def __init__(self, config: Config, bridge: Bridge) -> None:
        self._config = config
        self._bridge = bridge
        self._app = AsyncApp(token=config.slack_bot_token)
        self._handler: AsyncSocketModeHandler | None = None
        self._register_handlers()

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
            user_name = profile.get("display_name") or profile.get("real_name") or user_id
        except Exception:
            logger.warning("Failed to resolve user name for %s", user_id)

        channel_name = channel
        workspace_name = ""
        try:
            conv_info = await client.conversations_info(channel=channel)
            channel_name = conv_info["channel"].get("name") or channel
        except Exception:
            logger.warning("Failed to resolve channel name for %s", channel)
        try:
            team_info = await client.team_info()
            workspace_name = team_info["team"].get("name", "")
        except Exception:
            logger.warning("Failed to resolve workspace name")

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

        session_key = self._bridge.session_key("slack", channel, thread_ts)
        lock = self._bridge.get_lock(session_key)

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
        """Stream Claude events and update the Slack message."""
        accumulated_text = ""
        tool_status = ""
        last_update_time = 0.0

        async for event_obj in self._bridge.handle_message(
            session_key=session_key,
            text=text,
            context=context,
        ):
            if isinstance(event_obj, AssistantTextEvent):
                accumulated_text += event_obj.text
                now = time.monotonic()
                if now - last_update_time >= UPDATE_THROTTLE_SECONDS:
                    await self._update_message(
                        channel, message_ts, accumulated_text + tool_status
                    )
                    last_update_time = now

            elif isinstance(event_obj, ToolUseEvent):
                tool_status = f"\n\n_Using {event_obj.tool_name}..._"
                now = time.monotonic()
                if now - last_update_time >= UPDATE_THROTTLE_SECONDS:
                    display = accumulated_text + tool_status if accumulated_text else tool_status
                    await self._update_message(channel, message_ts, display)
                    last_update_time = now

            elif isinstance(event_obj, ResultEvent):
                final_text = event_obj.result_text or accumulated_text
                if event_obj.is_error:
                    final_text = f":x: Error: {final_text}"
                if not final_text:
                    final_text = "_No response from Claude._"
                await self._update_message(channel, message_ts, final_text)

    async def _update_message(
        self, channel: str, ts: str, text: str
    ) -> None:
        try:
            await self._app.client.chat_update(
                channel=channel,
                ts=ts,
                text=text,
            )
        except Exception:
            logger.exception("Failed to update Slack message")

    async def start(self) -> None:
        self._handler = AsyncSocketModeHandler(
            self._app, self._config.slack_app_token
        )
        logger.info("Starting Slack adapter (Socket Mode)")
        await self._handler.start_async()

    async def stop(self) -> None:
        if self._handler:
            await self._handler.close_async()

from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

load_dotenv()

from agent_bridge.agents.claude.config import ClaudeConfig  # noqa: E402
from agent_bridge.agents.claude.controller import ClaudeController  # noqa: E402
from agent_bridge.bridge import Bridge  # noqa: E402
from agent_bridge.config import BridgeConfig  # noqa: E402
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter  # noqa: E402
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig  # noqa: E402
from agent_bridge.platforms.slack.adapter import SlackAdapter  # noqa: E402
from agent_bridge.platforms.slack.config import SlackConfig  # noqa: E402
from agent_bridge.protocols import PlatformAdapter  # noqa: E402
from agent_bridge.session import SessionManager  # noqa: E402

logging.basicConfig(
    level=os.environ.get("AGENT_BRIDGE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Interval for periodic maintenance (session purge, stale pending cleanup)
CLEANUP_INTERVAL_SECONDS = 3600


async def main() -> None:
    bridge_config = BridgeConfig.from_env()
    claude_config = ClaudeConfig.from_env()

    logger.info("Claude work dir: %s", claude_config.work_dir)
    logger.info("Permission mode: %s", claude_config.permission_mode)
    logger.info("Session TTL: %s hours", bridge_config.session_ttl_hours)
    logger.info("Claude timeout: %s seconds", claude_config.timeout_seconds)
    logger.info("Max concurrent sessions: %s", bridge_config.max_concurrent_sessions)

    session_manager = SessionManager(
        bridge_config.session_store_path, bridge_config.session_ttl_hours
    )
    controller = ClaudeController(claude_config)
    bridge = Bridge(
        session_manager,
        controller,
        max_concurrent=bridge_config.max_concurrent_sessions,
    )

    # --- Adapters: each independently optional ---
    slack_adapter: SlackAdapter | None = None
    try:
        slack_config = SlackConfig.from_env()
    except ValueError as e:
        logger.info("Slack adapter disabled: %s", e)
    else:
        slack_adapter = SlackAdapter(slack_config, bridge, session_manager=session_manager)

    heartbeat_config = HeartbeatConfig.from_env()
    heartbeat_adapter = (
        HeartbeatAdapter(heartbeat_config, bridge) if heartbeat_config.enabled else None
    )
    if heartbeat_adapter is not None:
        logger.info(
            "Heartbeat adapter enabled (interval=%dm)",
            heartbeat_config.interval_minutes,
        )

    adapters: list[PlatformAdapter] = [
        a for a in (slack_adapter, heartbeat_adapter) if a is not None
    ]
    if not adapters:
        raise ValueError(
            "No platform adapter configured. "
            "Set Slack tokens or AGENT_BRIDGE_HEARTBEAT_ENABLED=true."
        )

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal, stopping...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Periodic cleanup task
    async def _periodic_cleanup() -> None:
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=CLEANUP_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
            if not shutdown_event.is_set():
                purged_ids = session_manager.purge_expired()
                stale = (
                    slack_adapter.cleanup_stale_sessions()
                    if slack_adapter is not None
                    else 0
                )
                for sid in purged_ids:
                    try:
                        await controller.cleanup_session(sid)
                    except Exception:
                        logger.exception(
                            "Worktree cleanup failed for session %s", sid
                        )
                if purged_ids or stale:
                    logger.info(
                        "Cleanup: purged %d expired sessions, %d stale pending",
                        len(purged_ids),
                        stale,
                    )

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    logger.info("Starting agent-bridge...")
    try:
        for adapter in adapters:
            await adapter.start()
        logger.info("agent-bridge is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        cleanup_task.cancel()
        for adapter in adapters:
            await adapter.stop()
        logger.info("Stopped.")


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()

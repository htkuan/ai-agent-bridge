from __future__ import annotations

import asyncio
import logging
import signal

from agent_bridge.adapters.slack import SlackAdapter
from agent_bridge.bridge import Bridge
from agent_bridge.claude.controller import ClaudeController
from agent_bridge.claude.session import SessionManager
from agent_bridge.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Interval for periodic maintenance (session purge, lock cleanup)
CLEANUP_INTERVAL_SECONDS = 3600


async def main() -> None:
    config = Config.from_env()
    logger.info("Claude work dir: %s", config.claude_work_dir)
    logger.info("Permission mode: %s", config.claude_permission_mode)
    logger.info("Session TTL: %s hours", config.session_ttl_hours)
    logger.info("Claude timeout: %s seconds", config.claude_timeout_seconds)

    session_manager = SessionManager(config.session_store_path, config.session_ttl_hours)
    controller = ClaudeController(config)
    bridge = Bridge(config, session_manager, controller)
    adapter = SlackAdapter(config, bridge)

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
                purged = session_manager.purge_expired()
                stale = bridge.cleanup_stale_locks()
                if purged or stale:
                    logger.info(
                        "Cleanup: purged %d expired sessions, %d stale locks",
                        purged,
                        stale,
                    )

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    logger.info("Starting agent-bridge...")
    try:
        await adapter.start()
        logger.info("agent-bridge is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        cleanup_task.cancel()
        await adapter.stop()
        logger.info("Stopped.")


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()

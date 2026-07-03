from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from dotenv import load_dotenv

from agent_bridge.agents.registry import build_agent
from agent_bridge.bridge import Bridge
from agent_bridge.config import BridgeConfig
from agent_bridge.config_loader import load_config_source
from agent_bridge.dedupe import PromptDedupeCache
from agent_bridge.platforms.registry import build_platforms
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# Interval for periodic maintenance (session purge, stale pending cleanup)
CLEANUP_INTERVAL_SECONDS = 3600


async def main(config_path: Path | None = None) -> None:
    load_dotenv()
    source = load_config_source(config_path)

    logging.basicConfig(
        level=(source.get("AGENT_BRIDGE_LOG_LEVEL", "log_level", "INFO") or "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if source.path is not None:
        logger.info("Loaded config file: %s", source.path)

    bridge_config = BridgeConfig.from_source(source)
    logger.info("Session TTL: %s hours", bridge_config.session_ttl_hours)
    logger.info("Max concurrent sessions: %s", bridge_config.max_concurrent_sessions)

    agent_name = source.get("AGENT_BRIDGE_AGENT", "agent", "claude") or "claude"
    logger.info("Agent: %s", agent_name)
    controller = build_agent(agent_name, source)

    session_manager = SessionManager(
        bridge_config.session_store_path, bridge_config.session_ttl_hours
    )

    dedupe: PromptDedupeCache | None = None
    if bridge_config.dedupe_ttl_seconds > 0:
        dedupe = PromptDedupeCache(
            ttl_seconds=bridge_config.dedupe_ttl_seconds,
            max_entries=bridge_config.dedupe_max_entries,
            simhash_threshold=bridge_config.dedupe_simhash_threshold,
        )
        logger.info(
            "Prompt dedupe enabled (ttl=%.0fs, max_entries=%d, simhash_threshold=%d)",
            bridge_config.dedupe_ttl_seconds,
            bridge_config.dedupe_max_entries,
            bridge_config.dedupe_simhash_threshold,
        )

    bridge = Bridge(
        session_manager,
        controller,
        max_concurrent=bridge_config.max_concurrent_sessions,
        dedupe=dedupe,
    )

    adapters = build_platforms(source, bridge, session_manager)
    if not adapters:
        raise ValueError(
            "No platform adapter configured. "
            "Set Slack tokens, enable the heartbeat, or provide a YAML config "
            "(see docs/configuration.md)."
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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=CLEANUP_INTERVAL_SECONDS)
            if not shutdown_event.is_set():
                purged_ids = session_manager.purge_expired()
                # Optional adapter hook: adapters that keep per-session state
                # (e.g. Slack) expose cleanup_stale_sessions() -> int.
                stale = 0
                for adapter in adapters:
                    hook = getattr(adapter, "cleanup_stale_sessions", None)
                    if callable(hook):
                        stale += hook()
                for sid in purged_ids:
                    bridge.forget_session_usage(sid)
                    try:
                        await controller.cleanup_session(sid)
                    except Exception:
                        logger.exception("Agent session cleanup failed for session %s", sid)
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
    parser = argparse.ArgumentParser(
        prog="agent-bridge",
        description="Bridge between chat platforms and AI agents",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config file (overrides AGENT_BRIDGE_CONFIG)",
    )
    args = parser.parse_args()
    asyncio.run(main(config_path=args.config))


if __name__ == "__main__":
    main_sync()

"""Entry point: wires platform adapters + bridge + agent, then supervises them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.bridge.config import DedupeConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.protocols import PlatformAdapter
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.config import AppConfig
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.slack.adapter import SlackAdapter

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _build_dedupe(config: DedupeConfig) -> PromptDedupeCache | None:
    if not config.enabled:
        return None
    logger.info(
        "Prompt dedupe enabled (ttl=%.0fs, max_entries=%d, simhash_threshold=%d)",
        config.ttl_seconds,
        config.max_entries,
        config.simhash_threshold,
    )
    return PromptDedupeCache(config)


def _build_adapters(
    config: AppConfig, bridge: Bridge, session_manager: SessionManager
) -> tuple[SlackAdapter | None, list[PlatformAdapter]]:
    """Construct every configured adapter — each independently optional.

    Returns the slack adapter separately because periodic cleanup needs it.
    """
    slack_adapter: SlackAdapter | None = None
    if config.slack is None:
        logger.info("Slack adapter disabled: no Slack tokens configured")
    else:
        slack_adapter = SlackAdapter(
            config.slack, bridge, session_manager=session_manager
        )

    heartbeat_adapter: HeartbeatAdapter | None = None
    if config.heartbeat is not None:
        heartbeat_adapter = HeartbeatAdapter(config.heartbeat, bridge)
        logger.info(
            "Heartbeat adapter enabled (interval=%dm)",
            config.heartbeat.interval_minutes,
        )

    adapters: list[PlatformAdapter] = [
        a for a in (slack_adapter, heartbeat_adapter) if a is not None
    ]
    if not adapters:
        raise ValueError(
            "No platform adapter configured. "
            "Set Slack tokens or AGENT_BRIDGE_HEARTBEAT_ENABLED=true."
        )
    return slack_adapter, adapters


async def _periodic_cleanup(
    interval_seconds: float,
    shutdown_event: asyncio.Event,
    session_manager: SessionManager,
    slack_adapter: SlackAdapter | None,
    bridge: Bridge,
    controller: ClaudeController,
) -> None:
    while not shutdown_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        if not shutdown_event.is_set():
            purged_ids = session_manager.purge_expired()
            stale = (
                slack_adapter.cleanup_stale_sessions()
                if slack_adapter is not None
                else 0
            )
            for sid in purged_ids:
                bridge.forget_session_usage(sid)
                try:
                    await controller.cleanup_session(sid)
                except Exception:
                    logger.exception("Worktree cleanup failed for session %s", sid)
            if purged_ids or stale:
                logger.info(
                    "Cleanup: purged %d expired sessions, %d stale pending",
                    len(purged_ids),
                    stale,
                )


async def run(config: AppConfig) -> None:
    """Build the whole system from ``config`` and supervise it until shutdown."""
    logger.info("Claude work dir: %s", config.claude.work_dir)
    logger.info("Permission mode: %s", config.claude.permission_mode)
    logger.info("Session TTL: %s hours", config.bridge.session.ttl_hours)
    logger.info("Claude timeout: %s seconds", config.claude.timeout_seconds)
    logger.info(
        "Max concurrent sessions: %s", config.bridge.router.max_concurrent_sessions
    )

    session_manager = SessionManager(config.bridge.session)
    controller = ClaudeController(config.claude)
    bridge = Bridge(
        config.bridge.router,
        session_manager,
        controller,
        dedupe=_build_dedupe(config.bridge.dedupe),
    )
    slack_adapter, adapters = _build_adapters(config, bridge, session_manager)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal, stopping...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    cleanup_task = asyncio.create_task(
        _periodic_cleanup(
            config.cleanup_interval_seconds,
            shutdown_event,
            session_manager,
            slack_adapter,
            bridge,
            controller,
        )
    )

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


async def main() -> None:
    config = AppConfig.from_env()
    _configure_logging(config.log_level)
    await run(config)


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()

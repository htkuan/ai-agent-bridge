"""Entry point: wires platform adapters + bridge + agent, then supervises them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agent_bridge.agents.claude.controller import ClaudeController
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.config import DedupeConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.protocols import AgentController, PlatformAdapter
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager
from agent_bridge.config import AppConfig
from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
from agent_bridge.platforms.slack.adapter import SlackAdapter

if TYPE_CHECKING:
    from agent_bridge.server.http_server import HttpServer

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


def _build_http_server(config: AppConfig) -> HttpServer | None:
    if config.http is None:
        return None
    # Imported lazily: fastapi/uvicorn are optional deps ([http]) that a
    # deployment without the HTTP server never needs installed.
    from agent_bridge.server.http_server import HttpServer

    return HttpServer(config.http)


def _build_adapters(
    config: AppConfig,
    bridge: Bridge,
    session_manager: SessionManager,
    http_server: HttpServer | None = None,
) -> list[PlatformAdapter]:
    """Construct every configured adapter — each independently optional."""
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

    webhook_adapter: PlatformAdapter | None = None
    if config.webhook is not None:
        # AppConfig._validate guarantees this for env-built configs; guard
        # again for programmatically assembled ones.
        if http_server is None:
            raise ValueError(
                "The webhook platform needs the HTTP server: set "
                "AGENT_BRIDGE_HTTP_ENABLED=true"
            )
        from agent_bridge.platforms.webhook.adapter import WebhookAdapter

        adapter = WebhookAdapter(config.webhook, bridge)
        http_server.include_router(adapter.router)
        webhook_adapter = adapter
        logger.info("Webhook adapter enabled (POST /platforms/webhook/v1/messages)")

    adapters: list[PlatformAdapter] = [
        a for a in (slack_adapter, heartbeat_adapter, webhook_adapter) if a is not None
    ]
    if not adapters:
        raise ValueError(
            "No platform adapter configured. Set Slack tokens, "
            "AGENT_BRIDGE_HEARTBEAT_ENABLED=true, or "
            "AGENT_BRIDGE_WEBHOOK_ENABLED=true."
        )
    return adapters


async def _periodic_cleanup(
    interval_seconds: float,
    shutdown_event: asyncio.Event,
    session_manager: SessionManager,
    adapters: list[PlatformAdapter],
    bridge: Bridge,
    controllers: Sequence[AgentController],
) -> None:
    while not shutdown_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        if not shutdown_event.is_set():
            purged_ids = session_manager.purge_expired()
            stale = 0
            for adapter in adapters:
                stale += await adapter.cleanup()
            for sid in purged_ids:
                bridge.forget_session_usage(sid)
                # A purged/orphaned id doesn't say which controller owned it —
                # cleanup on all of them; misses are cheap no-ops.
                for controller in controllers:
                    try:
                        await controller.cleanup_session(sid)
                    except Exception:
                        logger.exception("Agent cleanup failed for session %s", sid)
            if purged_ids or stale:
                logger.info(
                    "Cleanup: purged %d expired sessions, %d stale pending",
                    len(purged_ids),
                    stale,
                )


def _check_agent_prerequisites(config: AppConfig) -> None:
    """Startup-only probe of the world the config points at. The probes speak
    in AGENT_BRIDGE_* terms; point at the profile actually holding the bad
    value."""
    config.claude.check_prerequisites()
    for name, profile in config.claude_profiles.items():
        try:
            profile.check_prerequisites()
        except ValueError as e:
            raise ValueError(f"claude.profiles.{name}: {e}") from e
    for name, pi_profile in config.pi_profiles.items():
        try:
            pi_profile.check_prerequisites()
        except ValueError as e:
            raise ValueError(f"pi.profiles.{name}: {e}") from e
    for name, codex_profile in config.codex_profiles.items():
        try:
            codex_profile.check_prerequisites()
        except ValueError as e:
            raise ValueError(f"codex.profiles.{name}: {e}") from e


def _log_startup_config(config: AppConfig) -> None:
    logger.info("Claude work dir: %s", config.claude.work_dir)
    logger.info("Permission mode: %s", config.claude.permission_mode)
    for name, profile in config.claude_profiles.items():
        logger.info(
            "Claude profile %s: work_dir=%s, permission_mode=%s, model=%s",
            name,
            profile.work_dir,
            profile.permission_mode,
            profile.model or "(default)",
        )
    for name, pi_profile in config.pi_profiles.items():
        logger.info(
            "Pi profile %s: work_dir=%s, provider=%s, model=%s, tools=%s",
            name,
            pi_profile.work_dir,
            pi_profile.provider or "(default)",
            pi_profile.model or "(default)",
            ",".join(pi_profile.tools) or "(all)",
        )
    for name, codex_profile in config.codex_profiles.items():
        logger.info(
            "Codex profile %s: work_dir=%s, sandbox=%s, model=%s",
            name,
            codex_profile.work_dir,
            codex_profile.sandbox_mode,
            codex_profile.model or "(default)",
        )
    logger.info("Default agent: %s", config.default_agent or "(env-built claude)")
    logger.info("Session TTL: %s hours", config.bridge.session.ttl_hours)
    logger.info("Claude timeout: %s seconds", config.claude.timeout_seconds)
    logger.info(
        "Max concurrent sessions: %s", config.bridge.router.max_concurrent_sessions
    )


def _build_named_controllers(config: AppConfig) -> dict[str, AgentController]:
    # One routing namespace across agent types; AppConfig validated the names
    # don't collide.
    named: dict[str, AgentController] = {
        name: ClaudeController(profile)
        for name, profile in config.claude_profiles.items()
    }
    named.update(
        {name: PiController(profile) for name, profile in config.pi_profiles.items()}
    )
    named.update(
        {
            name: CodexController(profile)
            for name, profile in config.codex_profiles.items()
        }
    )
    return named


async def run(config: AppConfig) -> None:
    """Build the whole system from ``config`` and supervise it until shutdown."""
    # Value checks already ran on construction; the probes run here rather
    # than in ``from_env`` so a programmatically built config gets the same
    # fail-fast guarantee.
    _check_agent_prerequisites(config)
    _log_startup_config(config)

    session_manager = SessionManager(config.bridge.session)
    controller = ClaudeController(config.claude)
    named_controllers = _build_named_controllers(config)
    bridge = Bridge(
        config.bridge.router,
        session_manager,
        controller,
        dedupe=_build_dedupe(config.bridge.dedupe),
        named_controllers=named_controllers,
        default_agent=config.default_agent,
    )
    http_server = _build_http_server(config)
    adapters = _build_adapters(config, bridge, session_manager, http_server)

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
            adapters,
            bridge,
            [controller, *named_controllers.values()],
        )
    )

    logger.info("Starting agent-bridge...")
    try:
        for adapter in adapters:
            await adapter.start()
        # Server last: it only accepts traffic once every adapter is ready.
        if http_server is not None:
            await http_server.start()
        logger.info("agent-bridge is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        cleanup_task.cancel()
        # Mirror of startup: stop accepting new requests, then the adapters.
        if http_server is not None:
            await http_server.stop()
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

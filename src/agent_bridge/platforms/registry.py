from __future__ import annotations

import logging
from collections.abc import Callable

from agent_bridge.bridge import Bridge
from agent_bridge.config_loader import ConfigSource
from agent_bridge.protocols import PlatformAdapter
from agent_bridge.session import SessionManager

logger = logging.getLogger(__name__)

# A builder returns None when its platform is not configured/enabled.
type PlatformBuilder = Callable[
    [ConfigSource, Bridge, SessionManager], PlatformAdapter | None
]


def _build_slack(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> PlatformAdapter | None:
    from agent_bridge.platforms.slack.config import SlackConfig

    try:
        config = SlackConfig.from_source(source)
    except ValueError as e:
        logger.info("Slack adapter disabled: %s", e)
        return None
    # Imported lazily so the optional slack-bolt dependency is only required
    # when Slack is actually configured.
    from agent_bridge.platforms.slack.adapter import SlackAdapter

    return SlackAdapter(config, bridge, session_manager=session_manager)


def _build_telegram(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> PlatformAdapter | None:
    from agent_bridge.platforms.telegram.config import TelegramConfig

    try:
        config = TelegramConfig.from_source(source)
    except ValueError as e:
        logger.info("Telegram adapter disabled: %s", e)
        return None
    # Imported lazily so the optional aiohttp dependency is only required
    # when Telegram is actually configured.
    from agent_bridge.platforms.telegram.adapter import TelegramAdapter

    return TelegramAdapter(config, bridge, session_manager=session_manager)


def _build_line(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> PlatformAdapter | None:
    from agent_bridge.platforms.line.config import LineConfig

    try:
        config = LineConfig.from_source(source)
    except ValueError as e:
        logger.info("LINE adapter disabled: %s", e)
        return None
    # Imported lazily so the optional aiohttp dependency is only required
    # when LINE is actually configured.
    from agent_bridge.platforms.line.adapter import LineAdapter

    return LineAdapter(config, bridge, session_manager=session_manager)


def _build_api(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> PlatformAdapter | None:
    from agent_bridge.platforms.api.config import ApiConfig

    config = ApiConfig.from_source(source)
    # Disabled is the normal state (explicit opt-in, like heartbeat) — no log.
    if not config.enabled:
        return None
    # Imported lazily so the optional aiohttp dependency is only required
    # when the API server is actually enabled.
    from agent_bridge.platforms.api.adapter import ApiAdapter

    return ApiAdapter(config, bridge, session_manager=session_manager)


def _build_heartbeat(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> PlatformAdapter | None:
    from agent_bridge.platforms.heartbeat.adapter import HeartbeatAdapter
    from agent_bridge.platforms.heartbeat.config import HeartbeatConfig

    config = HeartbeatConfig.from_source(source)
    if not config.enabled:
        return None
    logger.info("Heartbeat adapter enabled (interval=%dm)", config.interval_minutes)
    return HeartbeatAdapter(config, bridge)


PLATFORM_BUILDERS: dict[str, PlatformBuilder] = {
    "slack": _build_slack,
    "telegram": _build_telegram,
    "line": _build_line,
    "api": _build_api,
    "heartbeat": _build_heartbeat,
}


def build_platforms(
    source: ConfigSource, bridge: Bridge, session_manager: SessionManager
) -> list[PlatformAdapter]:
    adapters: list[PlatformAdapter] = []
    for name, builder in PLATFORM_BUILDERS.items():
        adapter = builder(source, bridge, session_manager)
        if adapter is not None:
            logger.info("Platform adapter active: %s", name)
            adapters.append(adapter)
    return adapters

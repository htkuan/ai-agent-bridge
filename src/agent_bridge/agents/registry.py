from __future__ import annotations

import logging
from collections.abc import Callable

from agent_bridge.config_loader import ConfigSource
from agent_bridge.protocols import AgentController

logger = logging.getLogger(__name__)

type AgentBuilder = Callable[[ConfigSource], AgentController]


def _build_claude(source: ConfigSource) -> AgentController:
    from agent_bridge.agents.claude.config import ClaudeConfig
    from agent_bridge.agents.claude.controller import ClaudeController

    config = ClaudeConfig.from_source(source)
    logger.info("Claude work dir: %s", config.work_dir)
    logger.info("Permission mode: %s", config.permission_mode)
    logger.info("Claude timeout: %s seconds", config.timeout_seconds)
    return ClaudeController(config)


AGENT_BUILDERS: dict[str, AgentBuilder] = {
    "claude": _build_claude,
}


def build_agent(name: str, source: ConfigSource) -> AgentController:
    builder = AGENT_BUILDERS.get(name)
    if builder is None:
        available = ", ".join(sorted(AGENT_BUILDERS))
        raise ValueError(f"Unknown agent {name!r}. Available agents: {available}")
    return builder(source)

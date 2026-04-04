from __future__ import annotations

import asyncio
import logging

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


async def main() -> None:
    config = Config.from_env()
    logger.info("Claude work dir: %s", config.claude_work_dir)
    logger.info("Permission mode: %s", config.claude_permission_mode)

    session_manager = SessionManager(config.session_store_path, config.session_ttl_hours)
    controller = ClaudeController(config)
    bridge = Bridge(config, session_manager, controller)
    adapter = SlackAdapter(config, bridge)

    logger.info("Starting agent-bridge...")
    await adapter.start()


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()

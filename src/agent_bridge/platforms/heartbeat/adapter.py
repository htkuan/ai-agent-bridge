from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    StatusUpdate,
    TextDelta,
    UserQuestion,
)
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig

logger = logging.getLogger(__name__)


class HeartbeatAdapter:
    def __init__(self, config: HeartbeatConfig, bridge: Bridge) -> None:
        self._config = config
        self._bridge = bridge
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        last_run = self._read_last_run()
        interval = timedelta(minutes=self._config.interval_minutes)
        now = _now()

        if last_run is None or (now - last_run) >= interval:
            initial_delay = 0.0
            logger.info(
                "Heartbeat: firing on startup (last_run=%s, interval=%dm)",
                last_run, self._config.interval_minutes,
            )
        else:
            initial_delay = ((last_run + interval) - now).total_seconds()
            logger.info(
                "Heartbeat: next fire in %.1fs (last_run=%s, interval=%dm)",
                initial_delay, last_run, self._config.interval_minutes,
            )

        self._task = asyncio.create_task(self._run_loop(initial_delay))

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self, initial_delay: float) -> None:
        if initial_delay > 0 and await self._sleep_or_stop(initial_delay):
            return

        interval_seconds = self._config.interval_minutes * 60
        while not self._stopping.is_set():
            await self._fire_once()
            if await self._sleep_or_stop(interval_seconds):
                return

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep for ``seconds``. Return True if stop was signalled (caller should exit)."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _fire_once(self) -> None:
        fired_at = _now()
        # Unique key per tick → SessionManager always creates a fresh session.
        session_key = f"heartbeat:tick:{fired_at.isoformat()}"
        logger.info(
            "Heartbeat tick: session_key=%s prompt=%r",
            session_key, self._config.prompt,
        )

        context = {"source": "heartbeat", "fired_at": fired_at.isoformat()}
        try:
            async for event in self._bridge.handle_message(
                session_key=session_key,
                text=self._config.prompt,
                context=context,
                system_prompt=self._build_system_prompt(fired_at),
                resumable=False,
            ):
                self._log_event(session_key, event)
        except Exception:
            logger.exception("Heartbeat tick failed for session %s", session_key)
        finally:
            self._write_last_run(fired_at)

    @staticmethod
    def _build_system_prompt(fired_at: datetime) -> str:
        return (
            "This is a heartbeat session: a scheduled tick fired at a fixed "
            "interval to give the agent a chance to do periodic work. "
            f"Fired at {fired_at.isoformat()}."
        )

    def _log_event(self, session_key: str, event: BridgeEvent) -> None:
        match event:
            case Processing():
                logger.info("Heartbeat %s: processing", session_key)
            case TextDelta(text=chunk):
                logger.debug("Heartbeat %s: text +%d chars", session_key, len(chunk))
            case StatusUpdate(status=status, detail=detail):
                logger.info(
                    "Heartbeat %s: status=%s detail=%s",
                    session_key, status, detail,
                )
            case UserQuestion(questions=questions):
                # No human is on the other end of a heartbeat — surface loudly.
                logger.warning(
                    "Heartbeat %s: agent asked %d question(s) but no human can answer: %s",
                    session_key, len(questions), questions,
                )
            case Completion(text=text, is_error=is_error, cost_usd=cost, duration_ms=duration):
                if is_error:
                    logger.error(
                        "Heartbeat %s: completion error cost=$%.4f duration=%dms text=%s",
                        session_key, cost, duration, text,
                    )
                else:
                    logger.info(
                        "Heartbeat %s: completion cost=$%.4f duration=%dms",
                        session_key, cost, duration,
                    )
                    logger.info("Heartbeat %s: final reply: %s", session_key, text)

    def _read_last_run(self) -> datetime | None:
        if not self._config.state_path.exists():
            return None
        try:
            data = json.loads(self._config.state_path.read_text())
            value = data.get("last_run")
            if not value:
                return None
            return datetime.fromisoformat(value)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Heartbeat: failed to read state file: %s", e)
            return None

    def _write_last_run(self, when: datetime) -> None:
        try:
            self._config.state_path.parent.mkdir(parents=True, exist_ok=True)
            self._config.state_path.write_text(
                json.dumps({"last_run": when.isoformat()}, indent=2)
            )
        except OSError as e:
            logger.error("Heartbeat: failed to write state file: %s", e)


def _now() -> datetime:
    return datetime.now(timezone.utc)

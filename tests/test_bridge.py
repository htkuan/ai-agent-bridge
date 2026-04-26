import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_bridge.bridge import Bridge
from agent_bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    TextDelta,
)
from agent_bridge.session import SessionManager


class FakeController:
    """A controller whose run() yields a single TextDelta then Completion.

    ``delay`` lets tests simulate slow agent work.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.last_system_prompt: str | None = None
        self.last_context: dict[str, str] | None = None

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        self.calls.append(prompt)
        self.last_system_prompt = system_prompt
        self.last_context = context
        if self.delay:
            await asyncio.sleep(self.delay)
        yield TextDelta(text=f"echo:{prompt}")
        yield Completion(text=f"echo:{prompt}")


@pytest.fixture()
def session_mgr(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "sessions.json")


# --- Basic event flow ---


@pytest.mark.asyncio
async def test_handle_message_emits_processing_and_completion(session_mgr):
    bridge = Bridge(session_mgr, FakeController(), max_concurrent=5)

    events = [e async for e in bridge.handle_message("key1", "hello")]

    types = [type(e) for e in events]
    assert types == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_handle_message_forwards_system_prompt_to_controller(session_mgr):
    controller = FakeController()
    bridge = Bridge(session_mgr, controller, max_concurrent=5)

    async for _ in bridge.handle_message(
        "key1", "hello", context={"a": "b"}, system_prompt="be helpful"
    ):
        pass

    assert controller.last_system_prompt == "be helpful"
    assert controller.last_context == {"a": "b"}


@pytest.mark.asyncio
async def test_handle_message_forwards_none_system_prompt_when_omitted(session_mgr):
    controller = FakeController()
    bridge = Bridge(session_mgr, controller, max_concurrent=5)

    async for _ in bridge.handle_message("key1", "hello"):
        pass

    assert controller.last_system_prompt is None


@pytest.mark.asyncio
async def test_resumable_default_writes_to_session_store(session_mgr):
    """Default resumable=True path: SessionManager records the key on disk."""
    bridge = Bridge(session_mgr, FakeController(), max_concurrent=5)

    async for _ in bridge.handle_message("slack:C1:t1", "hi"):
        pass

    assert session_mgr.get("slack:C1:t1") is not None


@pytest.mark.asyncio
async def test_resumable_false_does_not_touch_session_store(session_mgr):
    """resumable=False: bridge mints an ephemeral UUID, store stays empty."""
    bridge = Bridge(session_mgr, FakeController(), max_concurrent=5)

    async for _ in bridge.handle_message(
        "heartbeat:tick:2026-01-01", "hi", resumable=False
    ):
        pass

    # Key never reaches the store
    assert session_mgr.get("heartbeat:tick:2026-01-01") is None
    assert session_mgr.list_sessions() == {}


@pytest.mark.asyncio
async def test_resumable_false_passes_uuid_session_id_to_controller(session_mgr):
    """Even without a stored mapping, the agent still gets a valid session_id."""
    controller = FakeController()
    bridge = Bridge(session_mgr, controller, max_concurrent=5)

    captured: list[str] = []

    async def capturing_run(session_id, prompt, is_new, context=None, system_prompt=None):
        captured.append(session_id)
        async for e in FakeController().run(
            session_id, prompt, is_new, context=context, system_prompt=system_prompt
        ):
            yield e

    bridge._controller = type("C", (), {"run": staticmethod(capturing_run)})()

    async for _ in bridge.handle_message("k", "hi", resumable=False):
        pass

    assert len(captured) == 1
    # UUID-shaped (36 chars with hyphens)
    assert len(captured[0]) == 36 and captured[0].count("-") == 4


@pytest.mark.asyncio
async def test_resumable_false_repeated_calls_yield_distinct_session_ids(session_mgr):
    """Two calls with the same key + resumable=False must NOT share state."""
    seen: list[str] = []

    class CapturingController:
        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            seen.append(session_id)
            yield Completion(text="ok")

    bridge = Bridge(session_mgr, CapturingController(), max_concurrent=5)

    async for _ in bridge.handle_message("same-key", "first", resumable=False):
        pass
    async for _ in bridge.handle_message("same-key", "second", resumable=False):
        pass

    assert len(seen) == 2
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_capacity_full_rejects_immediately(session_mgr):
    """When all slots are taken, handle_message yields an error Completion immediately."""
    controller = FakeController(delay=0.3)
    bridge = Bridge(session_mgr, controller, max_concurrent=1)

    # Occupy the single slot
    task1 = asyncio.create_task(_collect(bridge.handle_message("key1", "first")))
    await asyncio.sleep(0.05)

    # Second message should be rejected immediately (no Queued, no waiting)
    events = [e async for e in bridge.handle_message("key2", "rejected")]

    assert len(events) == 1
    assert isinstance(events[0], Completion)
    assert events[0].is_error is True
    assert events[0].metadata["error_code"] == "capacity_full"

    # First task should still complete normally
    events1 = await task1
    types1 = [type(e) for e in events1]
    assert types1 == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_slot_available_after_release(session_mgr):
    """After a task finishes and releases its slot, the next message succeeds."""
    controller = FakeController(delay=0.1)
    bridge = Bridge(session_mgr, controller, max_concurrent=1)

    # First message occupies and releases the slot
    events1 = [e async for e in bridge.handle_message("key1", "first")]
    assert [type(e) for e in events1] == [Processing, TextDelta, Completion]

    # Second message should succeed (slot is free)
    events2 = [e async for e in bridge.handle_message("key2", "second")]
    assert [type(e) for e in events2] == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_semaphore_released_after_error(session_mgr):
    """Semaphore is released even when the controller raises."""

    class FailingController:
        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            raise RuntimeError("boom")
            yield  # noqa: RET503 — make this an async generator

    bridge = Bridge(session_mgr, FailingController(), max_concurrent=1)

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in bridge.handle_message("key1", "fail"):
            pass

    # The finally block should have released the semaphore
    assert not bridge._sem.locked()


@pytest.mark.asyncio
async def test_concurrent_up_to_max(session_mgr):
    """Multiple messages up to max_concurrent all get Processing immediately."""
    controller = FakeController(delay=0.1)
    bridge = Bridge(session_mgr, controller, max_concurrent=3)

    tasks = [
        asyncio.create_task(_collect(bridge.handle_message(f"key{i}", f"msg{i}")))
        for i in range(3)
    ]
    results = await asyncio.gather(*tasks)

    for events in results:
        types = [type(e) for e in events]
        assert types[0] is Processing


@pytest.mark.asyncio
async def test_exceeding_max_concurrent_rejects_extra(session_mgr):
    """Messages beyond max_concurrent are rejected while earlier ones succeed."""
    controller = FakeController(delay=0.3)
    bridge = Bridge(session_mgr, controller, max_concurrent=2)

    # Start 2 tasks that occupy both slots
    task1 = asyncio.create_task(_collect(bridge.handle_message("key1", "a")))
    task2 = asyncio.create_task(_collect(bridge.handle_message("key2", "b")))
    await asyncio.sleep(0.05)

    # Third message should be rejected
    events3 = [e async for e in bridge.handle_message("key3", "c")]
    assert len(events3) == 1
    assert events3[0].is_error is True

    # First two should complete successfully
    results = await asyncio.gather(task1, task2)
    for events in results:
        assert any(isinstance(e, Processing) for e in events)
        assert any(isinstance(e, Completion) and not e.is_error for e in events)


async def _collect(aiter) -> list:
    return [e async for e in aiter]

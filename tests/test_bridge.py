import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_bridge.bridge import Bridge
from agent_bridge.dedupe import PromptDedupeCache
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


# --- Dedupe ---


@pytest.mark.asyncio
async def test_dedupe_disabled_when_cache_is_none(session_mgr):
    """No dedupe instance → identical prompts run twice (legacy behaviour)."""
    controller = FakeController()
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=None)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass
    async for _ in bridge.handle_message("slack:C1:t2", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_in_flight_skips_controller(session_mgr):
    """Second identical prompt while the first is running short-circuits."""
    controller = FakeController(delay=0.2)
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=cache)

    task1 = asyncio.create_task(
        _collect(
            bridge.handle_message(
                "slack:C1:t1",
                "alert",
                context={"thread_permalink": "https://w.slack.com/A/p1"},
            )
        )
    )
    await asyncio.sleep(0.05)  # let task1 enter the controller

    events2 = [
        e
        async for e in bridge.handle_message(
            "slack:C1:t2",
            "alert",
            context={"thread_permalink": "https://w.slack.com/A/p2"},
        )
    ]
    assert len(events2) == 1
    assert isinstance(events2[0], Completion)
    assert events2[0].metadata["dedupe"] == "in_flight"
    assert "https://w.slack.com/A/p1" in events2[0].text

    await task1
    assert controller.calls == ["alert"]  # only the first call actually ran


@pytest.mark.asyncio
async def test_dedupe_recent_hit_after_completion(session_mgr):
    """After the first run completes, a third identical prompt still hits cache."""
    controller = FakeController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message(
        "slack:C1:t1", "alert", context={"thread_permalink": "https://link"}
    ):
        pass

    events2 = [
        e async for e in bridge.handle_message("slack:C1:t2", "alert")
    ]
    assert len(events2) == 1
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert "https://link" in events2[0].text
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_dedupe_different_scope_does_not_collide(session_mgr):
    """Same prompt in different channels → both run (channel-scoped)."""
    controller = FakeController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass
    async for _ in bridge.handle_message("slack:C2:t1", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_normalizes_sender_tag_prefix(session_mgr):
    """Same alert under different Slack display names still collides."""
    controller = FakeController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message(
        "slack:C1:t1", "[Sentry (U1)]: [caac-web] Error: X"
    ):
        pass
    events2 = [
        e
        async for e in bridge.handle_message(
            "slack:C1:t2", "[Sentry Bot (U1)]: [caac-web] Error: X"
        )
    ]
    assert len(events2) == 1
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert controller.calls[0].startswith("[Sentry")


@pytest.mark.asyncio
async def test_dedupe_controller_exception_releases_cache_slot(session_mgr):
    """Controller crash must not block the next retry for the full TTL."""

    class FlakyController:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
                yield  # noqa: RET503 — async generator marker
            yield Completion(text="recovered")

    controller = FlakyController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=5, dedupe=cache)

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in bridge.handle_message("slack:C1:t1", "alert"):
            pass

    # Retry must reach the controller, not be blocked by a stale cache entry.
    events = [e async for e in bridge.handle_message("slack:C1:t2", "alert")]
    assert any(isinstance(e, Completion) and e.text == "recovered" for e in events)
    assert controller.calls == 2


@pytest.mark.asyncio
async def test_dedupe_capacity_full_releases_cache_slot(session_mgr):
    """If the bridge rejects on capacity, the dedupe entry must be cleared too."""
    controller = FakeController(delay=0.3)
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_mgr, controller, max_concurrent=1, dedupe=cache)

    occupier = asyncio.create_task(
        _collect(bridge.handle_message("slack:C1:t1", "first"))
    )
    await asyncio.sleep(0.05)

    # Second message is on a different prompt and different channel — gets
    # rejected by capacity gate. Cache entry must be freed so a retry works.
    events = [e async for e in bridge.handle_message("slack:C2:t1", "second")]
    assert events[0].metadata["error_code"] == "capacity_full"

    await occupier
    # Now capacity is free — retry should NOT be dedupe-blocked.
    events2 = [e async for e in bridge.handle_message("slack:C2:t2", "second")]
    assert any(isinstance(e, Completion) and not e.is_error for e in events2)
    assert "second" in controller.calls

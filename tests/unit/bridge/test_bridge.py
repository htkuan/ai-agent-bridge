import asyncio

import pytest

from agent_bridge.bridge import Bridge
from agent_bridge.dedupe import PromptDedupeCache
from agent_bridge.events import (
    Completion,
    Processing,
    TextDelta,
)
from tests.helpers import FakeAgentController, collect_events


# --- Basic event flow ---


@pytest.mark.asyncio
async def test_handle_message_emits_processing_and_completion(session_manager):
    bridge = Bridge(session_manager, FakeAgentController(), max_concurrent=5)

    events = [e async for e in bridge.handle_message("key1", "hello")]

    types = [type(e) for e in events]
    assert types == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_handle_message_forwards_system_prompt_to_controller(session_manager):
    controller = FakeAgentController()
    bridge = Bridge(session_manager, controller, max_concurrent=5)

    async for _ in bridge.handle_message(
        "key1", "hello", context={"a": "b"}, system_prompt="be helpful"
    ):
        pass

    assert controller.last_system_prompt == "be helpful"
    assert controller.last_context == {"a": "b"}


@pytest.mark.asyncio
async def test_handle_message_forwards_none_system_prompt_when_omitted(session_manager):
    controller = FakeAgentController()
    bridge = Bridge(session_manager, controller, max_concurrent=5)

    async for _ in bridge.handle_message("key1", "hello"):
        pass

    assert controller.last_system_prompt is None


@pytest.mark.asyncio
async def test_resumable_default_writes_to_session_store(session_manager):
    """Default resumable=True path: SessionManager records the key on disk."""
    bridge = Bridge(session_manager, FakeAgentController(), max_concurrent=5)

    async for _ in bridge.handle_message("slack:C1:t1", "hi"):
        pass

    assert session_manager.get("slack:C1:t1") is not None


@pytest.mark.asyncio
async def test_resumable_false_does_not_touch_session_store(session_manager):
    """resumable=False: bridge mints an ephemeral UUID, store stays empty."""
    bridge = Bridge(session_manager, FakeAgentController(), max_concurrent=5)

    async for _ in bridge.handle_message(
        "heartbeat:tick:2026-01-01", "hi", resumable=False
    ):
        pass

    # Key never reaches the store
    assert session_manager.get("heartbeat:tick:2026-01-01") is None
    assert session_manager.list_sessions() == {}


@pytest.mark.asyncio
async def test_resumable_false_passes_uuid_session_id_to_controller(session_manager):
    """Even without a stored mapping, the agent still gets a valid session_id."""
    controller = FakeAgentController()
    bridge = Bridge(session_manager, controller, max_concurrent=5)

    async for _ in bridge.handle_message("k", "hi", resumable=False):
        pass

    assert len(controller.runs) == 1
    session_id = controller.runs[0].session_id
    # UUID-shaped (36 chars with hyphens)
    assert len(session_id) == 36 and session_id.count("-") == 4


@pytest.mark.asyncio
async def test_resumable_false_repeated_calls_yield_distinct_session_ids(session_manager):
    """Two calls with the same key + resumable=False must NOT share state."""
    seen: list[str] = []

    class CapturingController:
        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            seen.append(session_id)
            yield Completion(text="ok")

    bridge = Bridge(session_manager, CapturingController(), max_concurrent=5)

    async for _ in bridge.handle_message("same-key", "first", resumable=False):
        pass
    async for _ in bridge.handle_message("same-key", "second", resumable=False):
        pass

    assert len(seen) == 2
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_capacity_full_rejects_immediately(session_manager):
    """When all slots are taken, handle_message yields an error Completion immediately."""
    controller = FakeAgentController(delay=0.3)
    bridge = Bridge(session_manager, controller, max_concurrent=1)

    # Occupy the single slot
    task1 = asyncio.create_task(collect_events(bridge.handle_message("key1", "first")))
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
async def test_slot_available_after_release(session_manager):
    """After a task finishes and releases its slot, the next message succeeds."""
    controller = FakeAgentController(delay=0.1)
    bridge = Bridge(session_manager, controller, max_concurrent=1)

    # First message occupies and releases the slot
    events1 = [e async for e in bridge.handle_message("key1", "first")]
    assert [type(e) for e in events1] == [Processing, TextDelta, Completion]

    # Second message should succeed (slot is free)
    events2 = [e async for e in bridge.handle_message("key2", "second")]
    assert [type(e) for e in events2] == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_semaphore_released_after_error(session_manager):
    """Semaphore is released even when the controller raises."""

    class FailingController:
        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            raise RuntimeError("boom")
            yield  # noqa: RET503 — make this an async generator

    bridge = Bridge(session_manager, FailingController(), max_concurrent=1)

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in bridge.handle_message("key1", "fail"):
            pass

    # The finally block should have released the semaphore
    assert not bridge._sem.locked()


@pytest.mark.asyncio
async def test_concurrent_up_to_max(session_manager):
    """Multiple messages up to max_concurrent all get Processing immediately."""
    controller = FakeAgentController(delay=0.1)
    bridge = Bridge(session_manager, controller, max_concurrent=3)

    tasks = [
        asyncio.create_task(collect_events(bridge.handle_message(f"key{i}", f"msg{i}")))
        for i in range(3)
    ]
    results = await asyncio.gather(*tasks)

    for events in results:
        types = [type(e) for e in events]
        assert types[0] is Processing


@pytest.mark.asyncio
async def test_exceeding_max_concurrent_rejects_extra(session_manager):
    """Messages beyond max_concurrent are rejected while earlier ones succeed."""
    controller = FakeAgentController(delay=0.3)
    bridge = Bridge(session_manager, controller, max_concurrent=2)

    # Start 2 tasks that occupy both slots
    task1 = asyncio.create_task(collect_events(bridge.handle_message("key1", "a")))
    task2 = asyncio.create_task(collect_events(bridge.handle_message("key2", "b")))
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


# --- Dedupe integration ---


@pytest.mark.asyncio
async def test_dedupe_disabled_when_cache_is_none(session_manager):
    """No dedupe instance → identical prompts run twice (legacy behaviour)."""
    controller = FakeAgentController()
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=None)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass
    async for _ in bridge.handle_message("slack:C1:t2", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_in_flight_skips_controller(session_manager):
    """Second identical prompt while the first is running short-circuits."""
    controller = FakeAgentController(delay=0.2)
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    task1 = asyncio.create_task(
        collect_events(bridge.handle_message("slack:C1:t1", "alert"))
    )
    await asyncio.sleep(0.05)

    events2 = [e async for e in bridge.handle_message("slack:C1:t2", "alert")]
    assert len(events2) == 1
    assert isinstance(events2[0], Completion)
    assert events2[0].metadata["dedupe"] == "in_flight"
    assert events2[0].metadata["first_session_key"] == "slack:C1:t1"

    await task1
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_dedupe_recent_hit_after_completion(session_manager):
    """After the first run completes, a third identical prompt still hits cache."""
    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass

    events2 = [e async for e in bridge.handle_message("slack:C1:t2", "alert")]
    assert len(events2) == 1
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_dedupe_url_variants_collapse_via_canonicalize(session_manager):
    """URL-only differences canonicalize to the same key → dedupe."""
    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message(
        "slack:C1:t1", "Error fetching https://x.com/orgs/123"
    ):
        pass
    events2 = [
        e
        async for e in bridge.handle_message(
            "slack:C1:t2", "Error fetching https://x.com/orgs/9999"
        )
    ]
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert controller.calls == ["Error fetching https://x.com/orgs/123"]


@pytest.mark.asyncio
async def test_dedupe_simhash_threshold_catches_near_match(session_manager):
    """With SimHash threshold, similar (not identical) alerts collapse."""
    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0, simhash_threshold=20)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message(
        "slack:C1:t1",
        "Zodios: Invalid response from endpoint 'get api/v1/orgs/members'",
    ):
        pass
    events2 = [
        e
        async for e in bridge.handle_message(
            "slack:C1:t2",
            "Zodios: Invalid response from endpoint 'get api/v1/orgs/messages'",
        )
    ]
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert len(controller.calls) == 1


@pytest.mark.asyncio
async def test_dedupe_different_scope_does_not_collide(session_manager):
    """Same prompt in different channels → both run (channel-scoped)."""
    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass
    async for _ in bridge.handle_message("slack:C2:t1", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_skipped_for_non_resumable(session_manager):
    """resumable=False (heartbeat) bypasses dedupe even with identical prompts."""
    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message(
        "heartbeat:tick:T1", "scheduled work", resumable=False
    ):
        pass
    async for _ in bridge.handle_message(
        "heartbeat:tick:T2", "scheduled work", resumable=False
    ):
        pass

    assert controller.calls == ["scheduled work", "scheduled work"]


@pytest.mark.asyncio
async def test_dedupe_controller_is_error_releases_cache_slot(session_manager):
    """A Completion(is_error=True) yield must free the dedupe slot for retry.

    Most real-world controller failures (timeout, non-zero exit, API error)
    are reported as is_error=True rather than raising — they must not lock
    out retries the same way a clean success would.
    """

    class ErroringController:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, session_id, prompt, is_new, context=None, system_prompt=None):
            self.calls += 1
            if self.calls == 1:
                yield Completion(text="timeout", is_error=True)
                return
            yield Completion(text="ok")

    controller = ErroringController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    events1 = [e async for e in bridge.handle_message("slack:C1:t1", "alert")]
    assert any(isinstance(e, Completion) and e.is_error for e in events1)

    # Retry must reach the controller — the failed first run is not held in cache.
    events2 = [e async for e in bridge.handle_message("slack:C1:t2", "alert")]
    assert any(isinstance(e, Completion) and not e.is_error and e.text == "ok" for e in events2)
    assert controller.calls == 2


@pytest.mark.asyncio
async def test_dedupe_controller_exception_releases_cache_slot(session_manager):
    """Controller crash must not block the next retry for the full TTL."""

    class FlakyController:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
                yield  # noqa: RET503 — async generator marker
            yield Completion(text="recovered")

    controller = FlakyController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in bridge.handle_message("slack:C1:t1", "alert"):
            pass

    events = [e async for e in bridge.handle_message("slack:C1:t2", "alert")]
    assert any(isinstance(e, Completion) and e.text == "recovered" for e in events)
    assert controller.calls == 2


@pytest.mark.asyncio
async def test_dedupe_capacity_full_releases_cache_slot(session_manager):
    """If the bridge rejects on capacity, the dedupe entry must be cleared too."""
    controller = FakeAgentController(delay=0.3)
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=1, dedupe=cache)

    occupier = asyncio.create_task(
        collect_events(bridge.handle_message("slack:C1:t1", "first"))
    )
    await asyncio.sleep(0.05)

    # Different prompt in different scope — gets rejected by capacity gate.
    events = [e async for e in bridge.handle_message("slack:C2:t1", "second")]
    assert events[0].metadata["error_code"] == "capacity_full"

    await occupier
    # Now capacity is free — retry must NOT be dedupe-blocked.
    events2 = [e async for e in bridge.handle_message("slack:C2:t2", "second")]
    assert any(isinstance(e, Completion) and not e.is_error for e in events2)
    assert "second" in controller.calls


@pytest.mark.asyncio
async def test_dedupe_hit_logs_dedupe_hit_line(session_manager, caplog):
    """The skipped branch must emit a `dedupe_hit` log line for observability."""
    import logging

    controller = FakeAgentController()
    cache = PromptDedupeCache(ttl_seconds=60.0)
    bridge = Bridge(session_manager, controller, max_concurrent=5, dedupe=cache)

    async for _ in bridge.handle_message("slack:C1:t1", "alert"):
        pass

    with caplog.at_level(logging.INFO, logger="agent_bridge.bridge"):
        async for _ in bridge.handle_message("slack:C1:t2", "alert"):
            pass

    matches = [r for r in caplog.records if "dedupe_hit" in r.getMessage()]
    assert len(matches) == 1
    msg = matches[0].getMessage()
    assert "scope=slack:C1" in msg
    assert "state=recent_hit" in msg
    assert "match=exact" in msg
    assert "first_session=slack:C1:t1" in msg


# --- Usage accumulation ---


def _usage_meta(**kw):
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "num_turns": 0,
        "duration_api_ms": 0,
    }
    base.update(kw)
    return base


def _usage_controller(cost_usd: float = 0.01) -> FakeAgentController:
    """Yields one Completion carrying a fixed usage report per call."""
    return FakeAgentController(
        events=[
            Completion(
                text="ok",
                cost_usd=cost_usd,
                duration_ms=100,
                metadata={
                    "usage": _usage_meta(input_tokens=10, output_tokens=5, num_turns=1)
                },
            )
        ]
    )


async def _completion(bridge, key, text="hi", **kw):
    return [
        e
        async for e in bridge.handle_message(key, text, **kw)
        if isinstance(e, Completion)
    ][0]


@pytest.mark.asyncio
async def test_completion_carries_turn_usage(session_manager):
    bridge = Bridge(session_manager, _usage_controller(cost_usd=0.02), max_concurrent=5)
    c = await _completion(bridge, "slack:c:t")
    assert c.usage is not None
    assert c.usage.input_tokens == 10
    assert c.usage.cost_usd == 0.02
    assert c.usage.duration_ms == 100


@pytest.mark.asyncio
async def test_session_usage_accumulates_across_turns(session_manager):
    bridge = Bridge(session_manager, _usage_controller(cost_usd=0.01), max_concurrent=5)

    c1 = await _completion(bridge, "slack:c:t", "first")
    assert c1.session_usage is not None
    assert c1.session_usage.input_tokens == 10
    assert c1.session_usage.cost_usd == pytest.approx(0.01)

    c2 = await _completion(bridge, "slack:c:t", "second")
    assert c2.session_usage.input_tokens == 20
    assert c2.session_usage.num_turns == 2
    assert c2.session_usage.cost_usd == pytest.approx(0.02)
    # turn usage stays per-turn, not cumulative
    assert c2.usage.input_tokens == 10


@pytest.mark.asyncio
async def test_session_usage_none_when_started_mid_session(session_manager):
    # Pre-create the session so the bridge resumes it without tracking its start.
    session_manager.get_or_create("slack:c:t")
    bridge = Bridge(session_manager, _usage_controller(), max_concurrent=5)

    c = await _completion(bridge, "slack:c:t")
    assert c.usage is not None  # turn usage still present
    assert c.session_usage is None  # partial → hidden


@pytest.mark.asyncio
async def test_session_usage_none_for_non_resumable(session_manager):
    bridge = Bridge(session_manager, _usage_controller(), max_concurrent=5)
    c = await _completion(bridge, "heartbeat:tick:1", resumable=False)
    assert c.usage is not None
    assert c.session_usage is None


@pytest.mark.asyncio
async def test_forget_session_usage_drops_running_total(session_manager):
    bridge = Bridge(session_manager, _usage_controller(), max_concurrent=5)

    c1 = await _completion(bridge, "slack:c:t")
    assert c1.session_usage is not None

    sid = session_manager.get("slack:c:t")
    bridge.forget_session_usage(sid)

    c2 = await _completion(bridge, "slack:c:t")
    assert c2.session_usage is None  # accumulator reset → now untracked

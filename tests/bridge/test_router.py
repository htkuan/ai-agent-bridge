import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_bridge.bridge.config import DedupeConfig, RouterConfig, SessionConfig
from agent_bridge.bridge.dedupe import PromptDedupeCache
from agent_bridge.bridge.events import (
    BridgeEvent,
    Completion,
    Processing,
    TextDelta,
)
from agent_bridge.bridge.request import BridgeRequest
from agent_bridge.bridge.router import Bridge
from agent_bridge.bridge.session import SessionManager


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


def _msg(bridge: Bridge, session_key: str, text: str, **kw):
    """One turn through the bridge; kwargs map onto BridgeRequest fields."""
    return bridge.handle_message(
        BridgeRequest(session_key=session_key, text=text, **kw)
    )


@pytest.fixture()
def session_mgr(tmp_path: Path) -> SessionManager:
    return SessionManager(SessionConfig(store_path=tmp_path / "sessions.json"))


# --- Basic event flow ---


@pytest.mark.asyncio
async def test_handle_message_emits_processing_and_completion(session_mgr):
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, FakeController()
    )

    events = [e async for e in _msg(bridge, "key1", "hello")]

    types = [type(e) for e in events]
    assert types == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_handle_message_forwards_system_prompt_to_controller(session_mgr):
    controller = FakeController()
    bridge = Bridge(RouterConfig(max_concurrent_sessions=5), session_mgr, controller)

    async for _ in _msg(
        bridge, "key1", "hello", context={"a": "b"}, system_prompt="be helpful"
    ):
        pass

    assert controller.last_system_prompt == "be helpful"
    assert controller.last_context == {"a": "b"}


@pytest.mark.asyncio
async def test_handle_message_forwards_none_system_prompt_when_omitted(session_mgr):
    controller = FakeController()
    bridge = Bridge(RouterConfig(max_concurrent_sessions=5), session_mgr, controller)

    async for _ in _msg(bridge, "key1", "hello"):
        pass

    assert controller.last_system_prompt is None


@pytest.mark.asyncio
async def test_resumable_default_writes_to_session_store(session_mgr):
    """Default resumable=True path: SessionManager records the key on disk."""
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, FakeController()
    )

    async for _ in _msg(bridge, "slack:C1:t1", "hi"):
        pass

    assert await session_mgr.get("slack:C1:t1") is not None


@pytest.mark.asyncio
async def test_resumable_false_does_not_touch_session_store(session_mgr):
    """resumable=False: bridge mints an ephemeral UUID, store stays empty."""
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, FakeController()
    )

    async for _ in _msg(bridge, "heartbeat:tick:2026-01-01", "hi", resumable=False):
        pass

    # Key never reaches the store
    assert await session_mgr.get("heartbeat:tick:2026-01-01") is None
    assert await session_mgr.list_sessions() == {}


@pytest.mark.asyncio
async def test_resumable_false_passes_uuid_session_id_to_controller(session_mgr):
    """Even without a stored mapping, the agent still gets a valid session_id."""
    captured: list[str] = []

    class CapturingController:
        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            captured.append(session_id)
            yield Completion(text="ok")

    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, CapturingController()
    )

    async for _ in _msg(bridge, "k", "hi", resumable=False):
        pass

    assert len(captured) == 1
    # UUID-shaped (36 chars with hyphens)
    assert len(captured[0]) == 36 and captured[0].count("-") == 4


@pytest.mark.asyncio
async def test_resumable_false_repeated_calls_yield_distinct_session_ids(session_mgr):
    """Two calls with the same key + resumable=False must NOT share state."""
    seen: list[str] = []

    class CapturingController:
        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            seen.append(session_id)
            yield Completion(text="ok")

    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, CapturingController()
    )

    async for _ in _msg(bridge, "same-key", "first", resumable=False):
        pass
    async for _ in _msg(bridge, "same-key", "second", resumable=False):
        pass

    assert len(seen) == 2
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_capacity_full_rejects_immediately(session_mgr):
    """When all slots are taken, handle_message yields an error Completion
    immediately."""
    controller = FakeController(delay=0.3)
    bridge = Bridge(RouterConfig(max_concurrent_sessions=1), session_mgr, controller)

    # Occupy the single slot
    task1 = asyncio.create_task(_collect(_msg(bridge, "key1", "first")))
    await asyncio.sleep(0.05)

    # Second message should be rejected immediately (no Queued, no waiting)
    events = [e async for e in _msg(bridge, "key2", "rejected")]

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
    bridge = Bridge(RouterConfig(max_concurrent_sessions=1), session_mgr, controller)

    # First message occupies and releases the slot
    events1 = [e async for e in _msg(bridge, "key1", "first")]
    assert [type(e) for e in events1] == [Processing, TextDelta, Completion]

    # Second message should succeed (slot is free)
    events2 = [e async for e in _msg(bridge, "key2", "second")]
    assert [type(e) for e in events2] == [Processing, TextDelta, Completion]


@pytest.mark.asyncio
async def test_semaphore_released_after_error(session_mgr):
    """Semaphore is released even when the controller raises."""

    class FailingController:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            yield Completion(text="ok")

    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=1), session_mgr, FailingController()
    )

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in _msg(bridge, "key1", "fail"):
            pass

    # The finally block released the slot: with max_concurrent=1, the next
    # turn only succeeds if the crashed one gave its lease back.
    events = [e async for e in _msg(bridge, "key2", "after")]
    assert isinstance(events[0], Processing)


@pytest.mark.asyncio
async def test_concurrent_up_to_max(session_mgr):
    """Multiple messages up to max_concurrent all get Processing immediately."""
    controller = FakeController(delay=0.1)
    bridge = Bridge(RouterConfig(max_concurrent_sessions=3), session_mgr, controller)

    tasks = [
        asyncio.create_task(_collect(_msg(bridge, f"key{i}", f"msg{i}")))
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
    bridge = Bridge(RouterConfig(max_concurrent_sessions=2), session_mgr, controller)

    # Start 2 tasks that occupy both slots
    task1 = asyncio.create_task(_collect(_msg(bridge, "key1", "a")))
    task2 = asyncio.create_task(_collect(_msg(bridge, "key2", "b")))
    await asyncio.sleep(0.05)

    # Third message should be rejected
    events3 = [e async for e in _msg(bridge, "key3", "c")]
    assert len(events3) == 1
    assert events3[0].is_error is True

    # First two should complete successfully
    results = await asyncio.gather(task1, task2)
    for events in results:
        assert any(isinstance(e, Processing) for e in events)
        assert any(isinstance(e, Completion) and not e.is_error for e in events)


async def _collect(aiter) -> list:
    return [e async for e in aiter]


# --- Dedupe integration ---


@pytest.mark.asyncio
async def test_dedupe_disabled_when_cache_is_none(session_mgr):
    """No dedupe instance → identical prompts run twice (legacy behaviour)."""
    controller = FakeController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=None
    )

    async for _ in _msg(bridge, "slack:C1:t1", "alert"):
        pass
    async for _ in _msg(bridge, "slack:C1:t2", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_in_flight_skips_controller(session_mgr):
    """Second identical prompt while the first is running short-circuits."""
    controller = FakeController(delay=0.2)
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    task1 = asyncio.create_task(_collect(_msg(bridge, "slack:C1:t1", "alert")))
    await asyncio.sleep(0.05)

    events2 = [e async for e in _msg(bridge, "slack:C1:t2", "alert")]
    assert len(events2) == 1
    assert isinstance(events2[0], Completion)
    assert events2[0].metadata["dedupe"] == "in_flight"
    assert events2[0].metadata["first_session_key"] == "slack:C1:t1"

    await task1
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_dedupe_recent_hit_after_completion(session_mgr):
    """After the first run completes, a third identical prompt still hits cache."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(bridge, "slack:C1:t1", "alert"):
        pass

    events2 = [e async for e in _msg(bridge, "slack:C1:t2", "alert")]
    assert len(events2) == 1
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_dedupe_url_variants_collapse_via_canonicalize(session_mgr):
    """URL-only differences canonicalize to the same key → dedupe."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(bridge, "slack:C1:t1", "Error fetching https://x.com/orgs/123"):
        pass
    events2 = [
        e
        async for e in _msg(
            bridge, "slack:C1:t2", "Error fetching https://x.com/orgs/9999"
        )
    ]
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert controller.calls == ["Error fetching https://x.com/orgs/123"]


@pytest.mark.asyncio
async def test_dedupe_simhash_threshold_catches_near_match(session_mgr):
    """With SimHash threshold, similar (not identical) alerts collapse."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0, simhash_threshold=20))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(
        bridge,
        "slack:C1:t1",
        "Zodios: Invalid response from endpoint 'get api/v1/orgs/members'",
    ):
        pass
    events2 = [
        e
        async for e in _msg(
            bridge,
            "slack:C1:t2",
            "Zodios: Invalid response from endpoint 'get api/v1/orgs/messages'",
        )
    ]
    assert events2[0].metadata["dedupe"] == "recent_hit"
    assert len(controller.calls) == 1


@pytest.mark.asyncio
async def test_dedupe_different_scope_does_not_collide(session_mgr):
    """Same prompt in different channels → both run (channel-scoped)."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(bridge, "slack:C1:t1", "alert"):
        pass
    async for _ in _msg(bridge, "slack:C2:t1", "alert"):
        pass

    assert controller.calls == ["alert", "alert"]


@pytest.mark.asyncio
async def test_dedupe_skipped_for_non_resumable(session_mgr):
    """resumable=False (heartbeat) bypasses dedupe even with identical prompts."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(bridge, "heartbeat:tick:T1", "scheduled work", resumable=False):
        pass
    async for _ in _msg(bridge, "heartbeat:tick:T2", "scheduled work", resumable=False):
        pass

    assert controller.calls == ["scheduled work", "scheduled work"]


@pytest.mark.asyncio
async def test_dedupe_controller_is_error_releases_cache_slot(session_mgr):
    """A Completion(is_error=True) yield must free the dedupe slot for retry.

    Most real-world controller failures (timeout, non-zero exit, API error)
    are reported as is_error=True rather than raising — they must not lock
    out retries the same way a clean success would.
    """

    class ErroringController:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            self.calls += 1
            if self.calls == 1:
                yield Completion(text="timeout", is_error=True)
                return
            yield Completion(text="ok")

    controller = ErroringController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    events1 = [e async for e in _msg(bridge, "slack:C1:t1", "alert")]
    assert any(isinstance(e, Completion) and e.is_error for e in events1)

    # Retry must reach the controller — the failed first run is not held in cache.
    events2 = [e async for e in _msg(bridge, "slack:C1:t2", "alert")]
    assert any(
        isinstance(e, Completion) and not e.is_error and e.text == "ok" for e in events2
    )
    assert controller.calls == 2


@pytest.mark.asyncio
async def test_dedupe_controller_exception_releases_cache_slot(session_mgr):
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
                yield
            yield Completion(text="recovered")

    controller = FlakyController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in _msg(bridge, "slack:C1:t1", "alert"):
            pass

    events = [e async for e in _msg(bridge, "slack:C1:t2", "alert")]
    assert any(isinstance(e, Completion) and e.text == "recovered" for e in events)
    assert controller.calls == 2


@pytest.mark.asyncio
async def test_dedupe_capacity_full_releases_cache_slot(session_mgr):
    """If the bridge rejects on capacity, the dedupe entry must be cleared too."""
    controller = FakeController(delay=0.3)
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=1), session_mgr, controller, dedupe=cache
    )

    occupier = asyncio.create_task(_collect(_msg(bridge, "slack:C1:t1", "first")))
    await asyncio.sleep(0.05)

    # Different prompt in different scope — gets rejected by capacity gate.
    events = [e async for e in _msg(bridge, "slack:C2:t1", "second")]
    assert events[0].metadata["error_code"] == "capacity_full"

    await occupier
    # Now capacity is free — retry must NOT be dedupe-blocked.
    events2 = [e async for e in _msg(bridge, "slack:C2:t2", "second")]
    assert any(isinstance(e, Completion) and not e.is_error for e in events2)
    assert "second" in controller.calls


@pytest.mark.asyncio
async def test_dedupe_hit_logs_dedupe_hit_line(session_mgr, caplog):
    """The skipped branch must emit a `dedupe_hit` log line for observability."""
    import logging

    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    async for _ in _msg(bridge, "slack:C1:t1", "alert"):
        pass

    with caplog.at_level(logging.INFO, logger="agent_bridge.bridge.middleware.dedupe"):
        async for _ in _msg(bridge, "slack:C1:t2", "alert"):
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


class UsageController:
    """Yields one Completion carrying a fixed usage report per call."""

    def __init__(self, cost_usd: float = 0.01) -> None:
        self.cost_usd = cost_usd

    async def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]:
        yield Completion(
            text="ok",
            cost_usd=self.cost_usd,
            duration_ms=100,
            metadata={
                "usage": _usage_meta(input_tokens=10, output_tokens=5, num_turns=1)
            },
        )


async def _completion(bridge, key, text="hi", **kw):
    completions = [
        e async for e in _msg(bridge, key, text, **kw) if isinstance(e, Completion)
    ]
    return completions[0]


@pytest.mark.asyncio
async def test_completion_carries_turn_usage(session_mgr):
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        UsageController(cost_usd=0.02),
    )
    c = await _completion(bridge, "slack:c:t")
    assert c.usage is not None
    assert c.usage.input_tokens == 10
    assert c.usage.cost_usd == 0.02
    assert c.usage.duration_ms == 100


@pytest.mark.asyncio
async def test_session_usage_accumulates_across_turns(session_mgr):
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        UsageController(cost_usd=0.01),
    )

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
async def test_session_usage_none_when_started_mid_session(session_mgr):
    # Pre-create the session so the bridge resumes it without tracking its start.
    await session_mgr.get_or_create("slack:c:t")
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, UsageController()
    )

    c = await _completion(bridge, "slack:c:t")
    assert c.usage is not None  # turn usage still present
    assert c.session_usage is None  # partial → hidden


@pytest.mark.asyncio
async def test_session_usage_none_for_non_resumable(session_mgr):
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, UsageController()
    )
    c = await _completion(bridge, "heartbeat:tick:1", resumable=False)
    assert c.usage is not None
    assert c.session_usage is None


@pytest.mark.asyncio
async def test_forget_session_usage_drops_running_total(session_mgr):
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, UsageController()
    )

    c1 = await _completion(bridge, "slack:c:t")
    assert c1.session_usage is not None

    sid = await session_mgr.get("slack:c:t")
    bridge.forget_session_usage(sid)

    c2 = await _completion(bridge, "slack:c:t")
    assert c2.session_usage is None  # accumulator reset → now untracked


# --- Named agent routing ---


@pytest.mark.asyncio
async def test_agent_routes_to_named_controller(session_mgr):
    default = FakeController()
    research = FakeController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        default,
        named_controllers={"research": research},
    )

    async for _ in _msg(bridge, "slack:C1:t1", "hi", agent="research"):
        pass

    assert research.calls == ["hi"]
    assert default.calls == []


@pytest.mark.asyncio
async def test_agent_none_routes_to_default_controller(session_mgr):
    default = FakeController()
    research = FakeController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        default,
        named_controllers={"research": research},
    )

    async for _ in _msg(bridge, "slack:C1:t1", "hi"):
        pass

    assert default.calls == ["hi"]
    assert research.calls == []


@pytest.mark.asyncio
async def test_default_agent_resolves_none_to_named_controller(session_mgr):
    default = FakeController()
    fast = FakeController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        default,
        named_controllers={"fast": fast},
        default_agent="fast",
    )

    async for _ in _msg(bridge, "slack:C1:t1", "hi"):
        pass

    assert fast.calls == ["hi"]
    assert default.calls == []


@pytest.mark.asyncio
async def test_default_agent_sticks_sessions_to_the_resolved_name(session_mgr):
    """``agent=None`` resolves before the session lookup, so the session
    records the actual profile: an explicit ``agent="fast"`` resumes it, and
    a deployment that drops the default abandons it like any remap."""
    fast = FakeController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        FakeController(),
        named_controllers={"fast": fast},
        default_agent="fast",
    )
    async for _ in _msg(bridge, "slack:C1:t1", "hi"):
        pass
    first_id = await session_mgr.get("slack:C1:t1")
    assert first_id is not None

    async for _ in _msg(bridge, "slack:C1:t1", "again", agent="fast"):
        pass
    assert await session_mgr.get("slack:C1:t1") == first_id

    # Same key through a bridge whose default reverted to the env-built
    # controller: recorded agent "fast" ≠ None → fresh session.
    reverted = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        FakeController(),
        named_controllers={"fast": fast},
    )
    async for _ in _msg(reverted, "slack:C1:t1", "back"):
        pass
    assert await session_mgr.get("slack:C1:t1") != first_id


@pytest.mark.asyncio
async def test_unknown_default_agent_yields_error_completion(session_mgr):
    # Startup validation makes this unreachable for env-built configs; the
    # guard covers programmatically assembled ones.
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        FakeController(),
        default_agent="ghost",
    )
    events = [e async for e in _msg(bridge, "slack:C1:t1", "hi")]
    assert len(events) == 1
    completion = events[0]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.metadata["error_code"] == "unknown_agent"


@pytest.mark.asyncio
async def test_unknown_agent_rejects_before_any_side_effect(session_mgr):
    """The rejection must precede the dedupe claim, session mint, and
    semaphore — a misconfigured channel must not poison any shared state."""
    controller = FakeController()
    cache = PromptDedupeCache(DedupeConfig(ttl_seconds=60.0))
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5), session_mgr, controller, dedupe=cache
    )

    events = [e async for e in _msg(bridge, "slack:C1:t1", "alert", agent="nope")]

    assert len(events) == 1
    assert isinstance(events[0], Completion)
    assert events[0].is_error is True
    assert events[0].metadata["error_code"] == "unknown_agent"
    assert await session_mgr.list_sessions() == {}
    assert controller.calls == []

    # No dedupe slot was claimed: the same prompt through a valid route runs.
    async for _ in _msg(bridge, "slack:C1:t2", "alert"):
        pass
    assert controller.calls == ["alert"]


@pytest.mark.asyncio
async def test_same_key_different_agent_gets_fresh_session(session_mgr):
    seen: list[tuple[str, bool]] = []

    class CapturingController:
        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            seen.append((session_id, is_new))
            yield Completion(text="ok")

    controller = CapturingController()
    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        controller,
        named_controllers={"research": controller},
    )

    async for _ in _msg(bridge, "slack:C1:t1", "a"):
        pass
    async for _ in _msg(bridge, "slack:C1:t1", "b", agent="research"):
        pass

    # The remap minted a fresh session instead of resuming the default one.
    assert [is_new for _, is_new in seen] == [True, True]
    assert seen[0][0] != seen[1][0]


@pytest.mark.asyncio
async def test_same_key_same_agent_resumes_session(session_mgr):
    seen: list[tuple[str, bool]] = []

    class CapturingController:
        async def run(
            self, session_id, prompt, is_new, context=None, system_prompt=None
        ):
            seen.append((session_id, is_new))
            yield Completion(text="ok")

    bridge = Bridge(
        RouterConfig(max_concurrent_sessions=5),
        session_mgr,
        FakeController(),
        named_controllers={"research": CapturingController()},
    )

    async for _ in _msg(bridge, "slack:C1:t1", "a", agent="research"):
        pass
    async for _ in _msg(bridge, "slack:C1:t1", "b", agent="research"):
        pass

    assert [is_new for _, is_new in seen] == [True, False]
    assert seen[0][0] == seen[1][0]

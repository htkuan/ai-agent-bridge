from __future__ import annotations

import time

import pytest

from agent_bridge.dedupe import PromptDedupeCache, normalize_prompt


def test_normalize_strips_slack_tag_prefix():
    raw = "[Sentry (U016J3JGHRC)]: [caac-web] Error: …"
    assert normalize_prompt(raw) == "[caac-web] Error: …"


def test_normalize_no_tag_unchanged():
    assert normalize_prompt("just a question") == "just a question"


def test_normalize_strips_surrounding_whitespace():
    assert normalize_prompt("  hello\n") == "hello"


def test_normalize_only_strips_leading_tag_not_inner_brackets():
    # The leading [Sentry...]: prefix should go, but inner [caac-web] tag must stay.
    raw = "[Sentry]: [caac-web] error happened"
    assert normalize_prompt(raw) == "[caac-web] error happened"


def test_lookup_or_claim_miss_then_hit():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    assert cache.lookup_or_claim("slack:C1", "alert", "https://link") is None
    hit = cache.lookup_or_claim("slack:C1", "alert", None)
    assert hit is not None
    assert hit.first_thread_link == "https://link"
    assert hit.completed_at is None  # still in-flight


def test_lookup_or_claim_normalizes_text():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("slack:C1", "[Sentry]: alert payload", "link-a")
    # Same payload from a renamed sender still collides.
    hit = cache.lookup_or_claim("slack:C1", "[NewSentryName]: alert payload", "link-b")
    assert hit is not None
    assert hit.first_thread_link == "link-a"


def test_different_scope_does_not_collide():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("slack:C1", "alert", "link1")
    # Same text, different channel — must NOT be flagged.
    assert cache.lookup_or_claim("slack:C2", "alert", "link2") is None


def test_mark_completed_transitions_entry_state():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("s", "t", "link")
    cache.mark_completed("s", "t")
    hit = cache.lookup_or_claim("s", "t", "link")
    assert hit is not None
    assert hit.completed_at is not None


def test_mark_failed_removes_entry_so_retry_proceeds():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("s", "t", "link")
    cache.mark_failed("s", "t")
    # Next call is a fresh claim, not a hit.
    assert cache.lookup_or_claim("s", "t", "link2") is None


def test_expired_entry_treated_as_miss(monkeypatch):
    cache = PromptDedupeCache(ttl_seconds=1.0)
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    cache.lookup_or_claim("s", "t", None)
    fake_now[0] = 1000.5  # still inside the window
    assert cache.lookup_or_claim("s", "t", None) is not None

    fake_now[0] = 1002.0  # past TTL
    assert cache.lookup_or_claim("s", "t", None) is None


def test_lru_eviction_at_capacity():
    cache = PromptDedupeCache(ttl_seconds=60.0, max_entries=3)
    cache.lookup_or_claim("s", "a", None)
    cache.lookup_or_claim("s", "b", None)
    cache.lookup_or_claim("s", "c", None)
    # Touch "a" so "b" becomes the oldest.
    cache.lookup_or_claim("s", "a", None)
    # New entry pushes capacity over → "b" evicted (oldest non-touched).
    cache.lookup_or_claim("s", "d", None)
    assert cache.lookup_or_claim("s", "b", None) is None  # gone
    # "a" survived because LRU touch saved it.
    assert cache.lookup_or_claim("s", "a", None) is not None


def test_invalid_construction_raises():
    with pytest.raises(ValueError):
        PromptDedupeCache(ttl_seconds=0)
    with pytest.raises(ValueError):
        PromptDedupeCache(ttl_seconds=10, max_entries=0)

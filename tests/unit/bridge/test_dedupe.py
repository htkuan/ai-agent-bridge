from __future__ import annotations

import time

import pytest

from agent_bridge.dedupe import (
    PromptDedupeCache,
    canonicalize,
    hamming,
    simhash,
)

# --- canonicalize ---


def test_canonicalize_strips_url():
    assert canonicalize("Error fetching https://x.com/orgs/1/msgs/2?x=1") == "Error fetching <URL>"


def test_canonicalize_two_urls_collide():
    a = canonicalize("Error fetching https://x.com/orgs/123/msgs/456")
    b = canonicalize("Error fetching https://x.com/orgs/789/msgs/abc")
    assert a == b


def test_canonicalize_strips_uuid():
    text = "user 7c3f9e21-a4b2-1234-5678-abcdef012345 not found"
    assert canonicalize(text) == "user <UUID> not found"


def test_canonicalize_strips_iso_timestamp():
    assert canonicalize("at 2026-05-26T02:43:17Z something") == "at <TS> something"


def test_canonicalize_strips_email():
    assert canonicalize("contact foo@bar.com please") == "contact <EMAIL> please"


def test_canonicalize_strips_long_hex():
    # 16 lowercase hex chars
    assert canonicalize("sha 1a2b3c4d5e6f7890 done") == "sha <HEX> done"


def test_canonicalize_strips_ipv4():
    assert canonicalize("from 192.168.0.1 port 80") == "from <IP> port 80"


def test_canonicalize_strips_long_numbers():
    # 4+ digit runs become <NUM>; shorter ones stay
    assert canonicalize("id=98765 took 421ms") == "id=<NUM> took 421ms"


def test_canonicalize_collapses_whitespace():
    assert canonicalize("a   b\n\tc") == "a b c"


# --- simhash ---


def test_simhash_identical_strings_same_fingerprint():
    assert simhash("hello world") == simhash("hello world")


def test_simhash_similar_strings_closer_than_unrelated():
    a = simhash("Error: Zodios: Invalid response from endpoint members")
    b = simhash("Error: Zodios: Invalid response from endpoint messages")
    c = simhash("Completely unrelated heartbeat tick at midnight zzz")
    # similar pair is closer than unrelated
    assert hamming(a, b) < hamming(a, c)


def test_simhash_empty_string_returns_zero():
    assert simhash("") == 0


def test_hamming_basic():
    assert hamming(0b1010, 0b1001) == 2
    assert hamming(0, 0) == 0


# --- cache: exact / canonical match ---


def test_lookup_or_claim_miss_then_hit():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    r1 = cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t1")
    assert r1.hit is None
    r2 = cache.lookup_or_claim("slack:C1", "alert", "slack:C1:t2")
    assert r2.hit is not None
    assert r2.hit.first_session_key == "slack:C1:t1"
    assert r2.hamming == 0


def test_canonical_match_collapses_url_variants():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("s", "Error at https://a.com/1", "s:t1")
    r = cache.lookup_or_claim("s", "Error at https://b.com/9999", "s:t2")
    assert r.hit is not None
    assert r.hit.first_session_key == "s:t1"


def test_different_scope_does_not_collide():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    cache.lookup_or_claim("slack:C1", "alert", "k1")
    r = cache.lookup_or_claim("slack:C2", "alert", "k2")
    assert r.hit is None


def test_mark_completed_transitions_entry():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    r = cache.lookup_or_claim("s", "alert", "k1")
    cache.mark_completed("s", r.canonical)
    r2 = cache.lookup_or_claim("s", "alert", "k2")
    assert r2.hit is not None
    assert r2.hit.completed_at is not None


def test_mark_failed_removes_entry_so_retry_proceeds():
    cache = PromptDedupeCache(ttl_seconds=60.0)
    r = cache.lookup_or_claim("s", "alert", "k1")
    cache.mark_failed("s", r.canonical)
    r2 = cache.lookup_or_claim("s", "alert", "k2")
    assert r2.hit is None


def test_expired_entry_treated_as_miss(monkeypatch):
    cache = PromptDedupeCache(ttl_seconds=1.0)
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    cache.lookup_or_claim("s", "alert", "k1")
    fake_now[0] = 1000.5  # still within TTL
    assert cache.lookup_or_claim("s", "alert", "k2").hit is not None
    fake_now[0] = 1002.0  # past TTL
    assert cache.lookup_or_claim("s", "alert", "k3").hit is None


def test_lru_eviction_at_capacity():
    cache = PromptDedupeCache(ttl_seconds=60.0, max_entries=3)
    cache.lookup_or_claim("s", "a", "ka")
    cache.lookup_or_claim("s", "b", "kb")
    cache.lookup_or_claim("s", "c", "kc")
    # Touch "a" → "b" is now the oldest
    cache.lookup_or_claim("s", "a", "ka2")
    # Insert pushes past capacity → "b" gets evicted
    cache.lookup_or_claim("s", "d", "kd")
    assert cache.lookup_or_claim("s", "b", "kb2").hit is None
    assert cache.lookup_or_claim("s", "a", "ka3").hit is not None


# --- cache: SimHash fuzzy match ---


def test_simhash_threshold_zero_disables_fuzzy():
    cache = PromptDedupeCache(ttl_seconds=60.0, simhash_threshold=0)
    cache.lookup_or_claim("s", "Zodios endpoint members", "k1")
    r = cache.lookup_or_claim("s", "Zodios endpoint messages", "k2")
    # Canonical strings differ ("members" vs "messages"); without fuzzy, miss.
    assert r.hit is None


def test_simhash_threshold_catches_similar_text():
    cache = PromptDedupeCache(ttl_seconds=60.0, simhash_threshold=20)
    cache.lookup_or_claim(
        "s",
        "Zodios: Invalid response from endpoint 'get api/v1/orgs/members'",
        "k1",
    )
    r = cache.lookup_or_claim(
        "s",
        "Zodios: Invalid response from endpoint 'get api/v1/orgs/messages'",
        "k2",
    )
    assert r.hit is not None
    assert r.hamming > 0  # fuzzy match, not exact
    assert r.hit.first_session_key == "k1"


def test_simhash_does_not_cross_scope():
    cache = PromptDedupeCache(ttl_seconds=60.0, simhash_threshold=30)
    cache.lookup_or_claim("scope-A", "Zodios endpoint members", "kA")
    r = cache.lookup_or_claim("scope-B", "Zodios endpoint messages", "kB")
    # Same-shaped text but different scope → still a miss even with threshold.
    assert r.hit is None


# --- construction ---


def test_invalid_construction_raises():
    with pytest.raises(ValueError):
        PromptDedupeCache(ttl_seconds=0)
    with pytest.raises(ValueError):
        PromptDedupeCache(ttl_seconds=10.0, max_entries=0)
    with pytest.raises(ValueError):
        PromptDedupeCache(ttl_seconds=10.0, simhash_threshold=-1)

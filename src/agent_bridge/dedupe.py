"""Cross-session prompt dedupe cache.

Suppresses repeated identical (or near-identical) prompts arriving in
different sessions within a TTL window. Lives entirely inside the Bridge
layer — platforms and agents are unaware of it.

Two-stage matching:

1. **Canonicalize**: regex-mask volatile content (URLs, UUIDs, long numbers,
   timestamps, emails, IPs, long hex). Two alerts that differ only in such
   variable bits collapse to the same key.
2. **Optional SimHash fuzzy match**: when ``simhash_threshold > 0``, a miss
   on exact canonical match falls back to scanning entries in the same
   scope and matching the closest fingerprint within the Hamming threshold.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

# Order matters: more specific patterns first so coarser ones (NUM) don't eat
# parts of structured tokens.
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://\S+"), "<URL>"),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.I,
        ),
        "<UUID>",
    ),
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<TS>",
    ),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<EMAIL>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<HEX>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    (re.compile(r"\b\d{4,}\b"), "<NUM>"),
)
_WHITESPACE = re.compile(r"\s+")


def canonicalize(text: str) -> str:
    out = text
    for pat, repl in _NORMALIZERS:
        out = pat.sub(repl, out)
    return _WHITESPACE.sub(" ", out).strip()


def _hash64(s: str) -> int:
    # Cross-process-stable hash; PYTHONHASHSEED-independent.
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def simhash(text: str, ngram: int = 4) -> int:
    if not text:
        return 0
    grams = (
        [text]
        if len(text) < ngram
        else [text[i : i + ngram] for i in range(len(text) - ngram + 1)]
    )
    bits = [0] * 64
    for g in grams:
        h = _hash64(g)
        for b in range(64):
            bits[b] += 1 if (h >> b) & 1 else -1
    fp = 0
    for b, v in enumerate(bits):
        if v > 0:
            fp |= 1 << b
    return fp


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass
class DedupeEntry:
    scope: str
    canonical_text: str
    first_session_key: str
    fingerprint: int
    started_at: float  # monotonic
    completed_at: float | None = None  # None ⇒ still in-flight


@dataclass(frozen=True)
class DedupeResult:
    hit: DedupeEntry | None
    canonical: str
    hamming: int = 0  # 0 ⇒ exact canonical match; > 0 ⇒ SimHash neighbour


class PromptDedupeCache:
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 512,
        simhash_threshold: int = 0,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        if simhash_threshold < 0:
            raise ValueError(
                f"simhash_threshold must be >= 0, got {simhash_threshold}"
            )
        self._ttl = ttl_seconds
        self._max = max_entries
        self._threshold = simhash_threshold
        # (scope, canonical_text) → entry. Exact lookups stay O(1); SimHash
        # falls back to scanning entries with matching scope.
        self._entries: OrderedDict[tuple[str, str], DedupeEntry] = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        expired = [
            k for k, e in self._entries.items() if now - e.started_at >= self._ttl
        ]
        for k in expired:
            del self._entries[k]

    def lookup_or_claim(
        self,
        scope: str,
        text: str,
        first_session_key: str,
    ) -> DedupeResult:
        now = time.monotonic()
        self._purge_expired(now)
        canonical = canonicalize(text)
        key = (scope, canonical)

        # 1. Exact canonical match.
        exact = self._entries.get(key)
        if exact is not None:
            self._entries.move_to_end(key)
            return DedupeResult(hit=exact, canonical=canonical, hamming=0)

        # 2. SimHash fuzzy match within the same scope (optional).
        fp = simhash(canonical) if self._threshold > 0 else 0
        if self._threshold > 0:
            best_key: tuple[str, str] | None = None
            best_dist = self._threshold + 1
            for k, entry in self._entries.items():
                if entry.scope != scope:
                    continue
                d = hamming(entry.fingerprint, fp)
                if d <= self._threshold and d < best_dist:
                    best_dist = d
                    best_key = k
            if best_key is not None:
                hit = self._entries[best_key]
                self._entries.move_to_end(best_key)
                return DedupeResult(hit=hit, canonical=canonical, hamming=best_dist)

        # 3. Miss → claim slot.
        self._entries[key] = DedupeEntry(
            scope=scope,
            canonical_text=canonical,
            first_session_key=first_session_key,
            fingerprint=fp,
            started_at=now,
        )
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return DedupeResult(hit=None, canonical=canonical)

    def mark_completed(self, scope: str, canonical: str) -> None:
        entry = self._entries.get((scope, canonical))
        if entry is not None:
            entry.completed_at = time.monotonic()

    def mark_failed(self, scope: str, canonical: str) -> None:
        # Drop the slot so the next retry isn't blocked for the full TTL.
        self._entries.pop((scope, canonical), None)

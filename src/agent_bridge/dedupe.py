"""Cross-session prompt dedupe cache.

Suppresses repeated identical prompts arriving in different sessions within a
short window. Primary motivation: alerter integrations (e.g. Sentry → Slack)
that fan one underlying error into many Slack threads, each of which would
otherwise spin up its own agent run.

The cache is keyed on (scope, normalized_text). Scope is platform-defined
(typically the channel-level prefix of the session_key). Normalization strips
the platform sender tag so the same alerter under a renamed display name
still collides.

Concurrency: every public method is synchronous and never awaits. Under
asyncio's single-threaded model this means a method call is atomic with
respect to other coroutines — no explicit lock needed.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass

_TAG_PREFIX = re.compile(r"^\[[^\]]+\]:\s*")


def normalize_prompt(text: str) -> str:
    # The Slack adapter prefixes user messages with "[user_name (user_id)]: ".
    # Stripping it lets a Sentry alert still collide if its display name changes.
    return _TAG_PREFIX.sub("", text, count=1).strip()


@dataclass
class DedupeEntry:
    first_thread_link: str | None
    started_at: float  # monotonic
    completed_at: float | None = None  # None = still in-flight


class PromptDedupeCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], DedupeEntry] = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        # OrderedDict preserves insertion order, but entries are not strictly
        # time-ordered (we move_to_end on hits). Scan everything; cheap for
        # max_entries on the order of hundreds.
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.started_at >= self._ttl
        ]
        for key in expired:
            del self._entries[key]

    def lookup_or_claim(
        self,
        scope: str,
        text: str,
        thread_link: str | None,
    ) -> DedupeEntry | None:
        """Atomic: look up, and if absent, claim the slot.

        Returns the existing entry on hit (caller should skip the run).
        Returns None on miss (caller proceeds and owns this slot).
        """
        now = time.monotonic()
        self._purge_expired(now)
        key = (scope, normalize_prompt(text))
        existing = self._entries.get(key)
        if existing is not None:
            # LRU touch so active entries stick around when capacity is tight.
            self._entries.move_to_end(key)
            return existing
        self._entries[key] = DedupeEntry(
            first_thread_link=thread_link,
            started_at=now,
        )
        while len(self._entries) > self._max:
            # FIFO eviction (popitem(last=False)) keeps recently-used entries.
            self._entries.popitem(last=False)
        return None

    def mark_completed(self, scope: str, text: str) -> None:
        key = (scope, normalize_prompt(text))
        entry = self._entries.get(key)
        if entry is not None:
            entry.completed_at = time.monotonic()

    def mark_failed(self, scope: str, text: str) -> None:
        # On controller exception, drop the entry so a retry isn't blocked
        # for the full TTL.
        key = (scope, normalize_prompt(text))
        self._entries.pop(key, None)

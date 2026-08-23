from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from agent_bridge.bridge.events import BridgeEvent
from agent_bridge.bridge.request import BridgeRequest


class AgentController(Protocol):
    """Interface for AI agent backends.

    An agent is purely invoked: it receives a session ID + prompt,
    loads the session, executes, and yields events.  It does not
    define session semantics or care how results are rendered.

    ``system_prompt`` is built by the platform adapter and passed through
    verbatim — the agent must not interpret platform-specific fields out
    of ``context`` to construct it.  ``context`` itself is opaque metadata
    (useful for audit/logging) and platform-defined.
    """

    def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...

    async def cleanup_session(self, session_id: str) -> None:
        """Release per-session resources the agent holds (worktrees, session
        files, id mappings).  The app's cleanup loop calls this for every
        purged session on every controller — a session this agent never saw
        must be a cheap no-op, and it must never raise for one it did own.
        """
        ...


class MessageRouter(Protocol):
    """Interface platform adapters send messages through.

    ``Bridge`` is the production implementation.  Adapters depend on this
    protocol — not the concrete class — so tests can substitute a fake
    that replays a scripted event stream.

    The whole turn arrives as one ``BridgeRequest``: ``request.agent``
    selects a named agent controller registered with the router (``None``
    routes to the default one — the platform decides which name a session
    uses, e.g. Slack's per-channel profiles; the router only resolves it),
    and ``request.resumable`` decides whether the ``session_key`` maps to a
    persistent session or a one-shot ephemeral one.
    """

    def handle_message(self, request: BridgeRequest) -> AsyncIterator[BridgeEvent]: ...


class PlatformAdapter(Protocol):
    """Interface for chat platform frontends.

    A platform defines session semantics (e.g. Slack thread = session),
    manages per-session locking, and decides how to render agent events.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def cleanup(self) -> int:
        """Periodic housekeeping; returns entries removed (0 if none)."""
        ...


# --- Storage / strategy ports ---------------------------------------------
#
# The pipeline stages depend on these, never on a concrete implementation:
# app.py picks the implementation and injects it. All ports are async from
# day one — the built-in implementations are in-process and trivially so,
# but a networked one (RDBMS session store, Redis dedupe/capacity) must not
# force an interface change.


@dataclass(frozen=True)
class SessionEntry:
    """One session mapping as the store persists it.

    ``agent`` is the named profile the session was created under (None for
    the env-built default) — the affinity ``SessionManager`` enforces.
    Timestamps are ISO-8601 strings; the *policy* layer parses them, the
    store just round-trips them.
    """

    session_id: str
    created_at: str
    last_used: str
    agent: str | None = None


class SessionStoreError(OSError):
    """A store operation could not be persisted. The operation must not
    have been partially applied: raising means "nothing changed"."""


class SessionStore(Protocol):
    """Persistence behind ``SessionManager`` (key → ``SessionEntry``).

    Implementations hold *state only* — TTL, agent affinity, and orphan
    tracking are ``SessionManager`` policy. Mutations are atomic per call:
    ``put``/``delete`` either fully apply or raise ``SessionStoreError``
    with nothing changed, which is what lets the policy layer keep its
    rollback guarantees without compensating writes.
    """

    async def get(self, key: str) -> SessionEntry | None: ...

    async def put(self, key: str, entry: SessionEntry) -> None: ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present; absence is not an error."""
        ...

    async def list_all(self) -> dict[str, SessionEntry]: ...


@dataclass(frozen=True)
class DedupeHit:
    """An earlier prompt this one duplicates."""

    first_session_key: str
    in_flight: bool  # True while the first run hasn't completed yet
    matched_text: str  # the matched (algorithm-normalized) form, for logging
    hamming: int = 0  # 0 ⇒ exact match under the algorithm's normalization


@dataclass(frozen=True)
class DedupeDecision:
    """Outcome of ``lookup_or_claim``: a hit, or a claimed slot to release.

    ``claim_token`` is opaque to the caller — whatever the algorithm needs
    to find its own entry again in ``mark_completed``/``mark_failed``.
    """

    hit: DedupeHit | None
    claim_token: str


class DedupeCache(Protocol):
    """Cross-session duplicate-prompt suppression.

    How "duplicate" is decided (canonicalization, SimHash, embeddings, …)
    is entirely the implementation's business: the port receives the raw
    prompt text. A claim must be released exactly once — ``mark_completed``
    keeps collapsing duplicates onto the finished run, ``mark_failed``
    drops the entry so retries aren't blocked.
    """

    async def lookup_or_claim(
        self, scope: str, text: str, first_session_key: str
    ) -> DedupeDecision: ...

    async def mark_completed(self, scope: str, claim_token: str) -> None: ...

    async def mark_failed(self, scope: str, claim_token: str) -> None: ...


class CapacityLease(Protocol):
    """One held processing slot. ``release`` must be idempotent — the
    holder calls it in a ``finally`` that can run on any exit path."""

    async def release(self) -> None: ...


class CapacityLimiter(Protocol):
    """Global concurrency gate.

    ``try_acquire`` never queues: a full limiter answers ``None`` and the
    caller rejects the turn. Returning a lease (rather than exposing a bare
    ``release()``) keeps the interface correct for distributed
    implementations, where releasing means giving back *this* token.
    """

    async def try_acquire(self) -> CapacityLease | None: ...

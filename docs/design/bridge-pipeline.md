# Bridge Pipeline — Design

**Status:** implemented (2026-08). This document records the *why*; the
*what* (behaviour reference, configuration, event shapes) lives in
[docs/bridge.md](../bridge.md).

## Context

The bridge started as one method: `Bridge.handle_message` resolved the agent,
claimed a dedupe slot, resolved the session, checked a semaphore, invoked the
controller, decorated usage onto completions, and released the dedupe claim on
three separate exit paths. It worked, but every concern was welded to every
other: swapping session storage meant editing the router, adding a
cross-cutting concern meant threading it through the monolith, and the dedupe
release bookkeeping had to be repeated at each exit.

The goal was to make the bridge a strong intermediary gateway: routing at the
core, with traffic control, dedupe, and session control as replaceable
components — e.g. session storage moving from a local JSON file to a
relational database, or the dedupe algorithm being swapped — without touching
the routing.

## The two plugin axes (kept deliberately separate)

The word "plugin" hides two different mechanisms with different costs:

1. **Swap an implementation** → a **port** (strategy protocol). The concern
   stays, its backing changes: JSON file → RDBMS, in-memory dedupe → Redis,
   in-process semaphore → distributed tokens.
2. **Add or remove a concern** → a **stage** (middleware). The pipeline gains
   or loses a step: dedupe off, a future rate-limiter or audit stage in.

The user-facing goals ("different session storage", "different dedupe
algorithm") are all axis 1. Axis 2 exists so the *next* cross-cutting concern
lands as one new file instead of another branch inside a monolith.

## Architecture

```
Bridge (MessageRouter, thin shell)
  └─ handle_message(BridgeRequest) → composed Handler

  [AgentResolution] → [Dedupe?] → [SessionResolution] → [Usage] → [Capacity] → run_agent
        stage             stage          stage             stage       stage       core
                            │               │                            │
                       DedupeCache    SessionManager               CapacityLimiter
                          port         (policy)                        port
                            │               │ SessionStore port          │
                     PromptDedupeCache  JsonSessionStore        SemaphoreCapacityLimiter
                     (Redis later)      (RDBMS later)           (distributed later)
```

| Role | What it is | What may change |
|------|------------|-----------------|
| **Core** (`pipeline.run_agent`) | yield `Processing`, invoke the controller | nothing — this *is* routing |
| **Stage** (`bridge/middleware/`) | one node of the chain, wraps everything further in | present/absent per assembly; **order is never configurable** |
| **Port** (`bridge/protocols.py`) | async protocol a stage depends on | freely swappable; `app.py` injects the implementation |

Requests travel inward enriching a mutable `TurnContext`
(`request → agent/controller → session_id/is_new`); the agent's event stream
travels back outward through the same stages, which observe or decorate it.

## Decisions

### The stage order is fixed, and that is the point

The order encodes invariants that used to be enforced by careful code
placement inside the monolith:

1. **Agent resolution outermost** — an unknown agent name short-circuits
   before the dedupe claim, session mint, or capacity lease can be touched.
2. **Dedupe outside session** — a duplicate short-circuits before a session
   is minted or `last_used` moves.
3. **Dedupe outside capacity** — this is the structural win. A capacity
   reject, a controller error `Completion`, a raised exception, and an
   abandoned stream all *flow outward through* the dedupe stage, whose single
   `try/finally` releases the claim. The monolith needed release calls on
   three separate exit paths; the composition makes them one.
4. **Usage between session and capacity** — a session is marked
   usage-trackable the moment it is minted, even if this turn is then
   capacity-rejected (a rejected turn spends nothing, so the running total
   stays trustworthy). Decoration of outbound `Completion`s is
   position-independent as long as it is inside session resolution.

Because reordering is meaningless-to-dangerous, the assembly lives in code
(`Bridge.__init__`), never in configuration. There is no plugin registry and
no dynamic loading: swapping an implementation means changing the injected
object in `app.py`, keeping pyright's strict checking end to end.

### The middleware contract

Three rules every stage must obey (docstring of `bridge/pipeline.py`):

1. **Short-circuit** = yield exactly one `Completion` and return without
   calling `call_next`.
2. **Forwarding must not inject or swallow `Completion`s** — the controllers'
   exactly-one-`Completion` guarantee has to survive the chain. In-place
   decoration (usage) is allowed.
3. **Cleanup in `try/finally`.** A platform abandoning the stream closes the
   generator chain (`GeneratorExit` passes through every stage); `finally` is
   the only block guaranteed to run, and on that path a stage must only clean
   up, never yield.

`Processing` is emitted by the core, not the capacity stage, so a pipeline
assembled without a capacity gate still opens its stream correctly.

### `TurnContext` and the typing compromise

`ctx.controller` / `ctx.session_id` are `| None` because Python's type system
cannot express "set by the time the core runs". The core guards at runtime and
raises `RuntimeError` on a misassembled pipeline. A context whose type evolves
per stage was considered and rejected — the generics gymnastics cost more than
the runtime guard.

### Ports are async from day one

The built-in implementations are all in-process and trivially async, but a
networked implementation (RDBMS store, Redis cache/limiter) must not force an
interface change later. Async-ifying `SessionManager`'s callers was a one-time
cost paid in this refactor.

### `SessionStore` is per-entry, not load/save-the-blob

`get/put/delete/list_all` is the shape an RDBMS wants (`SELECT … WHERE key`);
"read the whole file, write the whole file" is the JSON store's private
business. Mutations are **atomic per call**: they fully apply or raise
`SessionStoreError` having changed nothing — which is what lets
`SessionManager` keep its rollback guarantees (failed touch → resume with old
`last_used`; failed create → raise, nothing recorded) without compensating
writes. All *policy* — TTL, agent affinity, orphan draining — stays in
`SessionManager`; a store implementation holds state only.

`JsonSessionStore` is deliberately stateless (every op reads the file fresh):
the file is small, the rate is one op per message, and a store that always
agrees with the disk is worth more than a cache.

### `DedupeCache` receives raw text and hands out an opaque claim token

Canonicalization and SimHash are `PromptDedupeCache` implementation details.
An implementation based on embeddings (or anything else) must not be
constrained by the port, so the port takes the raw prompt and returns
whatever token it needs to find its own entry again in `mark_*`.

### `CapacityLimiter` returns a lease, not a bare release

`try_acquire() → CapacityLease | None` (never queues; `None` = reject now).
A bare `release()` method is wrong for distributed implementations, where
releasing means giving back *this* token. The lease's `release()` is
idempotent because the holder calls it in a `finally` that can run on any
exit path. Keyed capacity (per-agent quotas) was deliberately deferred — the
lease shape leaves room for it without breaking callers.

### `BridgeRequest` is the whole call

`MessageRouter.handle_message(request)` replaced six positional/keyword
parameters (a breaking change, `feat!:`). The request object already existed
at the platform layer; it moved to `bridge/request.py` because it is the
inbound half of the bridge contract, and every new field lands as a dataclass
field instead of another parameter on every implementation and fake.

## Deliberate behaviour deltas vs the monolith

Kept intentionally small; everything else is behaviour-preserving (the entire
pre-refactor router test suite runs unchanged against the pipeline):

- `Processing` is emitted by the core rather than after semaphore acquisition
  — same observable stream order in the default assembly.
- `SessionManager` no longer purges expired entries in its constructor; the
  periodic cleanup loop drains them (and now reports those ids for
  agent-side `cleanup_session`, which the silent boot purge never did).
- The `dedupe_hit` log line moved loggers:
  `agent_bridge.bridge.router` → `agent_bridge.bridge.middleware.dedupe`.

## Testing strategy

Three layers, mapping to the three roles:

| Layer | Where | Pins |
|-------|-------|------|
| **Port contracts** | `tests/contracts/test_session_store.py`, `test_dedupe_cache.py`, `test_capacity_limiter.py` | every implementation of a port against the same spec; a new store/cache/limiter joins by adding a fixture param |
| **Stage units** | `tests/bridge/middleware/`, `tests/bridge/test_pipeline.py` | each stage in isolation against scripted downstream handlers: short-circuits, ctx enrichment, release-on-every-exit-path (including abandoned streams), compose order |
| **Bridge input→output** | `tests/bridge/test_router.py` (integration), `tests/e2e/` (full stacks) | the assembled pipeline end to end: a `BridgeRequest` in, the exact event stream out, across capacity/dedupe/usage/named-agent scenarios |

The middle layer is what the monolith could not have: paths like "the claim
is released when the platform abandons the stream mid-turn" are ten-line
stage tests instead of full-bridge orchestrations.

## How to extend

- **New session storage**: implement `SessionStore` (four methods,
  atomic-or-raise mutations), add it to the `tests/contracts/test_session_store.py`
  fixture params, inject it in `app.py`:
  `SessionManager(config, store=PostgresSessionStore(...))`.
- **New dedupe algorithm / backend**: implement `DedupeCache`, add to its
  contract suite, pass it as `Bridge(..., dedupe=...)`.
- **New capacity backend**: implement `CapacityLimiter`/`CapacityLease`, add
  to its contract suite, pass as `Bridge(..., limiter=...)`.
- **New cross-cutting concern** (rate limiting, audit, billing): write a
  stage in `bridge/middleware/` honoring the three contract rules, insert it
  at the position its invariants demand in `Bridge.__init__`, and document
  why that position — the order comment there is the authoritative list.

## Migration record

Landed as four commits, each green on the full suite:

1. `feat(bridge)!:` `handle_message(BridgeRequest)` — request collapse.
2. `refactor(bridge):` ports extracted (`SessionStore`/`DedupeCache`/
   `CapacityLimiter` + built-ins + contract suites); `SessionManager` async.
3. `refactor(bridge):` router decomposed into the pipeline; stage unit suites.
4. `docs:` this document and the updated references.

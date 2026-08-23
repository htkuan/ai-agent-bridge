# Bridge

The Bridge is the routing core of the service. It sits between platform
adapters and agent controllers as a **middleware pipeline**: a thin
`MessageRouter` shell over a fixed chain of stages, each of which drinks from
a swappable port. It knows nothing about Slack or Claude Code or any specific
platform/agent — both sides plug in through narrow protocols.

Design rationale (why the pipeline, why the ports, what the order encodes):
[docs/design/bridge-pipeline.md](design/bridge-pipeline.md). This page is the
behaviour reference.

Source: `src/agent_bridge/bridge/`. The package is also the layer both sides
plug into — it depends on neither `agents/` nor `platforms/`, while both of
them import its `events`, `request` and `protocols`.

| File | Role |
|------|------|
| `src/agent_bridge/bridge/router.py` | `Bridge` — the `MessageRouter` shell; assembles the default pipeline (the stage order comment there is authoritative) |
| `src/agent_bridge/bridge/pipeline.py` | `TurnContext`, `Handler`, `BridgeMiddleware`, `compose()`, and `run_agent` (the core) — plus the middleware contract |
| `src/agent_bridge/bridge/middleware/` | The stages: `resolution.py` (agent + session), `dedupe.py`, `capacity.py`, `usage.py` |
| `src/agent_bridge/bridge/request.py` | `BridgeRequest` — the single argument to `handle_message` |
| `src/agent_bridge/bridge/events.py` | The five `BridgeEvent` types (`Processing`, `TextDelta`, `StatusUpdate`, `UserQuestion`, `Completion`) plus the generic `Usage` report |
| `src/agent_bridge/bridge/protocols.py` | The contracts: `AgentController`, `PlatformAdapter`, `MessageRouter` — and the ports: `SessionStore`, `DedupeCache`, `CapacityLimiter`/`CapacityLease` (with `SessionEntry`, `DedupeDecision`/`DedupeHit`, `SessionStoreError`) |
| `src/agent_bridge/bridge/session.py` | `SessionManager` — session *policy* (TTL, agent affinity, orphan draining) over a `SessionStore` |
| `src/agent_bridge/bridge/stores.py` | `JsonSessionStore` — the built-in `SessionStore` (stateless, one JSON file) |
| `src/agent_bridge/bridge/dedupe.py` | `PromptDedupeCache` — the built-in `DedupeCache` (canonicalize + SimHash) |
| `src/agent_bridge/bridge/capacity.py` | `SemaphoreCapacityLimiter` — the built-in `CapacityLimiter` |
| `src/agent_bridge/bridge/config.py` | One config per component — `SessionConfig`, `RouterConfig`, `DedupeConfig` — plus `BridgeConfig` aggregating them |

## The pipeline

```
handle_message(BridgeRequest)
        │ inward: TurnContext is enriched stage by stage
        ▼
[AgentResolution] → [Dedupe?] → [SessionResolution] → [Usage] → [Capacity] → run_agent
        ▲                                                                        │
        └──────────── outward: BridgeEvents flow back through every stage ───────┘
```

| # | Stage | Present | Inward (request) | Outward (events) |
|---|-------|---------|------------------|------------------|
| 1 | `AgentResolutionStage` | always | `request.agent`/default → `ctx.agent`, `ctx.controller`; unknown name ⇒ short-circuit `unknown_agent` | forwards |
| 2 | `DedupeStage` | only when a cache is configured | `lookup_or_claim`; duplicate ⇒ short-circuit (`metadata["dedupe"]`) | watches `Completion.is_error`; `finally` releases the claim (`mark_completed`/`mark_failed`) |
| 3 | `SessionResolutionStage` | always | resumable ⇒ `SessionManager.get_or_create(key, agent)`; else mint ephemeral UUID → `ctx.session_id`, `ctx.is_new` | forwards |
| 4 | `UsageStage` | always | marks the session usage-trackable when `resumable ∧ is_new` | decorates `Completion` with `usage` / `session_usage` in place |
| 5 | `CapacityStage` | always | `limiter.try_acquire()`; full ⇒ short-circuit `capacity_full` | `finally` releases the lease |
| 6 | `run_agent` (core) | always | guard, yield `Processing`, `controller.run(...)` | the agent's stream |

**The order is fixed** — it encodes the invariants (unknown agent touches no
shared state; a duplicate mints no session; every failure flowing outward
passes the dedupe stage and releases the claim; usage tracks at mint). See the
design doc for the full argument.

**The ports are swappable** — `app.py` injects the implementations:

| Port | Built-in | Swap for |
|------|----------|----------|
| `SessionStore` | `JsonSessionStore` | an RDBMS store |
| `DedupeCache` | `PromptDedupeCache` | Redis, other algorithms |
| `CapacityLimiter` | `SemaphoreCapacityLimiter` | distributed tokens |

```python
bridge = Bridge(
    config.router,
    session_manager,  # SessionManager(config, store=...) to swap storage
    controller,  # the default (agent=None)
    dedupe=dedupe,  # DedupeCache | None — None disables the stage
    named_controllers={"backend": other},  # routed to by name
    limiter=None,  # CapacityLimiter | None — None = built-in semaphore
)
```

### The middleware contract

Every stage must obey three rules (enforced by review + the stage unit suites):

1. **Short-circuit** = yield exactly one `Completion` and return without
   calling `call_next`.
2. **Forwarding must not inject or swallow `Completion`s** — the controllers'
   exactly-one-`Completion` guarantee survives the whole chain; in-place
   decoration (usage) is allowed.
3. **Cleanup in `try`/`finally`** — an abandoned stream closes the generator
   chain, and `finally` is the only block guaranteed to run; on that path a
   stage must only clean up, never yield.

## What the Bridge Does (and Doesn't)

| Does | Does Not |
|------|----------|
| Resolve `session_key → session_id` (via `SessionManager`) | Define what a "session" means — that's the platform's job |
| Route by `request.agent` name to one of several registered controllers | Decide which agent a session uses — that's the platform's pick |
| Cap total in-flight sessions globally (`CapacityLimiter`) | Serialize per-session — that's the platform's lock |
| Optionally short-circuit identical prompts across sessions (`DedupeCache`) | Know about Slack threads, Discord channels, etc. |
| Forward `BridgeEvent`s yielded by the controller | Render those events into messages — that's the platform's job |
| Mint ephemeral UUIDs for non-resumable triggers | Touch disk on those non-resumable calls |

This deliberately narrow surface is what lets the same Bridge instance serve
every adapter and every agent simultaneously.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_SESSION_STORE_PATH` | `./sessions.json` | Where `JsonSessionStore` persists the `session_key → session_id` map. |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | `72` | Sessions inactive for longer than this are purged. |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | `5` | Size of the built-in capacity limiter. Beyond this, new requests are rejected immediately. |
| `AGENT_BRIDGE_DEDUPE_TTL_SECONDS` | `0` | Cross-session prompt dedupe TTL. `0` disables the feature entirely (the stage is not assembled). |
| `AGENT_BRIDGE_DEDUPE_MAX_ENTRIES` | `512` | LRU cap for the built-in dedupe cache. |
| `AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD` | `0` | Hamming threshold for SimHash fuzzy fallback. `0` = exact canonical match only. |

Each component takes its own config object, and `BridgeConfig` is the aggregate:

| Config | Component | Fields |
|--------|-----------|--------|
| `SessionConfig` | `SessionManager` / `JsonSessionStore` | `store_path`, `ttl_hours` |
| `RouterConfig` | `Bridge` (default limiter size) | `max_concurrent_sessions` |
| `DedupeConfig` | `PromptDedupeCache` | `ttl_seconds`, `max_entries`, `simhash_threshold`, plus the `enabled` property (`ttl_seconds > 0`) |

Validation runs on **every** construction (`__post_init__`), not just
`from_env()` — so a config built directly in a test is checked the same way
one read from the environment is. Invalid values raise `ValueError`, which at
startup means the process fails fast.

### Named agent routing

`BridgeRequest.agent` picks the controller: `None` (the default) routes to the
default controller — or, when `default_agent` is configured, resolves to that
*name* first, so sessions record the actual profile; a name routes to the
matching entry in `named_controllers`. The platform decides which name a
session uses (e.g. Slack's per-channel profiles); the bridge only resolves it.
Resolution is the outermost stage — an unknown name yields a single error
`Completion` (`error_code="unknown_agent"`) without touching any shared state.
Startup validation makes that unreachable for env-built configs; the guard
covers programmatic ones. The capacity limiter stays global across all
controllers.

`SessionManager` records each resumable session's `agent`. When the same
`session_key` later arrives with a different name (an operator remapped the
channel), the old session is abandoned — a session created under one
controller's work dir must not be resumed under another's — and its id is
handed to the periodic cleanup via `purge_expired()`, exactly like a TTL
purge.

## Session Resolution

Each call routes through one of two modes, picked by the adapter via
`BridgeRequest.resumable`:

### `resumable=True` (default)

Used by conversational platforms (e.g. Slack: thread = session).

- `SessionManager.get_or_create(session_key, agent)` returns `(session_id, is_new)`.
- The mapping is persisted through the `SessionStore` port (built-in: one JSON
  file) so the same `session_key` continues the same `session_id` across
  process restarts.
- Each access updates `last_used`; sessions inactive longer than
  `session_ttl_hours` are purged by the periodic cleanup loop, which reports
  the purged ids for agent-side `cleanup_session`.
- Store mutations are atomic-or-raise (`SessionStoreError`, an `OSError`):
  a failed *touch* keeps the resume working with the old `last_used`; a failed
  *create* raises — nothing was recorded, the turn must not proceed.

### `resumable=False`

Used by one-shot triggers (e.g. heartbeat ticks, webhooks).

- The session stage mints a fresh `str(uuid.uuid4())` every call.
- The `SessionManager` is not touched — no disk write, no entry in the store.
- Two calls with the *same* `session_key` deliberately get *different*
  `session_id`s. The platform is saying "every invocation is conceptually
  independent."
- Dedupe is also skipped for `resumable=False` — a scheduled prompt firing on
  its cadence is meaningful, not a duplicate.

## Global Concurrency Gate

The `CapacityStage` holds one `CapacityLimiter` shared by every agent run in
the process (built-in: `asyncio.Semaphore` behind
`SemaphoreCapacityLimiter`).

| Situation | Behaviour |
|-----------|-----------|
| Slots available | `try_acquire()` returns a lease; the core yields `Processing` and runs the controller |
| All slots taken | `try_acquire()` returns `None` ⇒ yield `Completion(is_error=True, metadata={"error_code": "capacity_full"})` and return — **no queuing** |
| Controller raises / stream abandoned | `finally` releases the lease; lease release is idempotent |

The reject-without-queue policy is deliberate: queuing in the Bridge would
hide back-pressure from the platform, which often has its own pending-message
strategy (Slack: latest-only pending slot per thread).

## Cross-Session Prompt Dedupe

Enabled when `AGENT_BRIDGE_DEDUPE_TTL_SECONDS > 0` and the Bridge is
constructed with a `DedupeCache` — then the `DedupeStage` is assembled into
the chain. Purpose: collapse alerter floods (e.g. one underlying error fanning
into many Slack threads) into a single agent run while the original
investigation is in flight or recently completed.

The **port** is algorithm-neutral: `lookup_or_claim(scope, text,
first_session_key) → DedupeDecision` receives the raw prompt and returns
either a `DedupeHit` or an opaque `claim_token` the stage later returns to
`mark_completed`/`mark_failed`. Everything below describes the built-in
`PromptDedupeCache`.

### Scope

The cache key is `(scope, canonical_text)`. `scope` is derived by the stage,
stripping the trailing identifier from the `session_key`:

```python
scope = session_key.rpartition(":")[0]
# slack:C12345678:1779766966.243639  →  slack:C12345678
```

This makes dedupe **cross-thread / cross-session within a channel**, while
staying **per-channel** so unrelated alerts in other channels don't collide.

Dedupe is automatically skipped for:

- Calls with `resumable=False` (heartbeats etc.)
- Empty / whitespace-only `text`
- `session_key`s without a `:` separator (no derivable scope)

### Two-stage matching (built-in cache)

**Stage 1 — Canonicalize, then exact match.** Regex-mask volatile content into
placeholders, then look up the canonical form in the cache. The substitutions
(`src/agent_bridge/bridge/dedupe.py`):

| Pattern | Replacement |
|---------|-------------|
| `https?://…` | `<URL>` |
| RFC 4122 UUID | `<UUID>` |
| ISO 8601 timestamp | `<TS>` |
| Email address | `<EMAIL>` |
| 12+ hex chars (commit SHA, long token) | `<HEX>` |
| IPv4 dotted quad | `<IP>` |
| 4+ digit number | `<NUM>` |
| Runs of whitespace | single space |

Order is intentional: specific patterns first, so the coarse `<NUM>` rule does
not eat parts of a UUID.

**Stage 2 — SimHash fuzzy fallback (optional).** When `simhash_threshold > 0`,
an exact-canonical miss falls back to scanning entries in the same scope and
picking the closest 64-bit SimHash fingerprint within the Hamming threshold.

- The fingerprint is built from 4-grams of the canonical text, hashed with MD5
  (so it's stable across processes — Python's built-in `hash()` is
  `PYTHONHASHSEED`-randomized).
- Comparison is one 64-bit `XOR` + `popcount` per candidate entry —
  microseconds even at the LRU cap.
- `threshold=20` is a sensible starting point for catching same-template
  variants ("endpoint members" vs "endpoint messages"); `0` keeps the feature
  off.

### Hit lifecycle

| State | `DedupeHit.in_flight` | Bridge response on subsequent hit |
|-------|----------------------|------------------------------------|
| In-flight | `True` | `Completion` with `metadata["dedupe"]="in_flight"` |
| Completed (within TTL) | `False` | `Completion` with `metadata["dedupe"]="recent_hit"` |
| Expired (past TTL) | — | Treated as a miss; old entry purged on next lookup |

Both states include `metadata["first_session_key"]`, which the platform
adapter can render into a clickable pointer back to the original
investigation.

### Failure paths release the claim

The stage's single `try`/`finally` covers every path — this is a property of
the composition (dedupe wraps everything further in), not of extra wiring:

| Trigger | Action |
|---------|--------|
| Controller raises | `mark_failed` — entry removed before the exception propagates |
| Controller yields `Completion(is_error=True)` | `mark_failed` (most real failures take this path: timeout, non-zero exit, API error) |
| Capacity-full rejection flowing out from the capacity stage | `mark_failed` |
| Platform abandons the stream mid-turn | `mark_failed` |
| Successful run | `mark_completed` — entry stays in the cache until TTL |

Without this, an alerter retrying after a single transient failure could be
locked out for the full TTL with a pointer back to the failed run.

### Storage and eviction (built-in cache)

- In-memory `OrderedDict`, no persistence — restart clears the cache, which is
  the right behaviour for a short-window dedupe.
- TTL purge runs lazily on every `lookup_or_claim` (scan + drop expired before
  checking).
- LRU eviction kicks in only when `len > max_entries`; `move_to_end` on hits
  keeps active entries around.
- The methods are async (the port demands it) but never actually suspend, so
  under asyncio they stay atomic w.r.t. other coroutines — no explicit lock
  needed. A networked implementation would need its own atomicity story.

## Event Flow

The Bridge yields one of two patterns per call:

**Normal path** — interleaved with whatever the controller yields:

```
Processing       (all gates passed, agent starting — emitted by the core)
TextDelta…       (incremental text, 0..N)
StatusUpdate…    (tool use / progress, 0..N)
UserQuestion?    (optional, pauses session until adapter replies)
Completion       (final, with cost/duration/error)
```

**Short-circuit path** — a single `Completion` from the stage that stopped the
turn:

| Stage | Reason | `Completion` shape |
|-------|--------|--------------------|
| AgentResolution | unknown agent name | `metadata={"error_code": "unknown_agent"}`, `is_error=True` |
| Dedupe | duplicate prompt | `text=":repeat: Duplicate detected — skipping."`, `metadata={"dedupe": "in_flight"\|"recent_hit", "first_session_key": …}`, `is_error=False` |
| Capacity | no free slot | `text="Too many requests being processed, please try again later."`, `metadata={"error_code": "capacity_full"}`, `is_error=True` |

The Bridge never invents `TextDelta`/`StatusUpdate`/`UserQuestion` itself —
those come exclusively from the controller.

## Usage Reporting

The Bridge owns the **generic usage contract** and assembles it from what the
agent reports — agents only emit raw values, platforms only render.

### The `Usage` structure

`Usage` (in `events.py`) is the canonical, agent-agnostic usage report. The
same shape serves a single turn and an accumulated session total:

| Field | Meaning |
|-------|---------|
| `input_tokens` / `output_tokens` | Token counts (exclude cache) |
| `cache_read_tokens` / `cache_creation_tokens` | Cache token counts |
| `num_turns` | Agent turns within the invocation |
| `duration_api_ms` | API-only duration |
| `duration_ms` / `cost_usd` | Wall-clock duration / cost (mirrors the `Completion` fields) |
| `total_tokens` (property) | Real total: `input + output + cache_read + cache_creation` |

### How it flows

1. **Agent** parses its native output and writes raw counts into
   `Completion.metadata["usage"]` using the canonical keys above (it knows its
   own format; it does not import `Usage`).
2. **`UsageStage`** calls `Usage.from_completion(...)` on every `Completion`
   flowing outward, pulling token/turn detail from `metadata["usage"]` and
   cost/duration from the first-class `Completion` fields. The result is set
   on `Completion.usage`.
3. **Platform** reads the typed `Completion.usage` and renders it however it
   likes (Slack appends a footer; heartbeat could log it). Rendering and any
   on/off toggle live in the platform, not the Bridge.

Bridge-minted completions (dedupe hits, capacity rejections) carry no usage
metadata, so their `usage` stays `None`.

### Session accumulation

`UsageStage` also maintains an **in-memory** per-session running total and
sets it on `Completion.session_usage`:

- Claude's `result` event reports only the *current* invocation's usage, so
  the cumulative total is summed across turns of the same `session_id`.
- The accumulator is **not persisted** — it resets on restart
  (`Bridge.forget_session_usage` also drops a session when its
  `SessionManager` entry is TTL-purged).
- A running total is only kept for sessions tracked from their **first turn**
  (`is_new` at mint — even if that first turn is then capacity-rejected,
  which spends nothing). For a session resumed without a tracked start —
  after a restart, or one that pre-existed the feature — `session_usage` is
  left `None` rather than reporting a misleadingly-low partial total.
  Non-resumable triggers never accumulate.

## Error Handling

| Failure mode | Bridge behaviour |
|--------------|------------------|
| Session store write error on create | `SessionStoreError` (an `OSError`) from `get_or_create` — propagates up to the platform adapter, which should log and surface to the user |
| Session store write error on touch | Logged; the turn proceeds with the old `last_used` (the store guarantees nothing was half-applied) |
| Controller raises | Capacity lease released, dedupe claim released, exception re-raised (the adapter's `try` block typically catches and posts an error message) |
| Controller yields `Completion(is_error=True)` | Forwarded as a normal event; dedupe claim released so retries can re-run |
| Capacity full | Single error `Completion`; the reject flows out through the dedupe stage, which releases the claim |
| Platform abandons the stream | `GeneratorExit` passes through every stage; each `finally` runs (lease back, claim released) |

## Adding a New Platform or Agent

The Bridge is intentionally not subclassable. To extend the system you
implement one side of its protocol contract:

### `PlatformAdapter`

```python
class PlatformAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    # Periodic housekeeping; returns entries removed.
    async def cleanup(self) -> int: ...
```

The adapter is responsible for defining `session_key` format, owning
per-session locking, building `text` and `system_prompt`, and choosing
`resumable=True/False`. It builds a `BridgeRequest`, calls
`bridge.handle_message(request)` and renders the resulting `BridgeEvent`s.

In practice adapters subclass `BasePlatformAdapter` (`platforms/base.py`),
which owns the shared flow: the platform callback pre-processes its native
event into a `BridgeRequest`, `process()` forwards it through
`handle_message` and dispatches each streamed event to an `on_*` hook, and the
subclass overrides only the hooks it renders. The Protocol stays the contract;
the base is implementation reuse.

See [docs/platforms/slack.md](platforms/slack.md) and
[docs/platforms/heartbeat.md](platforms/heartbeat.md) for working examples.

### `AgentController`

```python
class AgentController(Protocol):
    def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...
```

The controller receives a pre-built `prompt` and `system_prompt` from whatever
platform invoked it, runs the agent, and yields exactly one `Completion` at
the end. It must not parse platform-specific keys out of `context`.

See [docs/agents/claude.md](agents/claude.md) for a working example.

### A new port implementation or pipeline stage

See [docs/design/bridge-pipeline.md — How to extend](design/bridge-pipeline.md#how-to-extend).

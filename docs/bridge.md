# Bridge

The Bridge is the routing core of the service. It sits between platform adapters and agent controllers and has exactly three jobs: resolve a `session_key` into a stable `session_id`, gate global concurrency, and forward events from the agent back out. It knows nothing about Slack or Claude Code or any specific platform/agent — both sides plug in through narrow protocols.

Source: `src/agent_bridge/bridge/`. The package is also the layer both sides plug into — it depends on neither `agents/` nor `platforms/`, while both of them import its `events` and `protocols`.

| File | Role |
|------|------|
| `src/agent_bridge/bridge/router.py` | The `Bridge` class itself |
| `src/agent_bridge/bridge/events.py` | The five `BridgeEvent` types (`Processing`, `TextDelta`, `StatusUpdate`, `UserQuestion`, `Completion`) plus the generic `Usage` report |
| `src/agent_bridge/bridge/session.py` | `SessionManager` — persistent `session_key → session_id` map with TTL |
| `src/agent_bridge/bridge/dedupe.py` | `PromptDedupeCache` — optional cross-session prompt dedupe |
| `src/agent_bridge/bridge/protocols.py` | `AgentController`, `PlatformAdapter`, and `MessageRouter` protocols |
| `src/agent_bridge/bridge/config.py` | One config per component — `SessionConfig`, `RouterConfig`, `DedupeConfig` — plus `BridgeConfig` aggregating them |

## What the Bridge Does (and Doesn't)

| Does | Does Not |
|------|----------|
| Resolve `session_key → session_id` (via `SessionManager`) | Define what a "session" means — that's the platform's job |
| Cap total in-flight sessions globally (Semaphore) | Serialize per-session — that's the platform's lock |
| Optionally short-circuit identical prompts across sessions (`PromptDedupeCache`) | Know about Slack threads, Discord channels, etc. |
| Forward `BridgeEvent`s yielded by the controller | Render those events into messages — that's the platform's job |
| Mint ephemeral UUIDs for non-resumable triggers | Touch disk on those non-resumable calls |

This deliberately narrow surface is what lets the same Bridge instance serve every adapter and every agent simultaneously.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_SESSION_STORE_PATH` | `./sessions.json` | Where `SessionManager` persists the `session_key → session_id` map. |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | `72` | Sessions inactive for longer than this are purged. |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | `5` | Global Semaphore size. Beyond this, new requests are rejected immediately. |
| `AGENT_BRIDGE_DEDUPE_TTL_SECONDS` | `0` | Cross-session prompt dedupe TTL. `0` disables the feature entirely. |
| `AGENT_BRIDGE_DEDUPE_MAX_ENTRIES` | `512` | LRU cap for the dedupe cache. |
| `AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD` | `0` | Hamming threshold for SimHash fuzzy fallback. `0` = exact canonical match only. |

Each component takes its own config object, and `BridgeConfig` is the aggregate:

| Config | Component | Fields |
|--------|-----------|--------|
| `SessionConfig` | `SessionManager` | `store_path`, `ttl_hours` |
| `RouterConfig` | `Bridge` | `max_concurrent_sessions` |
| `DedupeConfig` | `PromptDedupeCache` | `ttl_seconds`, `max_entries`, `simhash_threshold`, plus the `enabled` property (`ttl_seconds > 0`) |

```python
bridge = Bridge(config.router, session_manager, controller, dedupe=dedupe)
```

Validation runs on **every** construction (`__post_init__`), not just `from_env()` — so a
config built directly in a test is checked the same way one read from the environment is.
Invalid values raise `ValueError`, which at startup means the process fails fast.

## Data Flow

```
Platform Adapter                Bridge                       SessionManager / DedupeCache / Controller
─────────────────               ──────                       ──────────────────────────────────────────
bridge.handle_message(
  session_key,                                                            
  text,                  ─────►  ① Dedupe lookup_or_claim?         ─────► PromptDedupeCache
  context,                       hit ──► yield Completion             (skipped if disabled / non-resumable)
  system_prompt,                       (metadata.dedupe=…),return
  resumable=True/False)
                                ② Resolve session                  ─────► SessionManager.get_or_create(key)
                                   (or mint UUID if !resumable)            (resumable=True only)

                                ③ Capacity gate                    ─────► Semaphore.locked()?
                                   full ──► yield Completion(is_error=True,
                                              error_code="capacity_full"),
                                              mark_failed, return

                                ④ await acquire()
                                   yield Processing()
                                ⑤ async for event in controller.run(
                                       session_id, text, is_new,
                                       context, system_prompt):
                                       yield event              ─────► AgentController.run(…)

                                ⑥ Success: mark_completed
                                   Exception: mark_failed, re-raise
                                   finally: release Semaphore
```

The order matters:

1. **Dedupe runs before session resolution.** A dedupe hit must not touch `SessionManager` (no wasted disk write, no minted `session_id`) — that's one of the points of dedupe.
2. **Capacity gate runs after session resolution.** The `session_id` is needed for the rejection log line; on a capacity reject we *do* free the dedupe slot so retries aren't blocked for the full TTL.

## Session Resolution

Each call routes through one of two modes, picked by the adapter:

### `resumable=True` (default)

Used by conversational platforms (e.g. Slack: thread = session).

- `SessionManager.get_or_create(session_key)` returns `(session_id, is_new)`.
- The mapping is persisted to `sessions.json` so the same `session_key` continues the same `session_id` across process restarts.
- Each access updates `last_used`; sessions inactive longer than `session_ttl_hours` are purged.
- File-write failure rolls back the in-memory change and raises `OSError`.

### `resumable=False`

Used by one-shot triggers (e.g. heartbeat ticks, webhooks).

- Bridge mints a fresh `str(uuid.uuid4())` every call.
- `SessionManager` is not touched — no disk write, no entry in `sessions.json`.
- Two calls with the *same* `session_key` deliberately get *different* `session_id`s. The platform is saying "every invocation is conceptually independent."
- Dedupe is also skipped for `resumable=False` — a scheduled prompt firing on its cadence is meaningful, not a duplicate.

## Global Concurrency Gate

A single `asyncio.Semaphore(max_concurrent_sessions)` caps how many controller invocations are in flight across the entire process.

| Situation | Behaviour |
|-----------|-----------|
| Slots available | `acquire()` immediately, yield `Processing`, run controller |
| All slots taken | Yield `Completion(is_error=True, metadata={"error_code": "capacity_full"})` and return — **no queuing** |
| Controller raises | `finally` releases the Semaphore; exception propagates |

The reject-without-queue policy is deliberate: queuing in the Bridge would hide back-pressure from the platform, which often has its own pending-message strategy (Slack: latest-only pending slot per thread).

## Cross-Session Prompt Dedupe

Enabled when `AGENT_BRIDGE_DEDUPE_TTL_SECONDS > 0` and the Bridge is constructed with a `PromptDedupeCache` instance. Purpose: collapse alerter floods (e.g. one underlying error fanning into many Slack threads) into a single agent run while the original investigation is in flight or recently completed.

### Scope

The cache key is `(scope, canonical_text)`. `scope` is derived by stripping the trailing identifier from the `session_key`:

```python
scope = session_key.rpartition(":")[0]
# slack:C12345678:1779766966.243639  →  slack:C12345678
```

This makes dedupe **cross-thread / cross-session within a channel**, while staying **per-channel** so unrelated alerts in other channels don't collide.

Dedupe is automatically skipped for:

- Calls with `resumable=False` (heartbeats etc.)
- Empty / whitespace-only `text`
- `session_key`s without a `:` separator (no derivable scope)

### Two-stage matching

**Stage 1 — Canonicalize, then exact match.** Regex-mask volatile content into placeholders, then look up the canonical form in the cache. The substitutions (`src/agent_bridge/bridge/dedupe.py`):

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

Order is intentional: specific patterns first, so the coarse `<NUM>` rule does not eat parts of a UUID.

**Stage 2 — SimHash fuzzy fallback (optional).** When `simhash_threshold > 0`, an exact-canonical miss falls back to scanning entries in the same scope and picking the closest 64-bit SimHash fingerprint within the Hamming threshold.

- The fingerprint is built from 4-grams of the canonical text, hashed with MD5 (so it's stable across processes — Python's built-in `hash()` is `PYTHONHASHSEED`-randomized).
- Comparison is one 64-bit `XOR` + `popcount` per candidate entry — microseconds even at the LRU cap.
- `threshold=20` is a sensible starting point for catching same-template variants ("endpoint members" vs "endpoint messages"); `0` keeps the feature off.

### Cache entry lifecycle

Each entry tracks `started_at` and `completed_at`:

| State | `completed_at` | Bridge response on subsequent hit |
|-------|---------------|------------------------------------|
| In-flight | `None` | `Completion` with `metadata["dedupe"]="in_flight"` |
| Completed (within TTL) | timestamp | `Completion` with `metadata["dedupe"]="recent_hit"` |
| Expired (past TTL) | — | Treated as a miss; old entry purged on next lookup |

Both states include `metadata["first_session_key"]`, which the platform adapter can render into a clickable pointer back to the original investigation.

### Failure paths release the slot

| Trigger | Action |
|---------|--------|
| Controller raises | `mark_failed` — entry removed before re-raise |
| Controller yields `Completion(is_error=True)` | `mark_failed` — entry removed (most real failures take this path: timeout, non-zero exit, API error) |
| Capacity-full rejection | `mark_failed` — entry removed before yielding the error |
| Successful run | `mark_completed` — entry stays in the cache until TTL |

Without this, an alerter retrying after a single transient failure could be locked out for the full TTL with a pointer back to the failed run.

### Storage and eviction

- In-memory `OrderedDict`, no persistence — restart clears the cache, which is the right behaviour for a short-window dedupe.
- TTL purge runs lazily on every `lookup_or_claim` (scan + drop expired before checking).
- LRU eviction kicks in only when `len > max_entries`; `move_to_end` on hits keeps active entries around.
- All public methods are synchronous and never `await`, so under asyncio they're atomic w.r.t. other coroutines — no explicit lock needed.

## Event Flow

The Bridge yields one of two patterns per call:

**Normal path** — interleaved with whatever the controller yields:

```
Processing       (capacity acquired, agent starting)
TextDelta…       (incremental text, 0..N)
StatusUpdate…    (tool use / progress, 0..N)
UserQuestion?    (optional, pauses session until adapter replies)
Completion       (final, with cost/duration/error)
```

**Short-circuit path** — a single `Completion` and return:

| Reason | `Completion` shape |
|--------|--------------------|
| Dedupe hit | `text=":repeat: Duplicate detected — skipping."`, `metadata={"dedupe": "in_flight"\|"recent_hit", "first_session_key": …}`, `is_error=False` |
| Capacity full | `text="Too many requests being processed, please try again later."`, `metadata={"error_code": "capacity_full"}`, `is_error=True` |

The Bridge never invents `TextDelta`/`StatusUpdate`/`UserQuestion` itself — those come exclusively from the controller.

## Usage Reporting

The Bridge owns the **generic usage contract** and assembles it from what the agent reports — agents only emit raw values, platforms only render.

### The `Usage` structure

`Usage` (in `events.py`) is the canonical, agent-agnostic usage report. The same shape serves a single turn and an accumulated session total:

| Field | Meaning |
|-------|---------|
| `input_tokens` / `output_tokens` | Token counts (exclude cache) |
| `cache_read_tokens` / `cache_creation_tokens` | Cache token counts |
| `num_turns` | Agent turns within the invocation |
| `duration_api_ms` | API-only duration |
| `duration_ms` / `cost_usd` | Wall-clock duration / cost (mirrors the `Completion` fields) |
| `total_tokens` (property) | Real total: `input + output + cache_read + cache_creation` |

### How it flows

1. **Agent** parses its native output and writes raw counts into `Completion.metadata["usage"]` using the canonical keys above (it knows its own format; it does not import `Usage`).
2. **Bridge** calls `Usage.from_completion(...)` on every `Completion` it forwards, pulling token/turn detail from `metadata["usage"]` and cost/duration from the first-class `Completion` fields. The result is set on `Completion.usage`.
3. **Platform** reads the typed `Completion.usage` and renders it however it likes (Slack appends a footer; heartbeat could log it). Rendering and any on/off toggle live in the platform, not the Bridge.

Bridge-minted completions (dedupe hits, capacity rejections) carry no usage metadata, so their `usage` stays `None`.

### Session accumulation

The Bridge also maintains an **in-memory** per-session running total and sets it on `Completion.session_usage`:

- Claude's `result` event reports only the *current* invocation's usage, so the cumulative total is summed by the Bridge across turns of the same `session_id`.
- The accumulator is **not persisted** — it resets on restart (`forget_session_usage` also drops a session when its `SessionManager` entry is TTL-purged).
- A running total is only kept for sessions the Bridge tracked from their **first turn** (`is_new` at creation). For a session resumed without a tracked start — after a restart, or one that pre-existed the feature — `session_usage` is left `None` rather than reporting a misleadingly-low partial total. Non-resumable triggers never accumulate.

## Error Handling

| Failure mode | Bridge behaviour |
|--------------|------------------|
| `SessionManager` disk-write error | Raises `OSError` from `get_or_create` — propagates up to the platform adapter, which should log and surface to the user |
| Controller raises | Semaphore released, dedupe entry cleared, exception re-raised (the adapter's `try` block typically catches and posts an error message) |
| Controller yields `Completion(is_error=True)` | Forwarded as a normal event; dedupe entry is *cleared* so retries can re-run (most controller failures take this path — timeout, non-zero exit, API error) |
| Capacity full | Single error `Completion`, no Semaphore acquired, dedupe slot freed |

The semaphore release is in a `finally` block so it runs whether the controller succeeded, raised, or was cancelled.

## Adding a New Platform or Agent

The Bridge is intentionally not subclassable. To extend the system you implement one side of its protocol contract:

### `PlatformAdapter`

```python
class PlatformAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    # Periodic housekeeping; returns entries removed.
    async def cleanup(self) -> int: ...
```

The adapter is responsible for defining `session_key` format, owning per-session locking, building `text` and `system_prompt`, and choosing `resumable=True/False`. It calls `bridge.handle_message(...)` and renders the resulting `BridgeEvent`s.

In practice adapters subclass `BasePlatformAdapter` (`platforms/base.py`), which owns the shared flow: the platform callback pre-processes its native event into a `BridgeRequest`, `process()` forwards it through `handle_message` and dispatches each streamed event to an `on_*` hook, and the subclass overrides only the hooks it renders. The Protocol stays the contract; the base is implementation reuse.

See [docs/platforms/slack.md](platforms/slack.md) and [docs/platforms/heartbeat.md](platforms/heartbeat.md) for working examples.

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

The controller receives a pre-built `prompt` and `system_prompt` from whatever platform invoked it, runs the agent, and yields exactly one `Completion` at the end. It must not parse platform-specific keys out of `context`.

See [docs/agents/claude.md](agents/claude.md) for a working example.

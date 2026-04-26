# Heartbeat Adapter

The heartbeat adapter is a **proactive** platform: instead of receiving messages from a human, it fires a fixed prompt at the agent on a fixed interval. The agent decides what to do and where to put the result (e.g. write to a file, post to a chat platform via tools, open a PR). The framework only logs events for audit.

Source: `src/agent_bridge/platforms/heartbeat/`

## When to use it

- Recurring autonomous work (sweep, triage, status report, queue drain) where there is no human to wait on.
- The "task list" lives somewhere the agent can read and update itself (file, issue tracker, Notion, etc.) — heartbeat is just the clock.
- You want a single fixed prompt; per-tick variation is the agent's job.

If you need cron expressions, multiple schedules, dynamic prompt templating, or per-tick output routing, this minimal adapter is the wrong shape — extend it or add another adapter.

## Setup

### 1. Decide the cadence and the prompt

Before configuring, answer two questions:

- **How often should the agent fire?** Choose a number of minutes. Whatever you pick, the agent must be able to finish its work well under that interval — there is no internal queue, so an over-running tick will simply drop the next fire (see [Concurrency](#concurrency)).
- **What should the agent do every tick?** This prompt is sent verbatim every time. It must be:
    - **Idempotent** — the same prompt may run twice if a previous tick crashed mid-flight or if the service restarts after the interval. The agent should detect "already done" and no-op.
    - **Self-contained** — no follow-up question. Heartbeat ticks have no human; the agent cannot use `AskUserQuestion`.
    - **Bounded** — the agent should know when to stop. A prompt like "keep optimising the codebase" never terminates and will burn cost on every tick.

A reasonable shape:

> Read `./TODO.md`. Pick the first unchecked item, do it, mark it checked. If everything is checked, stop without doing anything.

### 2. Set environment variables

```bash
AGENT_BRIDGE_HEARTBEAT_ENABLED=true
AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES=60
AGENT_BRIDGE_HEARTBEAT_PROMPT=Read ./TODO.md and work on the next unchecked item. If everything is done, do nothing.
# Optional — defaults to ./heartbeat.json
AGENT_BRIDGE_HEARTBEAT_STATE_PATH=./heartbeat.json
```

Validation runs at startup. Invalid configurations raise `ValueError` and prevent the service from coming up:

| Failure | Error |
|---------|-------|
| `ENABLED=true` but `INTERVAL_MINUTES` ≤ 0 | `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES must be positive, got 0` |
| `ENABLED=true` but `PROMPT` empty | `AGENT_BRIDGE_HEARTBEAT_PROMPT is required when heartbeat is enabled` |

### 3. Verify it is running

After starting the service, you should see in the log:

```
[INFO] Heartbeat adapter enabled (interval=60m)
[INFO] Heartbeat: firing on startup (last_run=None, interval=60m)
[INFO] Heartbeat tick: session_key=heartbeat:tick:2026-04-26T... prompt='...'
[INFO] Heartbeat heartbeat:tick:...: processing
[INFO] Heartbeat heartbeat:tick:...: completion cost=$0.0123 duration=4521ms
[INFO] Heartbeat heartbeat:tick:...: final reply: <agent's reply>
```

A state file appears at `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` after the first tick:

```json
{
  "last_run": "2026-04-26T12:00:00.000000+00:00"
}
```

If you see the `Heartbeat adapter enabled` line but no `Heartbeat tick:` line within the configured interval, the adapter is sleeping until `last_run + interval`. Delete the state file to force an immediate fire on the next startup.

## Configuration Reference

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Master switch — anything other than the literal string `true` (case-insensitive) keeps it off |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | Yes (if enabled) | — | Must be `> 0`. The smallest sensible value is whatever ensures the agent can finish well under one interval |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | Yes (if enabled) | — | Fixed string sent each tick. No templating |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | Path to the JSON file that stores `last_run` |

## Session Model

**Each tick is an independent session.**

The session key is unique per tick:

```
heartbeat:tick:{iso_timestamp_utc_with_microseconds}
```

Because the key is unique, `SessionManager` always creates a fresh session ID — the agent has **no memory** of previous heartbeats. If you want continuity across ticks, the agent must persist state itself (file, database, issue tracker, etc.).

Old session entries fall off naturally via the standard session TTL purge (`AGENT_BRIDGE_SESSION_TTL_HOURS`); the periodic cleanup task removes them an hour at a time.

## Lifecycle

```
start()                                stop()
  │                                      │
  ▼                                      ▼
read state file                  set stopping event
  │                                      │
  ▼                                      ▼
last_run + interval ≤ now? ──yes──► fire immediately ──┐
  │ no                                                  │
  ▼                                                     │
sleep until last_run + interval ────────────────────────┤
  │                                                     │
  └─────────────────────────────────────────► fire ─────┤
                                              │         │
                                              ▼         │
                                     write state file   │
                                              │         │
                                              ▼         │
                                       sleep interval ──┘
```

### `start()`

1. Reads `last_run` from the state file. Missing or malformed file → treat as "never run".
2. Computes `initial_delay`:
    - `last_run` missing or `now - last_run >= interval` → `initial_delay = 0` (fire immediately)
    - otherwise → `initial_delay = (last_run + interval) - now`
3. Spawns a single background task running `_run_loop(initial_delay)`. The call returns immediately — the loop runs concurrently for the rest of the process lifetime.

### `_run_loop`

After the initial delay, the loop alternates between firing and sleeping:

```
while not stopping:
    fire_once()
    if sleep_or_stop(interval_seconds): return
```

`sleep_or_stop` is implemented as `asyncio.wait_for(stopping.wait(), timeout=...)`:

- If the timeout elapses → continue to the next fire.
- If the stopping event is set → return cleanly (the wait completes early).

This means **shutdown latency is effectively zero**: a `stop()` call wakes the sleeping loop on the next event-loop tick.

### `stop()`

1. Sets the stopping event (wakes any in-progress sleep).
2. Cancels the scheduler task.
3. Awaits the task with `CancelledError` swallowed.

`stop()` is idempotent and safe to call before `start()` — if `_task is None` it is a no-op.

## Tick Execution

One tick (`_fire_once`) does this, in order:

1. **Capture timestamp** — `fired_at = utcnow()`.
2. **Build session key** — `f"heartbeat:tick:{fired_at.isoformat()}"`. Microsecond precision in the iso format makes collisions effectively impossible.
3. **Build context** — `{"source": "heartbeat", "fired_at": "<iso>"}`. See [Context](#context).
4. **Call the bridge** — `bridge.handle_message(session_key, prompt, context)`. This goes through the global concurrency gate; on capacity overflow the bridge yields a single error `Completion` and the tick effectively no-ops (with an ERROR log).
5. **Consume the event stream** — every `BridgeEvent` is logged (see [Logging](#logging)). The adapter does not render events to any external surface; the agent's tools are the only output channel.
6. **Catch exceptions** — anything raised during iteration is caught and logged via `logger.exception`. The tick does not crash the loop.
7. **Write state file** (in a `finally` block) — records `fired_at` as the new `last_run`, regardless of success or failure.

The `finally` write is deliberate: it ensures that a crashing tick still advances the clock, so the loop can not enter a tight retry. The trade-off is that a crashed tick is **not** retried — it is simply skipped, just like a slow tick that overran the interval.

## Restart Catch-up

On `start()`, the adapter consults the state file:

| Condition | Behavior |
|-----------|----------|
| State file missing | Fire immediately |
| `now - last_run >= interval` | Fire immediately |
| `now - last_run < interval` | Sleep until `last_run + interval`, then fire |
| State file present but malformed JSON / unparseable timestamp | Treated as missing (logged as a warning) |

This means a service restart **never** causes a missed tick to be lost forever, and **never** causes a flood of catch-up runs — at most one extra tick fires on startup.

If the agent's tasks are not safe to run twice (e.g. it sends an email rather than updating idempotent state), the prompt itself must guard against re-execution. The framework cannot tell whether a tick succeeded or merely advanced the clock.

## State File

Format (single key, JSON):

```json
{
  "last_run": "2026-04-26T12:00:00.000000+00:00"
}
```

| Aspect | Detail |
|--------|--------|
| Path | `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` (default `./heartbeat.json`) |
| Written | After **every** fire (success or failure), inside `_fire_once`'s `finally` block |
| Parent dir | Created automatically with `mkdir(parents=True, exist_ok=True)` |
| Read errors | Logged at WARNING; treated as "missing" — the next tick fires immediately |
| Write errors | Logged at ERROR; the tick still completes, but next restart will treat the state as whatever the previous successful write recorded |

Manual operations:

- **Force an immediate fire on next startup** — delete the file.
- **Delay the next fire** — write a future timestamp into `last_run`.
- **Inspect last activity** — read the file; the timestamp is the wall-clock UTC time of the most recent fire attempt.

## Logging

The adapter consumes `BridgeEvent`s from the bridge but does not render them anywhere user-facing. Each event is logged:

| Event | Log level | Format |
|-------|-----------|--------|
| `Processing` | INFO | `Heartbeat <key>: processing` |
| `TextDelta` | DEBUG | `Heartbeat <key>: text +<n> chars` (no body — avoid log spam from streaming chunks) |
| `StatusUpdate` | INFO | `Heartbeat <key>: status=<status> detail=<detail>` |
| `UserQuestion` | WARNING | `Heartbeat <key>: agent asked N question(s) but no human can answer: ...` |
| `Completion` (success) | INFO × 2 | One INFO line with `cost=$X duration=Yms`, one INFO line with the full final reply |
| `Completion` (error) | ERROR | `Heartbeat <key>: completion error cost=$X duration=Yms text=...` |

The event stream is the audit trail. To see what heartbeat ticks have done over time:

```bash
grep 'Heartbeat heartbeat:tick:' service.log
```

To see only failures:

```bash
grep 'Heartbeat heartbeat:tick:' service.log | grep -E '(error|WARNING|ERROR)'
```

### Why `UserQuestion` is a warning, not a feature

If the agent invokes `AskUserQuestion` during a heartbeat run, no one will answer. The adapter logs a warning and lets the run terminate naturally (the bridge produces a `Completion` once the agent gives up). Design the prompt and tool permissions so this never happens — for example, instruct the agent to assume sensible defaults rather than asking.

## Concurrency

Heartbeat ticks share the global `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` semaphore. If the bridge has no free slot when a tick fires, the bridge yields a `Completion` with `metadata["error_code"] = "capacity_full"`, the adapter logs an ERROR, and the next tick is scheduled normally — there is no retry, no queueing.

This has two practical consequences:

- **A slow tick can drop the next tick.** If a tick takes longer than `interval_minutes` to finish, the next fire arrives while the previous is still holding a slot, and the bridge may reject it. Either widen the interval, raise the concurrency limit, or make the prompt cheaper.
- **A heartbeat tick is not privileged.** It competes for the same slot as any other adapter. The first to acquire wins; the loser sees `capacity_full`. Design accordingly.

There is no internal queueing inside the heartbeat adapter — a missed tick is permanently missed, not deferred.

## Context

Each tick passes this context to the agent:

```python
{
    "source": "heartbeat",
    "fired_at": "<iso_timestamp_utc>",
}
```

Agents can branch on `context["source"]` to behave differently for proactive vs. interactive runs — for example, run with stricter permissions, decline to ask questions, write output to a fixed location instead of replying inline.

## Limitations / Non-goals

- **One schedule per process.** Multiple cadences require multiple processes or extending the adapter to support a list of schedules.
- **No catch-up replay.** Only one fire on startup, not N missed fires.
- **No per-tick prompt variation.** The prompt is a fixed string; templating like `{now}` or `{last_run}` is not implemented — let the agent compute what it needs at runtime.
- **No alerting.** Errors only surface in logs. If you need pager alerts, attach a log handler that forwards ERROR records, or have the agent post to a control channel itself.
- **No memory between ticks.** Each tick is a fresh session. If the agent needs to remember anything across ticks, it must persist the state itself.

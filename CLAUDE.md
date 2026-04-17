# Agent Bridge — Developer Guide

## What is this project?

A modular bridge service connecting **chat platforms** (e.g. Slack) to **AI agents** (e.g. Claude Code). The architecture enforces a strict three-layer separation:

```
Platform Adapter  ←→  Bridge  ←→  Agent Controller
(session owner)       (router)    (purely invoked)
```

Each layer has one job and knows nothing about the others' internals.

## Architecture

### Three-layer design

| Layer | Responsibility | Example |
|-------|---------------|---------|
| **Platform Adapter** (`platforms/`) | Defines session semantics, manages per-session locking, renders agent events into platform-native messages | `SlackAdapter` — thread = session |
| **Bridge** (`bridge.py`, `session.py`) | Pure routing: resolves session keys → session IDs, enforces global concurrency, forwards events | No platform or agent knowledge |
| **Agent Controller** (`agents/`) | Receives `(session_id, prompt, is_new, context)`, executes, yields `BridgeEvent`s | `ClaudeController` — spawns `claude -p` subprocess |

### Event model

All agent output flows through a generic event system. Platforms only consume these types:

| Event | Purpose |
|-------|---------|
| `Processing` | Slot acquired, agent starting |
| `TextDelta` | Incremental text chunk |
| `StatusUpdate` | Agent performing an action (tool use, etc.) |
| `UserQuestion` | Agent asking the user for input |
| `Completion` | Agent finished (includes cost, duration, error) |

Events are defined in `src/agent_bridge/events.py`. Agent-internal events (thinking, tool results) are translated within each agent module — never exposed to platforms.

### Protocols

- `AgentController` — `run(session_id, prompt, is_new, context) → AsyncIterator[BridgeEvent]`
- `PlatformAdapter` — `start()`, `stop()`

Defined in `src/agent_bridge/protocols.py`. New agents/platforms implement these.

### Session management

- Platform defines session key format (e.g. `slack:{channel}:{thread_ts}`)
- `SessionManager` maps session keys → UUIDs with TTL expiry
- Persisted to JSON file, auto-purges expired sessions
- Bridge resolves keys — it doesn't define what a "session" means

### Data flow

```
1. User message arrives at Platform Adapter
2. Adapter constructs session_key, acquires per-session lock
3. Bridge.handle_message(session_key, text, context)
   → SessionManager resolves key → (session_id, is_new)
   → Semaphore check (reject if capacity full)
   → AgentController.run(session_id, prompt, is_new, context)
4. Agent yields BridgeEvents
5. Adapter renders events as platform-native messages
```

## Tech stack

| Component | Choice | Notes |
|-----------|--------|-------|
| Language | **Python 3.12+** | Uses `type X = ...` syntax, `match` statements, `X \| Y` unions |
| Package manager | **uv** | Fast, pyproject.toml native |
| Build backend | **hatchling** | |
| Slack SDK | **slack-bolt** (optional dep) | Async Socket Mode |
| Async HTTP | **aiohttp** | Required by slack-bolt |
| Env config | **python-dotenv** | `.env` file loading |
| Testing | **pytest + pytest-asyncio** | `asyncio_mode = "auto"` |
| Claude CLI | `claude -p` with `--output-format stream-json` | Non-interactive, real-time streaming |

## Project structure

```
src/agent_bridge/
├── __init__.py          # Entry point: wires adapter + bridge + agent, signal handling, cleanup
├── config.py            # BridgeConfig (store path, TTL, concurrency)
├── bridge.py            # Pure routing + global concurrency (Semaphore)
├── events.py            # BridgeEvent type union (Processing, TextDelta, StatusUpdate, UserQuestion, Completion)
├── session.py           # SessionManager (key → UUID, TTL, JSON persistence)
├── protocols.py         # AgentController + PlatformAdapter protocol interfaces
├── agents/
│   └── claude/
│       ├── config.py    # ClaudeConfig (work_dir, permission_mode, timeout)
│       ├── controller.py # Subprocess spawner, stream reader, timeout handling
│       └── events.py    # Claude stream-json parser → BridgeEvent converter
└── platforms/
    └── slack/
        ├── config.py    # SlackConfig (bot_token, app_token)
        └── adapter.py   # Event handlers, per-session state machine, message rendering
```

## Conventions

### Code style

- **No docstrings** on obvious methods. Only add comments where logic is non-obvious.
- **Frozen dataclasses** for config objects (`@dataclass(frozen=True)`)
- **Plain dataclasses** for events and internal state
- **`from __future__ import annotations`** at top of every module
- **Type aliases** use Python 3.12 `type X = ...` syntax
- **Pattern matching** (`match`/`case`) for event dispatch
- **Protocols** over ABC for interface contracts

### Naming

- Environment variables: `AGENT_BRIDGE_` prefix for all config
- Session keys: `{platform}:{scope}:{identifier}` (e.g. `slack:{channel}:{thread_ts}`)
- Config classes: `{Component}Config` with `from_env()` classmethod + `_validate()`
- Modules: lowercase, no underscores in package names

### Error handling

- Config validation raises `ValueError` at startup — fail fast
- Runtime errors logged, not raised — platform adapters handle gracefully
- Subprocess failures yield error `Completion` events
- Session persistence failures roll back in-memory state

### Async patterns

- `asyncio.Semaphore` for global concurrency gating
- `asyncio.Lock` per session for serialization (owned by platform adapter)
- Background tasks for stderr draining, periodic cleanup
- `AsyncIterator[BridgeEvent]` for streaming (async generators with `yield`)

### Testing

- Run tests: `uv run pytest tests/ -v`
- Test files: `tests/test_*.py`
- Async tests run automatically (`asyncio_mode = "auto"`)
- Test naming: `test_{feature}_{scenario}`

### Adding a new platform adapter

1. Create `platforms/{name}/config.py` — config with `from_env()` + `_validate()`
2. Create `platforms/{name}/adapter.py` — implements `PlatformAdapter` protocol
3. Define session key format (e.g. `discord:{guild}:{channel}`)
4. Own per-session locking strategy
5. Consume `BridgeEvent`s from `bridge.handle_message()`
6. Wire up in `__init__.py`
7. Add documentation in `docs/platforms/{name}.md`

### Adding a new agent

1. Create `agents/{name}/config.py` — config with `from_env()` + `_validate()`
2. Create `agents/{name}/controller.py` — implements `AgentController` protocol
3. Create `agents/{name}/events.py` — parse agent output → `BridgeEvent`s
4. `run()` yields only generic `BridgeEvent`s — agent-internal events stay internal
5. Wire up in `__init__.py`
6. Add documentation in `docs/agents/{name}.md`

### Documentation maintenance

When modifying any component, update the corresponding documentation:
- Platform adapter changes → update `docs/platforms/{name}.md`
- Agent changes → update `docs/agents/{name}.md`
- Core bridge/event/session changes → update this file and `README.md`
- New env vars → update `.env.example` and the relevant docs

## Running

```bash
# Install dependencies
uv sync

# Run the bridge
uv run agent-bridge

# Run tests
uv run pytest tests/ -v
```

## Environment variables

All config loads from `.env` via python-dotenv. See `.env.example` for the full list.

| Variable | Required | Default | Component |
|----------|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | Yes | — | Claude CLI |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Claude |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Claude |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Claude |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Bridge |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Bridge |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Bridge |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | Global |

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

- `AgentController` — `run(session_id, prompt, is_new, context, system_prompt) → AsyncIterator[BridgeEvent]`. The platform adapter builds `prompt` (already pre-tagged with sender identity if needed) and `system_prompt` (platform-flavored directives); the agent forwards them as-is. The agent must not interpret platform-specific keys out of `context`.
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
3. Adapter builds `text` (pre-tagged with sender identity) and `system_prompt` (platform directives) — the agent stays platform-agnostic
4. Bridge.handle_message(session_key, text, context, system_prompt, resumable)
   → If `resumable=True`: SessionManager resolves key → (session_id, is_new), persisted on disk
   → If `resumable=False`: bridge mints a fresh ephemeral UUID, SessionManager untouched
   → Semaphore check (reject if capacity full)
   → AgentController.run(session_id, prompt, is_new, context, system_prompt)
5. Agent yields BridgeEvents
6. Adapter renders events as platform-native messages
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
| Lint / format | **ruff** | line-length 100, rules `E,F,W,I,B,UP,SIM,RUF` (`[tool.ruff]` in pyproject) |
| Docs site | **MkDocs Material** (`docs` dependency group) | `mkdocs.yml` nav; deployed to GitHub Pages |
| Claude CLI | `claude -p` with `--output-format stream-json` | Non-interactive, real-time streaming |

## Project structure

```
src/agent_bridge/
├── __init__.py          # Re-exports main/main_sync only
├── app.py               # Entry point: ConfigSource + registries wiring, signals, periodic cleanup
├── config.py            # BridgeConfig (store path, TTL, concurrency)
├── config_loader.py     # ConfigSource (env > YAML > default), $(VAR) secrets, file discovery
├── bridge.py            # Pure routing + global concurrency (Semaphore)
├── events.py            # BridgeEvent type union (Processing, TextDelta, StatusUpdate, UserQuestion, Completion)
├── session.py           # SessionManager (key → UUID, TTL, JSON persistence)
├── protocols.py         # AgentController + PlatformAdapter protocol interfaces
├── agents/
│   ├── registry.py      # name → build(source) → AgentController
│   ├── claude/
│   │   ├── config.py    # ClaudeConfig (work_dir, permission_mode, timeout)
│   │   ├── controller.py # Subprocess spawner, stream reader, timeout handling
│   │   └── events.py    # Claude stream-json parser → BridgeEvent converter
│   ├── codex/
│   │   ├── config.py    # CodexConfig (work_dir, model, sandbox, timeout, session map)
│   │   ├── controller.py # codex exec subprocess + bridge-session → thread-id map
│   │   └── events.py    # codex exec --json JSONL parser → BridgeEvent converter
│   └── opencode/
│       ├── config.py    # OpencodeConfig (work_dir, model, timeout, session map)
│       ├── controller.py # opencode run subprocess + bridge-session → ses-id map
│       └── events.py    # opencode run --format json JSONL parser → BridgeEvent converter
└── platforms/
    ├── registry.py      # name → build(source, bridge, session_manager) → adapter | None
    ├── slack/
    │   ├── config.py    # SlackConfig (bot_token, app_token)
    │   └── adapter.py   # Event handlers, per-session state machine, message rendering
    ├── telegram/
    │   ├── config.py    # TelegramConfig (bot_token, allow_chats, poll timeout, state path)
    │   └── adapter.py   # getUpdates long-poll loop, mention/reply filtering, placeholder-edit rendering
    ├── line/
    │   ├── config.py    # LineConfig (channel secret/token, webhook host/port/path)
    │   └── adapter.py   # Webhook server, HMAC signature check, buffered reply→push rendering
    ├── api/
    │   ├── config.py    # ApiConfig (enabled, host, port, auth_token)
    │   └── adapter.py   # POST /v1/messages (buffered JSON + SSE), bearer auth, healthz
    └── heartbeat/
        ├── config.py    # HeartbeatConfig (interval, prompt, state path)
        └── adapter.py   # Scheduled ticks, one-shot (non-resumable) sessions
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
- Config classes: `{Component}Config` with `from_source(source)` classmethod (+ `from_env()` delegating to an empty source) + `_validate()`. Every field reads through `source.get(env_key, yaml_path, default)` — YAML keys per `docs/configuration.md`
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

- Run all tests: `uv run pytest` — unit only: `uv run pytest -m "not integration"`, end-to-end only: `uv run pytest -m integration`
- Layout: `tests/unit/` mirrors `src/agent_bridge/` (`tests/unit/platforms/slack/`, `tests/unit/agents/claude/`, ...); `tests/integration/` holds end-to-end tests marked with `pytestmark = pytest.mark.integration`
- Every test directory has an `__init__.py` (package mode); shared fixtures live in `tests/conftest.py`, reusable fakes in `tests/helpers/` (`FakeAgentController`, `FakeBridge`, `collect_events`, `install_fake_cli`)
- All tests are offline — external CLIs are faked with generated scripts (`install_fake_cli` + `prepend_path`), never real tokens/network
- Async tests run automatically (`asyncio_mode = "auto"`)
- Test naming: `test_{feature}_{scenario}`
- Full guide (component checklists + templates): `docs/testing.md`

### Linting & formatting

- `uv run ruff check .` (lint; `--fix` to auto-fix) and `uv run ruff format .` must both be clean — CI enforces `ruff check` + `ruff format --check`
- Config lives in `[tool.ruff]` in `pyproject.toml`; the only per-file ignore is `SIM117` in `tests/**`
- `uv run pre-commit install` wires ruff (pre-commit stage) and commitlint (commit-msg stage) hooks

### Commits

- Follow [Conventional Commits](https://www.conventionalcommits.org/) with **lowercase** types: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `perf:`, `style:`, `build:`, `revert:`. (Not `Feat:`/`Fix:`.)
- Optional scope in parens, imperative subject, no trailing period: `fix(slack): release dedupe slot on error`.
- The type drives the automated release: `feat:` → MINOR, `fix:`/`perf:` → PATCH, `feat!:` or a `BREAKING CHANGE:` footer → breaking (MINOR while in 0.x). Other types cut no release.
- The `commitlint` PR check rejects non-conforming commits. Since PRs merge with merge commits, **each commit on a branch** (not just the PR title) must conform.
- Optional local guard: `uv run pre-commit install` wires the commitlint `commit-msg` hook (same config as CI) plus ruff lint/format `pre-commit` hooks (see `docs/releasing.md`).
- Releases are automated from these messages — never hand-edit `[project].version`. See `## Releasing` and `docs/releasing.md`.

### Adding a new platform adapter

1. Create `platforms/{name}/config.py` — config with `from_env()` + `_validate()`
2. Create `platforms/{name}/adapter.py` — implements `PlatformAdapter` protocol
3. Define session key format (e.g. `discord:{guild}:{channel}`)
4. Own per-session locking strategy
5. Build the text the agent receives — pre-tag the prompt with sender identity if your platform has one (e.g. `[name]: text`); pass `None` if it doesn't (proactive triggers)
6. Build the `system_prompt` — platform-flavored directives (chat framing, scheduled-invocation framing, webhook-trigger framing, etc.). The agent forwards it as-is
7. Decide `resumable`: pass `True` (default) if the same `session_key` should be able to resume the same session later (e.g. chat threads); pass `False` for one-shot triggers where every call must be a fresh, untracked session (e.g. heartbeat ticks)
8. Consume `BridgeEvent`s from `bridge.handle_message(session_key, text, context, system_prompt, resumable)`
9. Register a builder in `platforms/registry.py` — return `None` when unconfigured/disabled; import optional third-party deps lazily inside the builder
10. Add documentation in `docs/platforms/{name}.md`; update `.env.example`, `agent-bridge.example.yaml`, and the env tables

### Adding a new agent

1. Create `agents/{name}/config.py` — config with `from_env()` + `_validate()`
2. Create `agents/{name}/controller.py` — implements `AgentController` protocol
3. Create `agents/{name}/events.py` — parse agent output → `BridgeEvent`s
4. `run()` yields only generic `BridgeEvent`s — agent-internal events stay internal
5. Treat `system_prompt` and `prompt` as opaque strings built by the platform — do not parse `context` for platform-specific keys
6. Implement `cleanup_session(session_id)` (no-op is valid) and register a builder in `agents/registry.py`
7. Add documentation in `docs/agents/{name}.md`; update `.env.example`, `agent-bridge.example.yaml`, and the env tables

### Documentation maintenance

When modifying any component, update the corresponding documentation:
- Platform adapter changes → update `docs/platforms/{name}.md`
- Agent changes → update `docs/agents/{name}.md`
- Core bridge/event/session changes → update this file and `README.md`
- New env vars → update `.env.example` and the relevant docs
- New docs pages → add them to the `nav` in `mkdocs.yml`; `uv run mkdocs build --strict` must pass (broken links fail the build)

## Running

```bash
# Install dependencies
uv sync

# Run the bridge
uv run agent-bridge

# Run tests
uv run pytest

# Lint + format check
uv run ruff check . && uv run ruff format --check .

# Docs site: live preview / strict build
uv sync --group docs
uv run mkdocs serve
uv run mkdocs build --strict
```

## Releasing

Versioning is automated — **do not hand-edit `[project].version`**. On push to `main`,
python-semantic-release reads the [Conventional Commits](https://www.conventionalcommits.org/)
since the last tag (see `### Commits` for the format), bumps the version, tags `vX.Y.Z`,
writes `CHANGELOG.md`, and publishes to PyPI via OIDC. While in 0.x, breaking changes
bump the minor (not 1.0.0). Full process + one-time setup: `docs/releasing.md`.

## Environment variables

Config resolves as **env var > YAML file > built-in default**. `.env` is loaded once at
the entry point (`app.main`) via python-dotenv. Every variable has a matching YAML key
(nested; secrets via `$(VAR)`) — see `docs/configuration.md` for the mapping table and
`agent-bridge.example.yaml` for a full example. See `.env.example` for the env list.

| Variable | Required | Default | Component |
|----------|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | No | — | Claude CLI (only if not already authenticated via `claude login`) |
| `AGENT_BRIDGE_CONFIG` | No | — (`./agent-bridge.yaml` if present) | Global (path to YAML config file; also CLI `-c/--config`) |
| `AGENT_BRIDGE_AGENT` | No | `claude` | Global (which registered agent handles messages) |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` | No | — (allow all) | Slack (comma-separated channel-name allow-list; non-empty also blocks DMs) |
| `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` | No | (fixed English notice) | Slack (reply sent to non-allowed channels) |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED` | No | `false` | Slack (append usage/cost footer to the final reply) |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE` | No | — (built-in default) | Slack (`{placeholder}` template for the usage footer) |
| `AGENT_BRIDGE_TELEGRAM_BOT_TOKEN` | Yes (if using Telegram) | — | Telegram (bot token from @BotFather) |
| `AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS` | No | — (allow all) | Telegram (comma-separated chat-id allow-list; others silently ignored) |
| `AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS` | No | `30` | Telegram (getUpdates long-poll wait; 0 = short polling) |
| `AGENT_BRIDGE_TELEGRAM_STATE_PATH` | No | `./telegram.json` | Telegram (persists last processed update_id) |
| `AGENT_BRIDGE_TELEGRAM_API_BASE_URL` | No | `https://api.telegram.org` | Telegram (Bot API base URL; tests use a fake server) |
| `AGENT_BRIDGE_LINE_CHANNEL_SECRET` | Yes (if using LINE) | — | LINE (channel secret; webhook signature verification) |
| `AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN` | Yes (if using LINE) | — | LINE (channel access token; Messaging API) |
| `AGENT_BRIDGE_LINE_WEBHOOK_HOST` | No | `0.0.0.0` | LINE (webhook server bind address) |
| `AGENT_BRIDGE_LINE_WEBHOOK_PORT` | No | `8080` | LINE (webhook server port; 0 = ephemeral, for tests) |
| `AGENT_BRIDGE_LINE_WEBHOOK_PATH` | No | `/line/webhook` | LINE (webhook endpoint path) |
| `AGENT_BRIDGE_LINE_API_BASE_URL` | No | `https://api.line.me` | LINE (Messaging API base URL; tests use a fake server) |
| `AGENT_BRIDGE_API_ENABLED` | No | `false` | API (generic HTTP POST entry point; explicit opt-in) |
| `AGENT_BRIDGE_API_HOST` | No | `127.0.0.1` | API (bind address; loopback by default) |
| `AGENT_BRIDGE_API_PORT` | No | `8081` | API (server port; 0 = ephemeral, for tests) |
| `AGENT_BRIDGE_API_AUTH_TOKEN` | No | — (no auth) | API (bearer token required on `/v1` requests when set) |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Claude |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Claude |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Claude |
| `AGENT_BRIDGE_CLAUDE_EFFORT` | No | `xhigh` | Claude (one of `low`, `medium`, `high`, `xhigh`, `max`) |
| `AGENT_BRIDGE_CODEX_WORK_DIR` | No | `.` | Codex |
| `AGENT_BRIDGE_CODEX_MODEL` | No | — (CLI default) | Codex (optional `-m` model override) |
| `AGENT_BRIDGE_CODEX_SANDBOX` | No | `workspace-write` | Codex (one of `read-only`, `workspace-write`, `danger-full-access`) |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | No | `600` | Codex |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | No | `./codex-sessions.json` | Codex (bridge-session → codex-thread mapping) |
| `AGENT_BRIDGE_OPENCODE_WORK_DIR` | No | `.` | OpenCode |
| `AGENT_BRIDGE_OPENCODE_MODEL` | No | — (CLI default) | OpenCode (optional `--model` override, `provider/model` form) |
| `AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS` | No | `600` | OpenCode |
| `AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH` | No | `./opencode-sessions.json` | OpenCode (bridge-session → opencode-session mapping) |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Bridge |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Bridge |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Bridge |
| `AGENT_BRIDGE_DEDUPE_TTL_SECONDS` | No | `0` | Bridge (cross-session prompt dedupe; 0 disables) |
| `AGENT_BRIDGE_DEDUPE_MAX_ENTRIES` | No | `512` | Bridge (dedupe LRU cap) |
| `AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD` | No | `0` | Bridge (Hamming threshold for fuzzy match; 0 = exact canonical only) |
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Heartbeat |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | Yes (if heartbeat enabled) | — | Heartbeat |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | Yes (if heartbeat enabled) | — | Heartbeat |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | Heartbeat |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | Global |

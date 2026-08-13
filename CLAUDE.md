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
| **Bridge** (`bridge/`) | Pure routing: resolves session keys → session IDs, enforces global concurrency, forwards events. Also owns the shared contract (`events.py`, `protocols.py`) both other layers implement against | No platform or agent knowledge |
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

Events are defined in `src/agent_bridge/bridge/events.py`. Agent-internal events (thinking, tool results) are translated within each agent module — never exposed to platforms.

### Protocols

- `AgentController` — `run(session_id, prompt, is_new, context, system_prompt) → AsyncIterator[BridgeEvent]`. The platform adapter builds `prompt` (already pre-tagged with sender identity if needed) and `system_prompt` (platform-flavored directives); the agent forwards them as-is. The agent must not interpret platform-specific keys out of `context`.
- `PlatformAdapter` — `start()`, `stop()`
- `MessageRouter` — `handle_message(session_key, text, context, system_prompt, resumable) → AsyncIterator[BridgeEvent]`. The interface adapters send messages through; `Bridge` is the production implementation. Adapters depend on this protocol, not the concrete class, so tests can substitute fakes.

Defined in `src/agent_bridge/bridge/protocols.py`. New agents/platforms implement these.

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
| Coverage | **pytest-cov** | `fail_under = 75` ratchet; `[tool.coverage.report]` in pyproject.toml |
| Dependency audit | **pip-audit** | PR gate + weekly schedule; `.github/workflows/audit.yml` |
| Secrets scanning | **gitleaks** | pre-commit hook (staged diff) + CI (commit history); same workflow |
| Lint / format | **ruff** | one tool for both; `[tool.ruff]` in pyproject.toml |
| Type checking | **pyright (strict)** | `src/` only; `[tool.pyright]` in pyproject.toml |
| Claude CLI | `claude -p` with `--output-format stream-json` | Non-interactive, real-time streaming |

## Project structure

One package per layer. `agents/` and `platforms/` both depend on `bridge/`;
`bridge/` depends on neither — it holds the shared contract they plug into.
`app.py` is the only module that knows all three.

```
src/agent_bridge/
├── __init__.py          # Empty — importing a submodule must not drag in the whole app
├── app.py               # Entry point: builds everything from an AppConfig, signal handling, cleanup
├── config.py            # AppConfig — aggregates every layer's config; the only caller of load_dotenv
├── env.py               # Typed env readers (env_str/int/float/bool/path/csv) — the only module reading os.environ
├── bridge/
│   ├── router.py        # Bridge — pure routing + global concurrency (Semaphore)
│   ├── events.py        # BridgeEvent type union (Processing, TextDelta, StatusUpdate, UserQuestion, Completion)
│   ├── protocols.py     # AgentController + PlatformAdapter + MessageRouter protocol interfaces
│   ├── session.py       # SessionManager (key → UUID, TTL, JSON persistence)
│   ├── dedupe.py        # PromptDedupeCache (optional cross-session prompt dedupe)
│   └── config.py        # SessionConfig + RouterConfig + DedupeConfig, aggregated by BridgeConfig
├── agents/
│   └── claude/
│       ├── config.py    # ClaudeConfig (work_dir, permission_mode, timeout, effort, cli_path)
│       ├── controller.py # Subprocess spawner, stream reader, timeout handling
│       └── events.py    # Claude stream-json parser → BridgeEvent converter
└── platforms/
    ├── slack/
    │   ├── config.py    # SlackConfig (bot_token, app_token, allow-list, usage report, render knobs)
    │   └── adapter.py   # Event handlers, per-session state machine, message rendering
    └── heartbeat/
        ├── config.py    # HeartbeatConfig (interval, prompt, state path)
        └── adapter.py   # Periodic one-shot triggers (resumable=False)
```

## Conventions

### Code style

- **Ruff** enforces lint + format (config: `pyproject.toml` `[tool.ruff]`; the CI
  lint job and pre-commit hooks run the same checks). Before committing:
  `uv run ruff check --fix && uv run ruff format`
- Complexity is gated at C901 = 10 — no exemptions: the last `# noqa: C901`
  hotspots were refactored away. Decompose instead of suppressing.
- Suppress a rule only with a targeted `# noqa: <code>` plus a one-line reason
  (see the `assert`/S101 narrowing sites) — never blanket-disable in config.
- **Pyright strict** on `src/` (`uv run pyright`). slack-bolt is untyped: its
  `Any`-ness is contained at the adapter boundary (params annotated `Any`;
  Unknown-type rules relaxed for the slack package only). Suppress only with
  targeted `# pyright: ignore[<rule>]` plus a reason.
- Dataclass collection fields parametrize the factory —
  `field(default_factory=list[str])`, not `field(default_factory=list)` —
  or pyright infers `list[Unknown]`.
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
- Config classes: `{Component}Config` with `from_env(env)` classmethod + `_validate()`
- Modules: lowercase, no underscores in package names

### Configuration

One component, one config class. Every component takes its config as the first
constructor argument — `Component(config, *collaborators)` — and reads nothing
from the environment itself.

```
AppConfig            (src/agent_bridge/config.py)  ← app.py builds the system from this alone
├── BridgeConfig     .session / .router / .dedupe  → SessionManager / Bridge / PromptDedupeCache
├── ClaudeConfig                                   → ClaudeController
├── SlackConfig     | None                         → SlackAdapter    (None ⇒ not configured)
├── HeartbeatConfig | None                         → HeartbeatAdapter (None ⇒ disabled)
├── log_level
└── cleanup_interval_seconds
```

- **Reading env**: only `src/agent_bridge/env.py` touches `os.environ`, through typed
  readers (`env_str`, `env_bool`, `env_int`, `env_float`, `env_path`, `env_csv`) that
  share one truthy rule, one blank-handling rule and one error-message shape.
  `load_dotenv()` is called exactly once, by `AppConfig.from_env()`.
- **`from_env(env: Env = PROCESS_ENV)`**: every config reads from an injectable mapping.
  Tests pass a plain dict — no `monkeypatch.setenv`, and a developer's local `.env`
  can't leak into them.
- **`from_env_optional()`**: for components that may be absent (Slack, heartbeat).
  Returns `None` for "not configured", raises for "configured wrong".
- **Validation**: `_validate()` is called from `__post_init__`, so *every* construction
  path is checked — including configs built directly in tests. It does value checks only.
- **Prerequisite probes**: checks that touch the filesystem, git or the network go in
  `check_prerequisites()`, called only by `from_env()`. Startup still fails fast; holding
  a config in memory stays cheap and side-effect free.
- **Defaults live in the dataclass**, and `from_env` must pass the same default to its
  reader. A `Config.from_env({}) == Config()` test guards the drift.
- Knobs that aren't user-facing (Slack's render throttle and message ceiling, the app's
  cleanup interval) are config fields with `DEFAULT_*` constants but no env var — tests
  tune them through the config instead of monkeypatching module state.

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
- Tests drive behaviour by constructing config objects, never by setting env vars.
  Env parsing is covered separately, per config class, in `test_config.py` modules
  that pass explicit dicts to `from_env`.
- The test tree mirrors `src/` (`tests/bridge/`, `tests/agents/claude/`,
  `tests/platforms/slack/`, …). Cross-layer seams are tested through the
  protocols using the typed fakes in `tests/fakes/`; `tests/contracts/`
  runs each real implementation and its fake against the same suite.
  Full design: `docs/testing.md`.
- Async tests run automatically (`asyncio_mode = "auto"`)
- Test naming: `test_{feature}_{scenario}`
- Every test carries a layer marker (`unit` / `integration` / `e2e`) — mostly
  auto-applied; see `docs/testing.md`. CI runs `-m "not e2e"` across the
  version matrix and the e2e scenarios in a separate 3.12-only job.
- Coverage runs on every pytest invocation (`addopts` in pyproject.toml) and
  gates at `fail_under = 98` — a ratchet floor at the measured baseline, raised
  as coverage improves, never lowered. A partial run (single file, `-k`, `-m`)
  undercounts coverage and trips the gate; add `--no-cov` for those.

### Commits

- Follow [Conventional Commits](https://www.conventionalcommits.org/) with **lowercase** types: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `perf:`, `style:`, `build:`, `revert:`. (Not `Feat:`/`Fix:`.)
- Optional scope in parens, imperative subject, no trailing period: `fix(slack): release dedupe slot on error`.
- The type drives the automated release: `feat:` → MINOR, `fix:`/`perf:` → PATCH, `feat!:` or a `BREAKING CHANGE:` footer → breaking (MINOR while in 0.x). Other types cut no release.
- The `commitlint` PR check rejects non-conforming commits. Since PRs merge with merge commits, **each commit on a branch** (not just the PR title) must conform.
- Optional local guard: `uv run pre-commit install` wires a `commit-msg` hook that runs the same commitlint config before each commit (see `docs/releasing.md`).
- Releases are automated from these messages — never hand-edit `[project].version`. See `## Releasing` and `docs/releasing.md`.

### Adding a new platform adapter

1. Create `platforms/{name}/config.py` — config with `from_env(env)` + `_validate()` (called from `__post_init__`), parsing through `agent_bridge/env.py`; add `from_env_optional()` if the platform can be absent, and wire it into `AppConfig`
2. Create `platforms/{name}/adapter.py` — implements `PlatformAdapter` protocol
3. Define session key format (e.g. `discord:{guild}:{channel}`)
4. Own per-session locking strategy
5. Build the text the agent receives — pre-tag the prompt with sender identity if your platform has one (e.g. `[name]: text`); pass `None` if it doesn't (proactive triggers)
6. Build the `system_prompt` — platform-flavored directives (chat framing, scheduled-invocation framing, webhook-trigger framing, etc.). The agent forwards it as-is
7. Decide `resumable`: pass `True` (default) if the same `session_key` should be able to resume the same session later (e.g. chat threads); pass `False` for one-shot triggers where every call must be a fresh, untracked session (e.g. heartbeat ticks)
8. Consume `BridgeEvent`s from `bridge.handle_message(session_key, text, context, system_prompt, resumable)`
9. Wire up in `app.py`
10. Add documentation in `docs/platforms/{name}.md`

### Adding a new agent

1. Create `agents/{name}/config.py` — config with `from_env(env)` + `_validate()` (called from `__post_init__`), parsing through `agent_bridge/env.py`; put any filesystem/git probes in `check_prerequisites()`
2. Create `agents/{name}/controller.py` — implements `AgentController` protocol
3. Create `agents/{name}/events.py` — parse agent output → `BridgeEvent`s
4. `run()` yields only generic `BridgeEvent`s — agent-internal events stay internal
5. Treat `system_prompt` and `prompt` as opaque strings built by the platform — do not parse `context` for platform-specific keys
6. Wire up in `app.py`
7. Add documentation in `docs/agents/{name}.md`

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

# Lint + format (same checks as CI and the pre-commit hooks)
uv run ruff check --fix
uv run ruff format

# Type check (strict, src/ only)
uv run pyright

# Audit locked dependencies for known vulnerabilities (same scan as CI)
uv export --format requirements-txt --no-emit-project -o requirements-audit.txt
uvx pip-audit -r requirements-audit.txt --disable-pip

# Scan git history for hardcoded secrets (the pre-commit hook covers the
# staged diff automatically; CI re-scans commit history)
gitleaks git --redact -v .
```

## Releasing

Versioning is automated — **do not hand-edit `[project].version`**. On push to `main`,
python-semantic-release reads the [Conventional Commits](https://www.conventionalcommits.org/)
since the last tag (see `### Commits` for the format), bumps the version, tags `vX.Y.Z`,
writes `CHANGELOG.md`, and publishes to PyPI via OIDC. While in 0.x, breaking changes
bump the minor (not 1.0.0). Full process + one-time setup: `docs/releasing.md`.

## Environment variables

All config loads from `.env` via python-dotenv — once, in `AppConfig.from_env()`, which
walks each layer's `{Component}Config.from_env(env)`. `src/agent_bridge/env.py` is the only
module that reads `os.environ`. Booleans accept `true`/`1`/`yes`/`on` and
`false`/`0`/`no`/`off` (case-insensitive); anything else is rejected at startup, as are
unparseable numbers. See `.env.example` for the full list.

| Variable | Required | Default | Component |
|----------|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | No | — | Claude CLI (only if not already authenticated via `claude login`) |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL` | No | — | Slack (channel to greet after Socket Mode connects) |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE` | No | — | Slack (text of that startup notice) |
| `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` | No | — (allow all) | Slack (comma-separated channel-name allow-list; non-empty also blocks DMs) |
| `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` | No | (fixed English notice) | Slack (reply sent to non-allowed channels) |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED` | No | `false` | Slack (append usage/cost footer to the final reply) |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE` | No | — (built-in default) | Slack (`{placeholder}` template for the usage footer) |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Claude |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Claude |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Claude |
| `AGENT_BRIDGE_CLAUDE_EFFORT` | No | `xhigh` | Claude (one of `low`, `medium`, `high`, `xhigh`, `max`) |
| `AGENT_BRIDGE_CLAUDE_CLI_PATH` | No | `claude` | Claude (path to the Claude Code CLI executable) |
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

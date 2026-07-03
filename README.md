# Agent Bridge

[![PyPI](https://img.shields.io/pypi/v/ai-agent-bridge)](https://pypi.org/project/ai-agent-bridge/)
[![Python](https://img.shields.io/pypi/pyversions/ai-agent-bridge)](https://pypi.org/project/ai-agent-bridge/)
[![CI](https://github.com/htkuan/ai-agent-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/htkuan/ai-agent-bridge/actions/workflows/ci.yml)
[![Docs](https://github.com/htkuan/ai-agent-bridge/actions/workflows/docs.yml/badge.svg)](https://htkuan.github.io/ai-agent-bridge/)
[![License: MIT](https://img.shields.io/github/license/htkuan/ai-agent-bridge)](https://github.com/htkuan/ai-agent-bridge/blob/main/LICENSE)

Modular bridge that connects **chat platforms** to **AI agents** — message your
coding agent from Slack, Telegram, LINE, a plain HTTP endpoint, or a scheduler.
Each layer is independent: swap platforms or agents without touching the others.

```
┌──────────────┐     ┌──────────┐     ┌──────────────┐
│   Platform   │────▶│  Bridge  │────▶│    Agent     │
│   (Slack)    │◀────│ (Router) │◀────│ (Claude Code)│
└──────────────┘     └──────────┘     └──────────────┘
  Session owner       Pure routing      Purely invoked
  Locking & render    Key → ID map      Yields events
  UI logic            Concurrency       No UI knowledge
```

**Documentation: [htkuan.github.io/ai-agent-bridge](https://htkuan.github.io/ai-agent-bridge/)**

## Supported platforms & agents

Every platform works with every agent — pick one from each table:

| Platform | Connection mode | Session scope | Docs |
|----------|----------------|---------------|------|
| **Slack** | Socket Mode (websocket, no public URL) | Thread | [Slack](https://htkuan.github.io/ai-agent-bridge/platforms/slack/) |
| **Telegram** | Long polling (no public URL) | Chat / forum topic | [Telegram](https://htkuan.github.io/ai-agent-bridge/platforms/telegram/) |
| **LINE** | Webhook (needs a public HTTPS URL) | Chat / group / room | [LINE](https://htkuan.github.io/ai-agent-bridge/platforms/line/) |
| **POST API** | Built-in HTTP server (buffered JSON + SSE) | Client-chosen `session` id | [POST API](https://htkuan.github.io/ai-agent-bridge/platforms/api/) |
| **Heartbeat** | Internal scheduler (fixed-interval prompts) | One-shot per tick | [Heartbeat](https://htkuan.github.io/ai-agent-bridge/platforms/heartbeat/) |

| Agent | CLI dependency | Docs |
|-------|---------------|------|
| **Claude Code** (default) | [`claude`](https://docs.anthropic.com/en/docs/claude-code) — `claude -p --output-format stream-json` | [Claude Code](https://htkuan.github.io/ai-agent-bridge/agents/claude/) |
| **Codex** | [`codex`](https://developers.openai.com/codex) — `codex exec --json` | [Codex](https://htkuan.github.io/ai-agent-bridge/agents/codex/) |
| **OpenCode** | [`opencode`](https://opencode.ai) — `opencode run --format json` | [OpenCode](https://htkuan.github.io/ai-agent-bridge/agents/opencode/) |

Multiple platforms run simultaneously against the configured agent
(`AGENT_BRIDGE_AGENT=claude|codex|opencode`).

## Quick start

Full walkthrough: [Getting Started](https://htkuan.github.io/ai-agent-bridge/getting-started/).

### 1. Install

Requires Python 3.12+ and the CLI of your chosen agent (installed and authenticated).

```bash
pip install "ai-agent-bridge[all]"   # or [slack] / [telegram] / [line] / [api]
```

Or from source with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/htkuan/ai-agent-bridge.git
cd ai-agent-bridge
uv sync
```

### 2. Configure — `.env` or YAML

Configuration merges three sources, **env vars > YAML > built-in defaults**, so
either mode works alone and they mix freely.

**Env mode** — copy the template and fill in what you use:

```bash
cp .env.example .env
```

```bash
# Required for Slack
AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-your-bot-token
AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-your-app-level-token

# Claude Code working directory
AGENT_BRIDGE_CLAUDE_WORK_DIR=/path/to/your/project
```

**YAML mode** — one nested file for all components, secrets pulled from the
environment via `$(VAR)` (safe to commit):

```bash
cp agent-bridge.example.yaml agent-bridge.yaml   # auto-discovered in cwd
```

```yaml
agent: claude

platforms:
  slack:
    bot_token: $(SLACK_BOT_TOKEN)
    app_token: $(SLACK_APP_TOKEN)

agents:
  claude:
    work_dir: /path/to/your/project
```

Use `agent-bridge -c path/to/file.yaml` or `AGENT_BRIDGE_CONFIG=...` to point at
a different file. Full key ⇔ env var mapping:
[Configuration](https://htkuan.github.io/ai-agent-bridge/configuration/).

Platform credentials (creating the Slack app, the Telegram bot, the LINE
channel, ...) are covered step by step in each platform's docs page — e.g.
[Slack setup](https://htkuan.github.io/ai-agent-bridge/platforms/slack/#setup).

### 3. Run

```bash
agent-bridge          # or from source: uv run agent-bridge
```

Then talk to it — for example on Slack:

| Action | How |
|--------|-----|
| Channel | `@AgentBridge help me refactor this function` |
| DM | Send a direct message to the bot |
| Continue conversation | Reply in the same Slack thread |
| Attach files | Upload files in the message — the agent receives download URLs |

Each Slack thread (or Telegram topic, LINE chat, API `session` id) is one agent
session — the agent remembers context within it.

## Architecture

Three independent layers, connected by narrow protocols:

| Layer | Role |
|-------|------|
| **Platform Adapter** | Owns session semantics, per-session locking, UI rendering |
| **Bridge** | Routes messages, maps session keys → IDs, enforces global concurrency |
| **Agent Controller** | Executes prompts, yields generic events |

All agent output flows through five generic events — the shared language
between agents and platforms:

| Event | Description |
|-------|-------------|
| `Processing` | Slot acquired, agent is starting |
| `TextDelta` | Incremental text from agent |
| `StatusUpdate` | Agent performing an action (tool use, etc.) |
| `UserQuestion` | Agent asking user for input |
| `Completion` | Agent finished (with cost, duration, error status) |

Session lifecycle: the platform builds a session key (e.g.
`slack:{channel}:{thread_ts}`), the bridge resolves it to a UUID session id
(persisted, TTL-expired — default 72 h), and the agent runs with it (new or
resumed). The full contract — event semantics, `handle_message` parameters,
usage reporting: [Architecture](https://htkuan.github.io/ai-agent-bridge/architecture/)
and [Bridge Core](https://htkuan.github.io/ai-agent-bridge/bridge/).

## Environment variables

Every variable also has a YAML config key — see
[Configuration](https://htkuan.github.io/ai-agent-bridge/configuration/).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | — | API key for the Claude Code CLI (skip if already authenticated via `claude login`) |
| `AGENT_BRIDGE_CONFIG` | No | — | Path to a YAML config file (else `./agent-bridge.yaml` if present, else pure env mode) |
| `AGENT_BRIDGE_AGENT` | No | `claude` | Which registered agent handles messages |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack Bot User OAuth Token (`xoxb-...`) |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack App-Level Token for Socket Mode (`xapp-...`) |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL` | No | — | Channel to notify after Socket Mode connects |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE` | No | — | Message sent to the startup-notify channel |
| `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` | No | — (allow all) | Comma-separated channel-name allow-list; non-empty also blocks DMs |
| `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` | No | built-in English notice | Reply sent to messages from non-allowed channels |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED` | No | `false` | Append a usage/cost footer to the final reply |
| `AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE` | No | built-in layout | `{placeholder}` template for the usage footer |
| `AGENT_BRIDGE_TELEGRAM_BOT_TOKEN` | Yes (if using Telegram) | — | Telegram bot token from @BotFather |
| `AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS` | No | — (allow all) | Comma-separated chat-id allow-list; other chats silently ignored |
| `AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS` | No | `30` | `getUpdates` long-poll wait (seconds) |
| `AGENT_BRIDGE_TELEGRAM_STATE_PATH` | No | `./telegram.json` | Persists the last processed update id across restarts |
| `AGENT_BRIDGE_TELEGRAM_API_BASE_URL` | No | `https://api.telegram.org` | Bot API base URL (tests point this at a fake server) |
| `AGENT_BRIDGE_LINE_CHANNEL_SECRET` | Yes (if using LINE) | — | LINE channel secret (webhook signature verification) |
| `AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN` | Yes (if using LINE) | — | LINE channel access token (Messaging API) |
| `AGENT_BRIDGE_LINE_WEBHOOK_HOST` | No | `0.0.0.0` | Webhook server bind address |
| `AGENT_BRIDGE_LINE_WEBHOOK_PORT` | No | `8080` | Webhook server port (`0` = ephemeral, for tests) |
| `AGENT_BRIDGE_LINE_WEBHOOK_PATH` | No | `/line/webhook` | Webhook endpoint path |
| `AGENT_BRIDGE_LINE_API_BASE_URL` | No | `https://api.line.me` | Messaging API base URL (tests point this at a fake server) |
| `AGENT_BRIDGE_API_ENABLED` | No | `false` | Enable the generic HTTP POST API (buffered JSON + SSE streaming) |
| `AGENT_BRIDGE_API_HOST` | No | `127.0.0.1` | API server bind address (loopback by default) |
| `AGENT_BRIDGE_API_PORT` | No | `8081` | API server port (`0` = ephemeral, for tests) |
| `AGENT_BRIDGE_API_AUTH_TOKEN` | No | — (no auth) | Bearer token required on every `/v1` request when set |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Working directory for Claude Code |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude permission mode |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Run each session in an isolated git worktree (requires `origin/HEAD`) |
| `AGENT_BRIDGE_CLAUDE_EFFORT` | No | `xhigh` | Claude effort level: `low` / `medium` / `high` / `xhigh` / `max` |
| `AGENT_BRIDGE_CODEX_WORK_DIR` | No | `.` | Working directory for Codex |
| `AGENT_BRIDGE_CODEX_MODEL` | No | — (CLI default) | Optional Codex model override (`-m`) |
| `AGENT_BRIDGE_CODEX_SANDBOX` | No | `workspace-write` | Codex sandbox: `read-only` / `workspace-write` / `danger-full-access` |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | No | `./codex-sessions.json` | Bridge-session → codex-thread mapping file |
| `AGENT_BRIDGE_OPENCODE_WORK_DIR` | No | `.` | Working directory for OpenCode |
| `AGENT_BRIDGE_OPENCODE_MODEL` | No | — (CLI default) | Optional OpenCode model override (`--model`, `provider/model` form) |
| `AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH` | No | `./opencode-sessions.json` | Bridge-session → opencode-session mapping file |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Session mapping file path |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Session TTL (hours) |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Max concurrent agent processes |
| `AGENT_BRIDGE_DEDUPE_TTL_SECONDS` | No | `0` | Cross-session prompt dedupe window (seconds); `0` disables |
| `AGENT_BRIDGE_DEDUPE_MAX_ENTRIES` | No | `512` | Dedupe cache LRU cap |
| `AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD` | No | `0` | SimHash Hamming threshold for fuzzy dedupe; `0` = exact canonical only |
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Enable the heartbeat platform — fires a fixed prompt on a fixed interval |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | Yes (if heartbeat enabled) | — | Interval between heartbeat ticks (minutes) |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | Yes (if heartbeat enabled) | — | Prompt sent on every heartbeat tick |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | Last-run timestamp path (used for restart catch-up) |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Extending

Adding a component never requires touching the bridge core, the other agents,
or the other platforms:

- **New platform** — `platforms/{name}/` with `config.py` + `adapter.py`,
  implementing the `PlatformAdapter` protocol, plus a registry entry.
- **New agent** — `agents/{name}/` with `config.py`, `controller.py`,
  `events.py`, implementing the `AgentController` protocol (yield generic
  `BridgeEvent`s only), plus a registry entry.

The exact contract and step-by-step checklists:
[Architecture](https://htkuan.github.io/ai-agent-bridge/architecture/) ·
[Contributing](https://github.com/htkuan/ai-agent-bridge/blob/main/CONTRIBUTING.md).

## Development

```bash
uv sync                             # deps incl. dev tools (pytest, ruff, pre-commit)
uv run pre-commit install           # git hooks: ruff + commitlint

uv run pytest                       # all tests (unit + integration; fully offline)
uv run pytest -m "not integration"  # fast path: unit tests only

uv run ruff check .                 # lint
uv run ruff format --check .        # format check

AGENT_BRIDGE_LOG_LEVEL=DEBUG uv run agent-bridge   # run with debug logging
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(lowercase types: `feat:`, `fix:`, ...) and are enforced on PRs. Merging to
`main` cuts a release automatically — see
[Releasing](https://htkuan.github.io/ai-agent-bridge/releasing/). Contributions
welcome: [CONTRIBUTING.md](https://github.com/htkuan/ai-agent-bridge/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/htkuan/ai-agent-bridge/blob/main/LICENSE)

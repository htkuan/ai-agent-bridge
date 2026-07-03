# Agent Bridge

Modular bridge that connects **chat platforms** to **AI agents**. Each layer is independent — swap platforms or agents without touching the others.

Currently supports: **Slack**, **Telegram**, **LINE**, **POST API** (generic HTTP entry point), **Heartbeat** (scheduled prompts) + **Claude Code**, **Codex**

```
┌──────────────┐     ┌──────────┐     ┌──────────────┐
│   Platform   │────▶│  Bridge  │────▶│    Agent     │
│   (Slack)    │◀────│ (Router) │◀────│ (Claude Code)│
└──────────────┘     └──────────┘     └──────────────┘
  Session owner       Pure routing      Purely invoked
  Locking & render    Key → ID map      Yields events
  UI logic            Concurrency       No UI knowledge
```

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (or the [OpenAI Codex CLI](https://developers.openai.com/codex) when running with `AGENT_BRIDGE_AGENT=codex`)

### Install

```bash
git clone https://github.com/htkuan/ai-agent-bridge.git
cd agent-bridge
uv sync
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` with your tokens:

```bash
# Required for Slack
AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-your-bot-token
AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-your-app-level-token

# Claude Code working directory
AGENT_BRIDGE_CLAUDE_WORK_DIR=/path/to/your/project
```

See [Environment Variables](#environment-variables) for the full list.

Alternatively, use a **YAML config file** for nested, per-component settings — with
secrets referenced as `$(VAR)` from the environment and any key still overridable
by its env var:

```bash
cp agent-bridge.example.yaml agent-bridge.yaml   # auto-discovered in cwd
uv run agent-bridge                              # or: agent-bridge -c path/to/file.yaml
```

Precedence: env vars > YAML > built-in defaults. Full key ⇔ env var mapping:
[docs/configuration.md](docs/configuration.md).

### Slack App Setup

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** → generate an App-Level Token (`xapp-...`)
3. Add **Bot Token Scopes** (OAuth & Permissions):
   - `app_mentions:read`, `chat:write`, `files:write`, `im:history`, `im:read`
4. Subscribe to **Events**:
   - `app_mention`, `message.im`
5. Install to workspace → copy Bot User OAuth Token (`xoxb-...`)

### Run

```bash
uv run agent-bridge
```

## Usage

| Action | How |
|--------|-----|
| Channel | `@AgentBridge help me refactor this function` |
| DM | Send a direct message to the bot |
| Continue conversation | Reply in the same Slack thread |
| Attach files | Upload files in the message — the agent receives download URLs |

Each Slack thread is one agent session. The agent remembers context within a thread.

## Architecture

The system has three independent layers:

| Layer | Role | Docs |
|-------|------|------|
| **Platform Adapter** | Owns session semantics, per-session locking, UI rendering | [Slack](docs/platforms/slack.md) · [Telegram](docs/platforms/telegram.md) · [LINE](docs/platforms/line.md) · [POST API](docs/platforms/api.md) · [Heartbeat](docs/platforms/heartbeat.md) |
| **Bridge** | Routes messages, maps session keys → IDs, enforces concurrency | Core — see below |
| **Agent Controller** | Executes prompts, yields generic events | [Claude Agent](docs/agents/claude.md) · [Codex Agent](docs/agents/codex.md) |

### Event Model

All agent output flows through generic events — the shared language between agents and platforms:

| Event | Description |
|-------|-------------|
| `Processing` | Slot acquired, agent is starting |
| `TextDelta` | Incremental text from agent |
| `StatusUpdate` | Agent performing an action (tool use, etc.) |
| `UserQuestion` | Agent asking user for input |
| `Completion` | Agent finished (with cost, duration, error status) |

### Session Lifecycle

1. User sends message → Platform constructs session key (e.g. `slack:{channel}:{thread_ts}`)
2. Bridge resolves key → UUID session ID (creates new if first message)
3. Agent runs with session ID (new session or resume existing)
4. Sessions expire after configurable TTL (default 72h)

## Environment Variables

Every variable also has a YAML config key — see [docs/configuration.md](docs/configuration.md).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | — | API key for the Claude Code CLI (skip if already authenticated via `claude login`) |
| `AGENT_BRIDGE_CONFIG` | No | — | Path to a YAML config file (else `./agent-bridge.yaml` if present, else pure env mode) |
| `AGENT_BRIDGE_AGENT` | No | `claude` | Which registered agent handles messages |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack Bot User OAuth Token (`xoxb-...`) |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack App-Level Token for Socket Mode (`xapp-...`) |
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
| `AGENT_BRIDGE_CODEX_WORK_DIR` | No | `.` | Working directory for Codex |
| `AGENT_BRIDGE_CODEX_MODEL` | No | — (CLI default) | Optional Codex model override (`-m`) |
| `AGENT_BRIDGE_CODEX_SANDBOX` | No | `workspace-write` | Codex sandbox: `read-only` / `workspace-write` / `danger-full-access` |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | No | `./codex-sessions.json` | Bridge-session → codex-thread mapping file |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Session mapping file path |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Session TTL (hours) |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Max concurrent agent processes |
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Enable the heartbeat platform — fires a fixed prompt on a fixed interval |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | Yes (if heartbeat enabled) | — | Interval between heartbeat ticks (minutes) |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | Yes (if heartbeat enabled) | — | Prompt sent on every heartbeat tick |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | Last-run timestamp path (used for restart catch-up) |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Extending

### Add a new platform

Create `platforms/{name}/` with `config.py` and `adapter.py`. Implement the `PlatformAdapter` protocol. Define your session key format. See [Slack Adapter docs](docs/platforms/slack.md) for reference.

### Add a new agent

Create `agents/{name}/` with `config.py`, `controller.py`, and `events.py`. Implement the `AgentController` protocol — your `run()` yields `BridgeEvent`s. See [Claude Agent docs](docs/agents/claude.md) for reference.

Neither change requires modifying the bridge, the other agent, or the other platform.

## Development

```bash
# Run tests (unit + integration; all offline)
uv run pytest

# Fast path: unit tests only
uv run pytest -m "not integration"

# Run with debug logging
AGENT_BRIDGE_LOG_LEVEL=DEBUG uv run agent-bridge
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (lowercase
types: `feat:`, `fix:`, ...) and are enforced on PRs. Merging to `main` cuts a release
automatically — see [docs/releasing.md](docs/releasing.md).

## License

MIT

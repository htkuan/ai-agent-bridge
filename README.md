# Agent Bridge

Modular bridge that connects **chat platforms** to **AI agents**. Each layer is independent — swap platforms or agents without touching the others.

Currently supports: **Slack**, **Heartbeat**, **HTTP Webhook** + **Claude Code**, **Pi**

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
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

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

See [Environment Variables](#environment-variables) for the full list. Every variable is
read in one place — `AppConfig.from_env()` walks each layer's `{Component}Config.from_env()`,
and the whole app is then built from that single `AppConfig`.

### Slack App Setup

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** → generate an App-Level Token (`xapp-...`)
3. Add **Bot Token Scopes** (OAuth & Permissions):
   - `app_mentions:read`, `chat:write`, `files:write`, `im:history`, `im:read`
   - `channels:read`, `users:read`, `team:read` — name lookups for the agent's
     context and the channel allow-list (add `groups:read` / `mpim:read` for
     private channels / group DMs). Missing these logs `missing_scope` warnings;
     see [docs/platforms/slack.md](docs/platforms/slack.md#troubleshooting)
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
| **Platform Adapter** | Owns session semantics, per-session locking, UI rendering | [Slack](docs/platforms/slack.md) · [Heartbeat](docs/platforms/heartbeat.md) · [Webhook](docs/platforms/webhook.md) |
| **Bridge** | Routes messages, maps session keys → IDs, enforces concurrency | Core — see below |
| **Agent Controller** | Executes prompts, yields generic events | [Claude Agent](docs/agents/claude.md) · [Pi Agent](docs/agents/pi.md) |

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

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | — | API key for the Claude Code CLI (skip if already authenticated via `claude login`) |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes (if using Slack) | — | Slack Bot User OAuth Token (`xoxb-...`) |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes (if using Slack) | — | Slack App-Level Token for Socket Mode (`xapp-...`) |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Working directory for Claude Code |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude permission mode |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Run each session in an isolated git worktree (requires `origin/HEAD`) |
| `AGENT_BRIDGE_CLAUDE_MODEL` | No | — (CLI default) | Model passed to `claude --model` |
| `AGENT_BRIDGE_PROFILES_PATH` | No | — (disabled) | TOML file with named agent profiles (`[claude.profiles.*]`, `[pi.profiles.*]`) + Slack channel→profile routing (see `profiles.example.toml`) |
| `AGENT_BRIDGE_PI_*` | No | — | Base config for named [Pi agent](docs/agents/pi.md) profiles (work dir, provider, model, thinking, tool allowlist) |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Session mapping file path |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Session TTL (hours) |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Max concurrent agent processes |
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Enable the heartbeat platform — fires a fixed prompt on a fixed interval |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | Yes (if heartbeat enabled) | — | Interval between heartbeat ticks (minutes) |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | Yes (if heartbeat enabled) | — | Prompt sent on every heartbeat tick |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | Last-run timestamp path (used for restart catch-up) |
| `AGENT_BRIDGE_HTTP_ENABLED` | No | `false` | Enable the shared HTTP server (console page + HTTP platforms) |
| `AGENT_BRIDGE_HTTP_HOST` | No | `127.0.0.1` | HTTP server bind address (loopback by default) |
| `AGENT_BRIDGE_HTTP_PORT` | No | `8080` | HTTP server port |
| `AGENT_BRIDGE_WEBHOOK_ENABLED` | No | `false` | Enable the webhook platform — `POST /platforms/webhook/v1/messages`, result delivered via callback |
| `AGENT_BRIDGE_WEBHOOK_TOKEN` | Yes (if webhook enabled) | — | Bearer token guarding the webhook endpoint |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Extending

### Add a new platform

Create `platforms/{name}/` with `config.py` and `adapter.py`. Subclass `BasePlatformAdapter` (`platforms/base.py`): pre-process your platform's native event into a `BridgeRequest`, call `process()`, and override the `on_*` hooks to render the streamed events. Define your session key format with `make_session_key`. HTTP-based platforms expose an `APIRouter` that gets mounted on the [shared HTTP server](docs/server.md). See the [Slack](docs/platforms/slack.md) and [Webhook](docs/platforms/webhook.md) docs for reference.

### Add a new agent

Create `agents/{name}/` with `config.py`, `controller.py`, and `events.py`. Subclass `CliAgentController` (`agents/base.py`): describe your CLI with `build_command()` and `parse_line()`, and the base engine handles the subprocess lifecycle — the `AgentController` protocol stays the contract, and `run()` yields `BridgeEvent`s. See [Claude Agent docs](docs/agents/claude.md) for reference.

Neither change requires modifying the bridge, the other agent, or the other platform.

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Run the live e2e against the real claude CLI (opt-in, spends tokens)
uv run pytest -m live --live --no-cov -v

# Run with debug logging
AGENT_BRIDGE_LOG_LEVEL=DEBUG uv run agent-bridge
```

Test layout, fakes and markers: [docs/testing.md](docs/testing.md).

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (lowercase
types: `feat:`, `fix:`, ...) and are enforced on PRs. Merging to `main` cuts a release
automatically — see [docs/releasing.md](docs/releasing.md).

## License

MIT

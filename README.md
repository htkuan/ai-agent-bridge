# Agent Bridge

Modular bridge that connects **chat platforms** to **AI agents**. Each layer is independent — swap platforms or agents without touching the others.

Currently supports: **Slack** + **Claude Code**

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

See [Environment Variables](#environment-variables) for the full list.

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
| **Platform Adapter** | Owns session semantics, per-session locking, UI rendering | [Slack Adapter](docs/platforms/slack.md) |
| **Bridge** | Routes messages, maps session keys → IDs, enforces concurrency | Core — see below |
| **Agent Controller** | Executes prompts, yields generic events | [Claude Agent](docs/agents/claude.md) |

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
| `ANTHROPIC_API_KEY` | Yes | — | API key consumed by the Claude Code CLI |
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes | — | Slack Bot User OAuth Token (`xoxb-...`) |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes | — | Slack App-Level Token for Socket Mode (`xapp-...`) |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Working directory for Claude Code |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude permission mode |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Per-invocation timeout (seconds) |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Run each session in an isolated git worktree (requires `origin/HEAD`) |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Session mapping file path |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Session TTL (hours) |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Max concurrent agent processes |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Extending

### Add a new platform

Create `platforms/{name}/` with `config.py` and `adapter.py`. Implement the `PlatformAdapter` protocol. Define your session key format. See [Slack Adapter docs](docs/platforms/slack.md) for reference.

### Add a new agent

Create `agents/{name}/` with `config.py`, `controller.py`, and `events.py`. Implement the `AgentController` protocol — your `run()` yields `BridgeEvent`s. See [Claude Agent docs](docs/agents/claude.md) for reference.

Neither change requires modifying the bridge, the other agent, or the other platform.

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Run with debug logging
AGENT_BRIDGE_LOG_LEVEL=DEBUG uv run agent-bridge
```

## License

MIT

# Agent Bridge

Modular bridge service that connects **chat platforms** to **AI agents**. The architecture cleanly separates platform concerns from agent concerns, making it easy to add new platforms or agents.

Currently supports **Slack** as the chat platform and **Claude Code** as the AI agent backend.

## Architecture

```
┌─────────────────────────┐
│  Platform (Slack)       │  Defines session semantics (thread = session)
│  platforms/slack/       │  Manages per-session locking & flow control
│  - adapter.py           │  Renders agent events (stream updates, final reply)
│  - config.py            │
└──────────┬──────────────┘
           │ session_key, text, context
           ▼
┌─────────────────────────┐
│  Bridge                 │  Pure routing — no platform or agent knowledge
│  bridge.py              │  session_key → session_id (via SessionManager)
│  session.py             │  Forwards to agent, yields BridgeEvents back
│  events.py              │  TextDelta | StatusUpdate | Completion
│  protocols.py           │  AgentController + PlatformAdapter protocols
└──────────┬──────────────┘
           │ session_id, prompt, is_new, context
           ▼
┌─────────────────────────┐
│  Agent (Claude Code)    │  Purely invoked: load session + input → output
│  agents/claude/         │  Translates Claude stream-json → BridgeEvents
│  - controller.py        │  Does not define sessions or care about rendering
│  - events.py            │
│  - config.py            │
└─────────────────────────┘
```

### Design Principles

**Platform defines session semantics.** A Slack thread is a session. A Discord channel might be a session. This is platform knowledge — the bridge and agent don't care how sessions are defined.

**Agent is purely invoked.** It receives `(session_id, prompt, is_new, context)`, loads the session, executes, and yields events. It doesn't know where the session came from or how results will be rendered.

**Bridge is pure routing.** It resolves session keys to session IDs and forwards requests/events. No platform-specific or agent-specific logic.

### Generic Event Model

Platforms consume three event types — the common language between any agent and any platform:

| Event | Description |
|-------|-------------|
| `TextDelta` | Incremental text from the agent |
| `StatusUpdate` | Agent is performing an action (tool use, thinking, etc.) |
| `Completion` | Agent finished responding (with cost, duration, error status) |

Agent-internal events (init, thinking, tool results) are translated to these generic types within each agent module.

### Data Flow

1. User sends a message in Slack (via `@mention` in channel or direct message)
2. **Slack Adapter** receives the event, constructs a session key (`slack:{channel}:{thread_ts}`)
3. **Slack Adapter** acquires per-session lock (prevents concurrent processing)
4. **Bridge** resolves session key → session ID via **SessionManager**
5. **Agent (Claude Controller)** spawns `claude -p` with the session, yields `BridgeEvent`s
6. **Slack Adapter** renders events as real-time message updates (throttled to avoid rate limits)

### Session Management

- Each Slack thread maps to one agent session (defined by the platform)
- The bridge stores the mapping: `session_key → {session_id, created_at, last_used}`
- Mappings are persisted in a JSON file
- Sessions have a configurable TTL (default 72 hours) — expired sessions are automatically purged

## Tech Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Language | Python 3.12+ | Type union syntax (`X \| Y`), `match` statements, modern asyncio |
| Package manager | uv | Fast, supports pyproject.toml natively |
| Slack SDK | [slack-bolt](https://github.com/slackapi/bolt-python) | Official Slack SDK, async support, Socket Mode |
| Async HTTP | aiohttp | Required by slack-bolt for async Socket Mode |
| Env config | python-dotenv | Load `.env` files |
| Testing | pytest + pytest-asyncio | Standard Python testing |
| Claude CLI | `claude -p` with `--output-format stream-json` | Non-interactive mode with real-time streaming |

## Project Structure

```
agent-bridge/
├── pyproject.toml
├── .env.example
├── src/
│   └── agent_bridge/
│       ├── __init__.py             # Entry point: wires platform + bridge + agent
│       ├── config.py               # BridgeConfig (session store, TTL)
│       ├── bridge.py               # Pure routing: session resolve → agent call
│       ├── events.py               # TextDelta, StatusUpdate, Completion
│       ├── session.py              # SessionManager (key → session_id mapping)
│       ├── protocols.py            # AgentController + PlatformAdapter protocols
│       ├── agents/
│       │   └── claude/
│       │       ├── config.py       # ClaudeConfig (work_dir, permissions, timeout)
│       │       ├── controller.py   # Claude Code subprocess controller
│       │       └── events.py       # Claude stream-json parser + BridgeEvent converter
│       └── platforms/
│           └── slack/
│               ├── config.py       # SlackConfig (bot_token, app_token)
│               └── adapter.py      # Slack adapter (session def, locking, rendering)
└── tests/
    ├── test_events.py              # Claude event parsing + BridgeEvent conversion
    └── test_session.py             # Session manager tests
```

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

### Install

```bash
git clone <repo-url>
cd agent-bridge
uv sync
```

### Slack App Configuration

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** and generate an App-Level Token (`xapp-...`)
3. Add the following **Bot Token Scopes** under OAuth & Permissions:
   - `app_mentions:read` — receive @mention events
   - `chat:write` — send and update messages
   - `im:history` — read DM messages
   - `im:read` — access DM channels
4. Subscribe to these **Events** under Event Subscriptions:
   - `app_mention`
   - `message.im`
5. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)

### Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

```bash
AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-your-bot-token
AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-your-app-level-token
AGENT_BRIDGE_CLAUDE_WORK_DIR=/path/to/your/project
AGENT_BRIDGE_CLAUDE_PERMISSION_MODE=acceptEdits
AGENT_BRIDGE_SESSION_STORE_PATH=./sessions.json
AGENT_BRIDGE_SESSION_TTL_HOURS=72
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes | — | Slack Bot User OAuth Token |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes | — | Slack App-Level Token (Socket Mode) |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Working directory for Claude Code |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude permission mode |
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Path to session mapping file |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Session TTL in hours |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Claude agent timeout in seconds |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `10` | Max concurrent agent sessions |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Run (Local)

```bash
uv run agent-bridge
```

### Test

```bash
uv run pytest tests/ -v
```

## Usage

- **Channel**: Mention the bot — `@AgentBridge help me refactor this function`
- **DM**: Send a direct message — the bot responds in the same conversation
- **Thread continuity**: Reply in the same Slack thread to continue the agent session

## Extending

### Adding a new agent

Create `agents/<name>/` with `config.py`, `controller.py`, `events.py`. Implement the `AgentController` protocol — your `run()` method yields `BridgeEvent`s. Wire it up in `__init__.py`.

### Adding a new platform

Create `platforms/<name>/` with `config.py`, `adapter.py`. Define your own session key logic (e.g., `discord:{guild}:{channel}`), manage per-session locking, consume `BridgeEvent`s from `bridge.handle_message()`. Wire it up in `__init__.py`.

Neither change requires modifying the bridge, the other agent, or the other platform.

## Design Decisions

### One-shot per message (vs. long-running process)

Each user message spawns a new `claude -p` process that exits after completion. Session continuity is handled by Claude Code's built-in `--resume` flag.

**Why**: Simpler process lifecycle, no idle resource consumption, graceful handling of crashes.

### Per-session locking (platform-owned)

An `asyncio.Lock` per session key prevents concurrent agent processes for the same session. This is managed by the platform adapter, not the bridge, because locking strategy may vary by platform.

### Throttled Slack updates

Slack message updates are throttled to 1.5-second intervals during streaming.

**Why**: Slack's API rate limits are ~1 request/second per method.

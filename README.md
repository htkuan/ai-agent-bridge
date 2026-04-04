# Agent Bridge

Bridge service that connects chat platforms to CLI AI agent tools. Currently supports **Slack** as the chat platform and **Claude Code** as the AI agent backend.

Users interact with Claude Code through Slack messages — the bridge handles session management, process control, and real-time streaming of responses.

## Architecture

```
┌─────────────┐
│  Slack User  │
│  @mention/DM │
└──────┬───────┘
       │ Slack Events API (Socket Mode)
       ▼
┌──────────────────┐
│  Slack Adapter    │  slack-bolt async + Socket Mode
│  adapters/slack   │  throttled message updates (1.5s)
└──────┬───────────┘
       │ (platform, channel, thread, user, text)
       ▼
┌──────────────────┐
│  Bridge           │  per-session asyncio.Lock
│  bridge.py        │  session key → session_id mapping
└──────┬───────────┘
       │
  ┌────┴─────┐
  ▼          ▼
┌────────┐ ┌─────────────────┐
│Session │ │Claude Controller │  asyncio subprocess
│Manager │ │claude/controller │  claude -p --output-format stream-json
│JSON    │ │                  │  --session-id / --resume
└────────┘ └────────┬────────┘
                    │ stdout (NDJSON stream)
                    ▼
           ┌────────────────┐
           │ Event Parser    │  parse_stream_line()
           │ claude/events   │  → typed dataclass events
           └────────────────┘
```

### Data Flow

1. User sends a message in Slack (via `@mention` in channel or direct message)
2. **Slack Adapter** receives the event, extracts channel/thread/user/text
3. **Bridge** constructs a session key (`slack:{channel}:{thread_ts}`), acquires a per-session lock
4. **Session Manager** looks up or creates a Claude Code session ID (UUID) for that key
5. **Claude Controller** spawns `claude -p "prompt" --resume <session-id> --output-format stream-json`
6. stdout is read line-by-line, each JSON line is parsed into a typed **Event**
7. **Slack Adapter** updates the Slack message in real-time as events arrive (throttled to avoid rate limits)

### Session Management

- Each Slack thread maps to one Claude Code session
- Session context (conversation history, file state) is managed internally by Claude Code
- The bridge only stores the mapping: `session_key → {session_id, created_at, last_used}`
- Mappings are persisted in a JSON file
- First message in a thread → `claude -p --session-id <new-uuid>` (creates session)
- Subsequent messages → `claude -p --resume <existing-uuid>` (continues session)
- Sessions have a configurable TTL (default 72 hours since last use) — expired sessions are automatically purged on startup and treated as new on next access

### Stream Event Types

The Claude CLI outputs newline-delimited JSON (NDJSON). The bridge parses these into typed events:

| Event | Description |
|-------|-------------|
| `InitEvent` | Session initialized, includes model and available tools |
| `AssistantTextEvent` | Text fragment from Claude's response |
| `ThinkingEvent` | Claude's internal reasoning (extended thinking) |
| `ToolUseEvent` | Claude is invoking a tool (Bash, Edit, Read, etc.) |
| `ToolResultEvent` | Result returned from a tool execution |
| `ResultEvent` | Final result with total cost, duration, error status |

## Tech Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Language | Python 3.12+ | Type union syntax (`X \| Y`), modern asyncio |
| Package manager | uv | Fast, supports pyproject.toml natively |
| Slack SDK | [slack-bolt](https://github.com/slackapi/bolt-python) | Official Slack SDK, async support, Socket Mode |
| Async HTTP | aiohttp | Required by slack-bolt for async Socket Mode |
| Async file I/O | aiofiles | Non-blocking file operations |
| Env config | python-dotenv | Load `.env` files |
| Testing | pytest + pytest-asyncio | Standard Python testing |
| JSON parsing | Built-in `json` module | Sufficient for NDJSON line parsing |
| Claude CLI | `claude -p` with `--output-format stream-json` | Non-interactive mode with real-time streaming |

## Project Structure

```
agent-bridge/
├── pyproject.toml                  # Project config & dependencies
├── .env.example                    # Environment variables template
├── src/
│   └── agent_bridge/
│       ├── __init__.py             # Entry point: main() / main_sync()
│       ├── config.py               # Config dataclass from env vars
│       ├── bridge.py               # Orchestrator: session + controller + locking
│       ├── claude/
│       │   ├── events.py           # Stream JSON event parser & dataclasses
│       │   ├── session.py          # Session key ↔ session_id mapping (JSON file)
│       │   └── controller.py       # Claude Code subprocess controller
│       └── adapters/
│           ├── base.py             # PlatformAdapter protocol
│           └── slack.py            # Slack adapter (bolt async + Socket Mode)
└── tests/
    ├── test_events.py              # Stream event parsing tests
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
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-level-token
CLAUDE_WORK_DIR=/path/to/your/project
CLAUDE_PERMISSION_MODE=acceptEdits
SESSION_STORE_PATH=./sessions.json
SESSION_TTL_HOURS=72
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | Yes | — | Slack Bot User OAuth Token |
| `SLACK_APP_TOKEN` | Yes | — | Slack App-Level Token (Socket Mode) |
| `CLAUDE_WORK_DIR` | No | `.` | Working directory for Claude Code |
| `CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | Claude permission mode (`acceptEdits`, `dangerously-skip-permissions`, `default`, `plan`) |
| `SESSION_STORE_PATH` | No | `./sessions.json` | Path to session mapping file |
| `SESSION_TTL_HOURS` | No | `72` | Session TTL in hours; inactive sessions beyond this are expired and recreated |

### Run (Local)

```bash
uv run agent-bridge
```

### Run (Docker)

```bash
cp .env.example .env
# Edit .env with your tokens (including ANTHROPIC_API_KEY for Docker)

# Build and run
docker compose up --build

# Or run in background
docker compose up -d --build
```

The `CLAUDE_WORK_DIR` in `.env` determines which local directory is mounted into the container at `/workspace` for Claude Code to operate on. Default is the current directory.

### Test

```bash
uv run pytest tests/ -v
```

## Usage

- **Channel**: Mention the bot — `@AgentBridge help me refactor this function`
- **DM**: Send a direct message — the bot responds in the same conversation
- **Thread continuity**: Reply in the same Slack thread to continue the Claude Code session (context is preserved)

## Design Decisions

### One-shot per message (vs. long-running process)

Each user message spawns a new `claude -p` process that exits after completion, rather than maintaining a long-running process per session. Session continuity is handled by Claude Code's built-in `--resume` flag.

**Why**: Simpler process lifecycle, no idle resource consumption, graceful handling of crashes. The trade-off is per-message startup cost, which is acceptable for v1.

### Per-session locking

An `asyncio.Lock` per session key prevents concurrent `claude` processes for the same Slack thread. If two messages arrive quickly in the same thread, the second waits for the first to complete.

**Why**: Claude Code sessions don't support concurrent access. Without locking, messages could interleave and corrupt session state.

### Throttled Slack updates

Slack message updates are throttled to 1.5-second intervals during streaming.

**Why**: Slack's API rate limits are ~1 request/second per method. Updating on every text fragment would quickly hit rate limits.

### Adapter pattern

The `PlatformAdapter` protocol allows adding new chat platforms (Discord, Teams, etc.) without changing the bridge or controller logic.

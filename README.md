# Agent Bridge

A modular bridge that connects **chat platforms** (Slack, …) to **AI agents** (Claude Code, …) through a single, generic event protocol.

> Each layer is independent. You can swap the platform or the agent without touching the other side, the bridge core, or the session store.

```
┌──────────────────────┐        ┌──────────────────┐        ┌──────────────────────┐
│  Platform Adapter    │  ───▶  │     Bridge       │  ───▶  │   Agent Controller   │
│  (Slack / Heartbeat) │  ◀───  │     (Router)     │  ◀───  │     (Claude Code)    │
└──────────────────────┘        └──────────────────┘        └──────────────────────┘
  Owns session keys,              Pure routing.               Receives session_id +
  per-session locking,            Resolves key → UUID.        prompt, yields generic
  rendering UI.                   Global concurrency gate.    BridgeEvents.
       │                                                              │
       └────────────────────── BridgeEvent stream ────────────────────┘
              (Processing · TextDelta · StatusUpdate · UserQuestion · Completion)
```

Currently shipped: **Slack** + **Heartbeat** platforms · **Claude Code** agent.

---

## Table of contents

- [Quick start](#quick-start)
- [How to use it](#how-to-use-it)
- [Architecture](#architecture)
  - [The three layers](#the-three-layers)
  - [The two interfaces](#the-two-interfaces)
  - [The event protocol](#the-event-protocol)
  - [Session lifecycle](#session-lifecycle)
- [Documentation map](#documentation-map)
- [Extending](#extending)
- [Configuration reference](#configuration-reference)
- [Development](#development)

---

## Quick start

### Prerequisites

- Python **3.12+**
- [`uv`](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude login` or `ANTHROPIC_API_KEY`)
- A Slack workspace where you can install a bot (skip if you only want the heartbeat platform)

### 1. Install

```bash
git clone https://github.com/htkuan/ai-agent-bridge.git
cd agent-bridge
uv sync
```

### 2. Configure

```bash
cp .env.example .env
```

Minimum config to talk to the bot from Slack:

```bash
AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-...        # Slack Bot User OAuth Token
AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-...        # Slack App-Level Token (Socket Mode)
AGENT_BRIDGE_CLAUDE_WORK_DIR=/abs/path/to/codebase   # what Claude Code can see
```

Full list lives in [`.env.example`](.env.example) and the [Configuration reference](#configuration-reference) below.

### 3. Set up the Slack app (one-time)

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Enable **Socket Mode** → generate an App-Level Token (`xapp-...`).
3. **OAuth & Permissions** → add bot scopes: `app_mentions:read`, `chat:write`, `files:write`, `im:history`, `im:read`.
4. **Event Subscriptions** → subscribe to bot events: `app_mention`, `message.im`.
5. Install to workspace, copy the Bot User OAuth Token (`xoxb-...`).

Detailed walkthrough → [`docs/platforms/slack.md`](docs/platforms/slack.md).

### 4. Run

```bash
uv run agent-bridge
```

The bridge starts every enabled platform adapter. You should see a Slack connect log; mention or DM the bot to start a session.

---

## How to use it

### Slack

| Action | How |
|--------|-----|
| Start a session in a channel | `@AgentBridge fix the bug in foo.py` |
| Direct message | Send a DM to the bot |
| Continue the conversation | Reply in the same Slack thread |
| Attach files | Upload them with your message — the agent receives signed download URLs |
| Answer the agent | When the agent posts a `:question:` block, just reply in the thread |

One Slack thread = one agent session. Sessions expire after `AGENT_BRIDGE_SESSION_TTL_HOURS` (default 72h).

Full behavior, state machine, throttling, error rendering → [`docs/platforms/slack.md`](docs/platforms/slack.md).

### Heartbeat (proactive ticks)

A second platform that fires a fixed prompt on a fixed interval — useful for autonomous sweeps, triage, status reports, queue drains.

```bash
AGENT_BRIDGE_HEARTBEAT_ENABLED=true
AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES=60
AGENT_BRIDGE_HEARTBEAT_PROMPT=Read ./TODO.md and work on the next unchecked item. If everything is done, do nothing.
```

There is no human listening: the agent must be self-contained, idempotent, and bounded. Output goes to logs (and to whatever tools the agent calls itself).

Full semantics, state file, restart catch-up, concurrency interactions → [`docs/platforms/heartbeat.md`](docs/platforms/heartbeat.md).

---

## Architecture

### The three layers

| Layer | Owns | Knows nothing about | Source |
|-------|------|---------------------|--------|
| **Platform Adapter** | Session-key format, per-session locking, message rendering, prompt + system_prompt construction | Which agent runs, agent-internal events | `src/agent_bridge/platforms/` |
| **Bridge** | Session-key → UUID resolution, global concurrency gate, event forwarding | Platform-specific framing, agent internals | `src/agent_bridge/bridge.py`, `session.py` |
| **Agent Controller** | Executing prompts, parsing agent output, yielding `BridgeEvent`s | Where the prompt came from, how events are rendered | `src/agent_bridge/agents/` |

This separation is the whole point of the project: changing one layer should never force you to read the other two.

### The two interfaces

Everything between the layers flows through these two contracts (see `src/agent_bridge/protocols.py`).

#### Platform Adapter → Bridge

```python
async for event in bridge.handle_message(
    session_key:   str,                       # platform-defined, e.g. "slack:{channel}:{thread_ts}"
    text:          str,                       # the prompt the agent will receive (already pre-tagged with sender if any)
    context:       dict[str, str] | None,     # opaque metadata for audit/logging — agent must NOT parse it
    system_prompt: str | None,                # platform-flavored directives, forwarded as-is to the agent
    resumable:     bool = True,               # True → same key resumes same session; False → fresh ephemeral UUID, no disk trace
):
    ...
```

Field-by-field rules:

| Field | Built by | Forwarded as | Notes |
|-------|----------|--------------|-------|
| `session_key` | Platform | Resolved by `SessionManager` to a UUID `session_id` | Must encode "what the user thinks of as one conversation" |
| `text` | Platform | Passed verbatim as the agent prompt | Pre-tag sender identity here (e.g. `[alice (U123)]: ...`) — the agent stays platform-agnostic |
| `context` | Platform | Opaque pass-through to the agent | For audit/logging only; the agent must not interpret platform keys |
| `system_prompt` | Platform | Passed verbatim as agent system directives | Platform owns the framing (chat vs scheduled trigger vs webhook…) |
| `resumable` | Platform | Controls bridge session-store behavior | `True` for chat threads, `False` for one-shot triggers (heartbeat, webhooks) |

#### Bridge → Agent Controller

```python
class AgentController(Protocol):
    def run(
        self,
        session_id:    str,                      # UUID minted/resolved by the bridge
        prompt:        str,                      # = the platform's `text`, untouched
        is_new:        bool,                     # True on first message in this session
        context:       dict[str, str] | None,    # = the platform's `context`, untouched (opaque)
        system_prompt: str | None,               # = the platform's `system_prompt`, untouched
    ) -> AsyncIterator[BridgeEvent]: ...
```

Rules the agent must respect:

- Treat `prompt` and `system_prompt` as opaque strings. Don't parse them.
- Treat `context` as opaque. Don't read platform-specific keys.
- Yield only `BridgeEvent`s — keep agent-internal events (thinking, raw tool results, init messages) inside this module.
- Yield exactly one `Completion` at the end (success or error).

### The event protocol

The shared vocabulary between agents and platforms — defined in `src/agent_bridge/events.py`.

| Event | Fields | Meaning |
|-------|--------|---------|
| `Processing` | — | Slot acquired, agent is starting |
| `TextDelta` | `text: str` | Incremental text chunk from the agent |
| `StatusUpdate` | `status: str`, `detail: str` | Agent doing something visible (tool use, progress) |
| `UserQuestion` | `questions: list[dict]` | Agent needs user input (rendered by chat platforms; warned-and-ignored by proactive ones) |
| `Completion` | `text`, `is_error`, `cost_usd`, `duration_ms`, `metadata` | Final result. Always exactly one per `run()` |

Anything an agent does internally — extended thinking, tool execution traces, init payloads — never crosses this boundary. The translation layer sits inside each agent module (e.g. `agents/claude/events.py`).

### Session lifecycle

```
1. User triggers something on Platform
2. Adapter builds session_key, acquires per-session lock, builds text + system_prompt
3. bridge.handle_message(session_key, text, context, system_prompt, resumable)
     ├─ resumable=True  → SessionManager.get_or_create(key) → (session_id, is_new), persisted to JSON
     └─ resumable=False → mint fresh UUID, skip SessionManager (no disk trace)
4. Global semaphore check — if full, yield error Completion and return
5. controller.run(session_id, prompt, is_new, context, system_prompt) → BridgeEvent stream
6. Adapter renders each event into platform-native UI
7. Sessions auto-expire after AGENT_BRIDGE_SESSION_TTL_HOURS (resumable ones only)
```

---

## Documentation map

The repo follows the same three-layer split — code under `src/agent_bridge/{platforms,agents}/`, docs under `docs/{platforms,agents}/`. **Every component change should update its corresponding doc.**

| Topic | Doc |
|-------|-----|
| Architecture conventions, code style, dev guide | [`CLAUDE.md`](CLAUDE.md) |
| Slack adapter — setup, session model, state machine, file uploads, error rendering | [`docs/platforms/slack.md`](docs/platforms/slack.md) |
| Heartbeat adapter — proactive ticks, restart catch-up, state file, concurrency | [`docs/platforms/heartbeat.md`](docs/platforms/heartbeat.md) |
| Claude Code agent — subprocess lifecycle, worktree mode, stream-json parsing, timeouts | [`docs/agents/claude.md`](docs/agents/claude.md) |
| All env vars (canonical) | [`.env.example`](.env.example) |

---

## Extending

Both extension paths are designed so the rest of the codebase stays untouched.

### Add a new platform adapter

1. Create `src/agent_bridge/platforms/{name}/config.py` — `{Name}Config` with `from_env()` + `_validate()`.
2. Create `src/agent_bridge/platforms/{name}/adapter.py` — implement the `PlatformAdapter` protocol (`start()`, `stop()`).
3. Define a session-key format (e.g. `discord:{guild}:{channel}`).
4. Own per-session locking.
5. Build `text` (pre-tag sender identity if your platform has one) and `system_prompt` (your invocation framing).
6. Pick `resumable`: `True` for chat-like threads, `False` for one-shot triggers.
7. Consume `BridgeEvent`s from `bridge.handle_message(...)` and render them.
8. Wire into `src/agent_bridge/__init__.py`.
9. Add `docs/platforms/{name}.md`.

Reference implementations: [Slack](docs/platforms/slack.md) (chat) and [Heartbeat](docs/platforms/heartbeat.md) (proactive).

### Add a new agent

1. Create `src/agent_bridge/agents/{name}/config.py` — `{Name}Config` with `from_env()` + `_validate()`.
2. Create `src/agent_bridge/agents/{name}/controller.py` — implement `AgentController.run(...)`.
3. Create `src/agent_bridge/agents/{name}/events.py` — translate agent-internal events into `BridgeEvent`s.
4. Yield only `BridgeEvent`s. Treat `prompt`, `system_prompt`, `context` as opaque pass-through.
5. Yield exactly one `Completion` at the end.
6. Wire into `src/agent_bridge/__init__.py`.
7. Add `docs/agents/{name}.md`.

Reference implementation: [Claude Code agent](docs/agents/claude.md).

---

## Configuration reference

All config loads from `.env` via `python-dotenv`. See [`.env.example`](.env.example) for templates.

### Slack platform

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `AGENT_BRIDGE_SLACK_BOT_TOKEN` | Yes | — | Bot User OAuth Token (`xoxb-...`) |
| `AGENT_BRIDGE_SLACK_APP_TOKEN` | Yes | — | App-Level Token for Socket Mode (`xapp-...`) |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL` | No | — | Channel ID to ping on startup |
| `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE` | No | — | Message body for the startup ping (both vars must be set) |

### Heartbeat platform

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `AGENT_BRIDGE_HEARTBEAT_ENABLED` | No | `false` | Master switch |
| `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | If enabled | — | Must be `> 0` |
| `AGENT_BRIDGE_HEARTBEAT_PROMPT` | If enabled | — | Fixed prompt sent every tick |
| `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | No | `./heartbeat.json` | `last_run` timestamp store |

### Claude Code agent

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `ANTHROPIC_API_KEY` | If not logged in | — | Skip if `claude login` already done |
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | No | `.` | Codebase the agent operates on |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | No | `acceptEdits` | One of `default`, `acceptEdits`, `auto`, `plan`, `dontAsk`, `bypassPermissions`, `dangerously-skip-permissions` |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | No | `600` | Per-invocation wall-clock limit |
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | No | `false` | Run each session in an isolated git worktree (requires `origin/HEAD`) |
| `AGENT_BRIDGE_CLAUDE_EFFORT` | No | `xhigh` | One of `low`, `medium`, `high`, `xhigh`, `max` |

### Bridge core

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `AGENT_BRIDGE_SESSION_STORE_PATH` | No | `./sessions.json` | Resumable session-key → UUID map |
| `AGENT_BRIDGE_SESSION_TTL_HOURS` | No | `72` | Idle TTL for resumable sessions |
| `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | No | `5` | Global semaphore size |
| `AGENT_BRIDGE_LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Run with debug logging
AGENT_BRIDGE_LOG_LEVEL=DEBUG uv run agent-bridge
```

Coding conventions, async patterns, and architectural rules are documented in [`CLAUDE.md`](CLAUDE.md). When you change a component, update the matching doc (`docs/platforms/{name}.md` or `docs/agents/{name}.md`); when you change the bridge core or event protocol, update `CLAUDE.md` and this README.

---

## License

MIT

# Agent Bridge

**Modular bridge between chat platforms and AI agents.** Message your coding
agent from Slack, Telegram, LINE, a plain HTTP endpoint, or a scheduler — and
swap the agent underneath without touching the platform side.

```
┌──────────────┐     ┌──────────┐     ┌──────────────┐
│   Platform   │────▶│  Bridge  │────▶│    Agent     │
│   (Slack)    │◀────│ (Router) │◀────│ (Claude Code)│
└──────────────┘     └──────────┘     └──────────────┘
  Session owner       Pure routing      Purely invoked
  Locking & render    Key → ID map      Yields events
  UI logic            Concurrency       No UI knowledge
```

Each layer has one job and knows nothing about the others' internals — the
bridge speaks only a five-event protocol (`Processing`, `TextDelta`,
`StatusUpdate`, `UserQuestion`, `Completion`), so any platform works with any
agent. The full contract lives in [Architecture](architecture.md).

## Supported components

Every **platform** works with every **agent** — pick one from each column:

| Platform | Connection mode | Docs |
|----------|----------------|------|
| **Slack** | Socket Mode (websocket, no public URL) | [Slack](platforms/slack.md) |
| **Telegram** | Long polling (no public URL) | [Telegram](platforms/telegram.md) |
| **LINE** | Webhook (needs a public HTTPS URL) | [LINE](platforms/line.md) |
| **POST API** | Built-in HTTP server (JSON + SSE) | [POST API](platforms/api.md) |
| **Heartbeat** | Internal scheduler (fixed-interval prompts) | [Heartbeat](platforms/heartbeat.md) |

| Agent | Drives | Docs |
|-------|--------|------|
| **Claude Code** | `claude -p` (stream-json) | [Claude Code](agents/claude.md) |
| **Codex** | `codex exec --json` | [Codex](agents/codex.md) |
| **OpenCode** | `opencode run --format json` | [OpenCode](agents/opencode.md) |

Multiple platforms run simultaneously against one configured agent; sessions
(a Slack thread, a Telegram topic, a LINE chat, an API `session` id) resume
across messages with TTL expiry.

## Where to go next

- **[Getting Started](getting-started.md)** — install, configure, run in five minutes
- **[Configuration](configuration.md)** — env vars, YAML mode, `$(VAR)` secrets, full key reference
- **[Architecture](architecture.md)** — the three-layer contract, event model, and how to add components
- **[Testing](testing.md)** — the fully-offline test pyramid and per-component playbooks
- **[Contributing](contributing.md)** — dev setup, lint, commit conventions

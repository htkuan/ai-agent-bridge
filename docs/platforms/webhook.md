# Webhook Adapter

The webhook adapter is a **machine-to-machine** platform: any system that can
speak HTTP can drive the agent. A caller POSTs a message, gets `202 Accepted`
immediately, and receives the agent's final reply later as a single JSON
payload POSTed to its `callback_url`. There is no long-lived HTTP connection —
agent turns can take minutes, so the request/response cycle is decoupled from
the turn.

Source: `src/agent_bridge/platforms/webhook/`
Transport: the shared HTTP server (`src/agent_bridge/server/`, see
[docs/server.md](../server.md)) — the adapter only contributes a router.

## When to use it

- Triggering the agent from CI, cron systems, internal tools, or another
  service — anything that can POST JSON and (optionally) receive a POST back.
- Multi-turn conversations driven by another program: reuse a
  `conversation_id` and the agent resumes the same session.
- One-shot triggers: pass `"resumable": false` and each call is an
  independent, untracked session.

If a human is on the other end and needs streaming/interactive rendering, use
a chat platform adapter (e.g. Slack) instead — the webhook delivers only the
final result.

## Setup

```bash
# The webhook platform rides on the shared HTTP server — both must be on.
AGENT_BRIDGE_HTTP_ENABLED=true
AGENT_BRIDGE_HTTP_HOST=127.0.0.1   # default; set 0.0.0.0 to expose beyond localhost
AGENT_BRIDGE_HTTP_PORT=8080        # default

AGENT_BRIDGE_WEBHOOK_ENABLED=true
AGENT_BRIDGE_WEBHOOK_TOKEN=<long random secret>
```

Validation runs at startup. Invalid configurations raise `ValueError` and
prevent the service from coming up:

| Failure | Error |
|---------|-------|
| `WEBHOOK_ENABLED=true` but no token | `AGENT_BRIDGE_WEBHOOK_TOKEN is required when the webhook platform is enabled` |
| `WEBHOOK_ENABLED=true` but `HTTP_ENABLED` unset/false | `The webhook platform needs the HTTP server: set AGENT_BRIDGE_HTTP_ENABLED=true ...` |

On startup you should see:

```
[INFO] Webhook adapter enabled (POST /platforms/webhook/v1/messages)
[INFO] HTTP server listening on http://127.0.0.1:8080
```

## API

### `POST /platforms/webhook/v1/messages`

Headers:

```
Authorization: Bearer <AGENT_BRIDGE_WEBHOOK_TOKEN>
Content-Type: application/json
```

Body:

```json
{
  "conversation_id": "deploy-review-42",
  "text": "Summarize the failing checks on PR #42.",
  "sender": "ci-bot",
  "resumable": true,
  "callback_url": "https://ci.internal/agent-callback"
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `conversation_id` | Yes | — | 1–128 chars of `[A-Za-z0-9._-]`. Defines the session: same id ⇒ same agent session (when resumable) |
| `text` | Yes | — | The message. Non-empty |
| `sender` | No | — | Identity tag; the adapter pre-tags the prompt as `[sender]: text` |
| `resumable` | No | `true` | `false` ⇒ fresh, untracked session per call (nothing persisted to `sessions.json`) |
| `agent` | No | — (bridge default) | Named agent profile to route to (`[a-z0-9_-]+`, ≤ 64 chars — see the profiles file). An unknown name is still `202`-accepted: the callback carries an `unknown_agent` error. Reusing a `conversation_id` under a *different* agent abandons the old session and starts fresh — pick one agent per conversation |
| `callback_url` | No | — | Where the result is POSTed. Omit for fire-and-forget (result only logged) |

Responses:

| Status | Meaning |
|--------|---------|
| `202` | Accepted — the turn runs in the background. Body: `{"status": "accepted", "conversation_id": ..., "resumable": ...}` |
| `401` | Missing/invalid bearer token |
| `409` | A turn for this `conversation_id` is already in flight — retry after its callback arrives |
| `422` | Body failed validation (bad `conversation_id`, empty `text`, malformed `callback_url`, `agent` not matching `[a-z0-9_-]+`) |

`202` means "accepted for processing", not "will succeed": failures discovered
later (including global capacity rejection) arrive on the callback with
`is_error: true`.

### The callback

When the turn finishes, the adapter POSTs one JSON document to
`callback_url`:

```json
{
  "conversation_id": "deploy-review-42",
  "text": "<the agent's final reply>",
  "is_error": false,
  "cost_usd": 0.0123,
  "duration_ms": 45210
}
```

Error results carry `is_error: true` and, when known, an `error_code`:

| `error_code` | Cause |
|--------------|-------|
| `capacity_full` | The bridge's global concurrency gate had no free slot (`AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS`) |
| `unknown_agent` | The request's `agent` names no registered profile — check the profiles file |
| `no_completion` | The agent stream ended without a final completion |
| `internal_error` | The turn raised unexpectedly (details in the service log) |

Delivery: any 2xx from your endpoint counts as delivered. Non-2xx responses
and transport errors are retried on a short backoff (by default two retries at
1s and 5s, 10s timeout per attempt — `WebhookConfig` fields, not env vars).
After the last attempt the result is dropped and logged at ERROR. Design the
callback receiver to be idempotent per (`conversation_id`, turn).

### Example

```bash
curl -s -X POST http://127.0.0.1:8080/platforms/webhook/v1/messages \
  -H "Authorization: Bearer $AGENT_BRIDGE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "conversation_id": "demo-1",
        "text": "What does this repo do? One paragraph.",
        "callback_url": "https://example.internal/hook"
      }'
```

## Session model

Session key: `webhook:default:{conversation_id}`.

- **Resumable (default)** — the same `conversation_id` maps to the same agent
  session until it expires (`AGENT_BRIDGE_SESSION_TTL_HOURS`). Multi-turn
  conversations just reuse the id.
- **Non-resumable** — `"resumable": false` makes the bridge mint a fresh
  ephemeral UUID per call; nothing is persisted. Use for stateless triggers.

## Concurrency

Two layers, both non-queueing:

- **Per conversation**: one turn in flight. A second POST for the same
  `conversation_id` gets `409` immediately — the caller retries after the
  callback, or uses a different id. Different conversations run in parallel.
- **Global**: all platforms share `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS`.
  Because the request was already `202`-accepted when the gate is checked,
  a capacity rejection is reported on the **callback** (`capacity_full`),
  not as an HTTP 429.

## Security

- **Bearer token, always.** The endpoint drives an agent with tool access;
  the adapter refuses to start without `AGENT_BRIDGE_WEBHOOK_TOKEN`, and every
  request is checked with a constant-time comparison. Use a long random value
  and treat it like any credential.
- **Loopback by default.** The HTTP server binds `127.0.0.1` unless
  `AGENT_BRIDGE_HTTP_HOST` says otherwise. If you expose it, terminate TLS in
  front of it (reverse proxy) — the token travels in a header.
- **`callback_url` is caller-controlled.** Authenticated callers choose where
  results go. This grants nothing they don't already have — a token holder can
  have the *agent* call arbitrary URLs anyway — but it is one more reason the
  token must stay secret. Callback requests carry no signature in v1; if your
  receiver needs authentication, put a secret in the URL you supply.

## Event handling

The adapter consumes the bridge's event stream but renders nothing
incrementally — only the final `Completion` leaves the process (on the
callback). Events are logged:

| Event | Log level | Notes |
|-------|-----------|-------|
| `Processing`, `TextDelta`, `StatusUpdate` | DEBUG | Event type only |
| `UserQuestion` | WARNING | No one can answer — the system prompt tells the agent not to ask; see below |
| `Completion` | — | Not logged per se; it becomes the callback payload |

The system prompt (owned by the adapter, opaque to the agent) frames the
invocation: machine-to-machine, no interactive user, only the final reply is
delivered, and the same `conversation_id` may continue the conversation.

## Housekeeping

Per-conversation state (the in-flight flag) lives in memory. The app's
periodic cleanup loop calls `cleanup()`, which drops entries idle longer than
`WebhookConfig.idle_state_seconds` (default 1h). Agent sessions themselves
expire via the standard session TTL.

On shutdown, in-flight turns are **cancelled** and their callbacks are not
sent — another reason callers should treat a missing callback as "unknown
outcome" and retry idempotently.

## Limitations / Non-goals

- **No streaming.** Only the final result is delivered. If callers need
  incremental output, that is an SSE/WebSocket feature to add later.
- **No queueing.** `409` per conversation, `capacity_full` globally; the
  caller owns retries.
- **No callback signing** (v1). Supply a secret inside your callback URL if
  the receiver must authenticate the poster.
- **Single token.** All callers share one credential and one `default` scope;
  per-client tokens/scopes are a future extension.

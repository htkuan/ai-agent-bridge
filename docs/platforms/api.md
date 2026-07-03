# POST API Adapter

The POST API adapter is a **generic HTTP entry point**: any program that can send an HTTP request can talk to the agent — scripts, cron jobs, CI pipelines, other services, `curl`. It runs its own `aiohttp` server with a single message endpoint supporting two response modes: **buffered JSON** (wait for the full answer) and **SSE streaming** (receive every bridge event as it happens).

Source: `src/agent_bridge/platforms/api/`

Install the optional dependency:

```bash
pip install ai-agent-bridge[api]
```

## Configuration

The adapter is **disabled by default** and requires an explicit enable — there is no secret to infer activation from (auth is optional), so like heartbeat it must be opted into:

```bash
AGENT_BRIDGE_API_ENABLED=true
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_BRIDGE_API_ENABLED` | Yes (to activate) | `false` | Explicit opt-in (`true`/`1`/`yes`/`on`) |
| `AGENT_BRIDGE_API_HOST` | No | `127.0.0.1` | Bind address — loopback only by default |
| `AGENT_BRIDGE_API_PORT` | No | `8081` | Server port (`0` = ephemeral, mainly for tests; read back via `ApiAdapter.bound_port`) |
| `AGENT_BRIDGE_API_AUTH_TOKEN` | No | — (no auth) | When set, every `/v1` request must send `Authorization: Bearer <token>` |

Every variable has a matching YAML key under `platforms.api.*` — see [docs/configuration.md](../configuration.md).

### Security

- **Default bind is `127.0.0.1`** — only local processes can reach the server. Binding `0.0.0.0` is a deliberate act; if you do, set `AGENT_BRIDGE_API_AUTH_TOKEN` and put a reverse proxy with TLS in front (the adapter itself speaks plain HTTP).
- Token comparison is constant-time (`hmac.compare_digest`).
- `GET /healthz` never requires auth (load-balancer probes); `POST /v1/messages` requires the bearer token whenever one is configured.
- Remember what this endpoint *does*: it hands arbitrary prompts to an agent that can run tools. Treat the token like a shell credential.

## Endpoints

### `GET /healthz`

Liveness probe. Always `200` with `{"status": "ok"}`. No auth.

### `POST /v1/messages`

Request body (JSON):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | **Yes** (non-empty) | The prompt, passed to the agent verbatim (the API has no sender identity, so no `[name]:` tagging) |
| `session` | string \| null | No | Client-chosen conversation id. Present → `session_key = api:client:{session}`, resumable (a later POST with the same value continues the conversation). Absent/null → one-shot: a fresh, untracked session per call |
| `system_prompt` | string \| null | No | Platform directives forwarded to the agent as-is. Omitted → a short built-in framing ("programmatic HTTP entry point, respond with the final answer") |
| `context` | object (string → string) \| null | No | Opaque metadata forwarded to the agent. The adapter always injects `"platform": "api"`, which **overrides** a client-supplied `platform` key (the breadcrumb must be trustworthy) |
| `stream` | boolean | No (default `false`) | `false` = buffered JSON response, `true` = SSE stream |

Status codes:

| Code | When | Body |
|------|------|------|
| `200` | Success (including agent-reported errors — see `is_error`) | Buffered JSON or SSE stream |
| `400` | Malformed JSON, missing/empty `text`, wrong field type | `{"error": "<what's wrong>"}` |
| `401` | Auth token configured but header missing/wrong | `{"error": "unauthorized"}` (+ `WWW-Authenticate: Bearer`) |
| `503` | Bridge at capacity (`max_concurrent_sessions` slots all busy) | Same shape as the buffered 200 body, `is_error: true` — also returned for `stream: true` (the stream never starts) |

#### Buffered mode (`stream: false`, default)

The request blocks until the agent finishes, then returns everything at once:

```json
{
  "session": "job-1",
  "text": "the final answer",
  "is_error": false,
  "usage": {
    "input_tokens": 10, "output_tokens": 5,
    "cache_read_tokens": 0, "cache_creation_tokens": 0,
    "num_turns": 1, "duration_api_ms": 900, "duration_ms": 1200,
    "cost_usd": 0.01, "total_tokens": 15
  },
  "session_usage": { "...same shape, running session total..." },
  "status_updates": ["Running Bash: ls"]
}
```

- `session` echoes the request's `session` (or `null` for one-shot).
- `text` is `Completion.text` — the authoritative full answer (buffered `TextDelta`s are only a fallback when it is empty).
- `usage` is this turn; `session_usage` is the running total for sessions the bridge has tracked from their first turn; either can be `null`.
- `status_updates` lists the agent's progress lines (`"status"` or `"status: detail"`), in order.
- A `questions` array is added **only** when the agent asked for user input (`UserQuestion`); answer by POSTing the next message with the same `session`.

`curl` example:

```bash
curl -s http://127.0.0.1:8081/v1/messages \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Summarize the failing CI job", "session": "ci-4711"}'
```

#### SSE mode (`stream: true`)

Responds with `Content-Type: text/event-stream` (`Cache-Control: no-cache`) and emits one SSE event per bridge event, then closes the stream:

| SSE `event:` | `data:` (JSON) | Bridge event |
|--------------|----------------|--------------|
| `processing` | `{}` | `Processing` |
| `text_delta` | `{"text": "..."}` | `TextDelta` |
| `status` | `{"status": "...", "detail": "..."}` | `StatusUpdate` |
| `question` | `{"questions": [...]}` | `UserQuestion` |
| `completion` | `{"text", "is_error", "usage", "session_usage"}` | `Completion` (always the last event) |

```bash
curl -sN http://127.0.0.1:8081/v1/messages \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Refactor utils.py", "session": "job-7", "stream": true}'
```

```
event: processing
data: {}

event: status
data: {"status": "Running Bash", "detail": "ls"}

event: text_delta
data: {"text": "Here is the plan..."}

event: completion
data: {"text": "Here is the plan...", "is_error": false, "usage": {...}, "session_usage": {...}}
```

- No keep-alive/heartbeat comments are sent — runs are bounded by the agent timeout.
- If the client disconnects mid-stream, the agent run **continues to completion** (session state stays consistent, resumable later); the disconnect is logged, never raised.
- A capacity rejection arrives *before* the stream starts, as a plain `503` JSON response.

## Session Semantics

**The client owns session identity.** Two modes:

| Request | Session key | `resumable` | Behavior |
|---------|-------------|-------------|----------|
| `"session": "job-1"` | `api:client:job-1` | `true` | Same value later → same agent conversation (persisted, survives restarts, expires after `AGENT_BRIDGE_SESSION_TTL_HOURS`) |
| No `session` | `api:oneshot:{uuid}` | `false` | Fresh untracked session per call; nothing persisted; cross-session dedupe skipped |

Concurrent POSTs to the **same** client session are serialized by a per-session `asyncio.Lock` (the second request waits — mind your HTTP client timeout). Different sessions and one-shot calls run concurrently up to the bridge's global cap; the adapter adds no concurrency limit of its own.

Session ids are client-chosen — pick unguessable values if callers must not join each other's conversations (all callers share one bearer token).

## Limitations

- Plain HTTP only — TLS termination belongs to a reverse proxy.
- Single shared bearer token — no per-caller identity or rate limiting.
- Buffered requests hold the HTTP connection for the whole agent run (up to the agent timeout, default 600s) — configure client timeouts accordingly, or use `stream: true`.
- No webhooks/callbacks — the response is delivered on the request connection only.

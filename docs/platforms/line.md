# LINE Adapter

The LINE adapter connects Agent Bridge to LINE via the [Messaging API](https://developers.line.biz/en/docs/messaging-api/) using a **webhook** — LINE has no polling option, so the adapter runs its own `aiohttp` HTTP server and needs a public HTTPS URL in front of it (reverse proxy, tunnel, or load balancer; LINE only delivers to `https://`).

Source: `src/agent_bridge/platforms/line/`

Install the optional dependency:

```bash
pip install ai-agent-bridge[line]
```

## Setup

### 1. Create a Messaging API channel

1. Sign in to the [LINE Developers Console](https://developers.line.biz/console/)
2. Create a **Provider** (or pick an existing one)
3. Create a **Messaging API channel** under that provider
4. On the **Basic settings** tab, copy the **Channel secret** — this is your `AGENT_BRIDGE_LINE_CHANNEL_SECRET`
5. On the **Messaging API** tab, issue a **Channel access token (long-lived)** — this is your `AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN`

### 2. Point the webhook at the bridge

1. Expose the adapter's server publicly over HTTPS (e.g. `https://bridge.example.com` → reverse-proxied to `http://<host>:8080`)
2. On the **Messaging API** tab, set **Webhook URL** to `https://bridge.example.com/line/webhook` (matching `AGENT_BRIDGE_LINE_WEBHOOK_PATH`)
3. Click **Verify** — the console sends a signed, empty-events request; the running adapter answers it with `200`
4. Enable **Use webhook**
5. Recommended: under the channel's LINE Official Account features, **disable auto-reply messages** (and greeting messages if unwanted) so the agent is the only responder

### 3. Environment variables

```bash
AGENT_BRIDGE_LINE_CHANNEL_SECRET=your-channel-secret
AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN=your-long-lived-token
```

Both are **required** — without them the adapter is disabled at startup (logged, not an error).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_BRIDGE_LINE_CHANNEL_SECRET` | Yes | — | Channel secret (webhook signature verification) |
| `AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN` | Yes | — | Channel access token (Messaging API authorization) |
| `AGENT_BRIDGE_LINE_WEBHOOK_HOST` | No | `0.0.0.0` | Webhook server bind address |
| `AGENT_BRIDGE_LINE_WEBHOOK_PORT` | No | `8080` | Webhook server port (`0` = ephemeral, mainly for tests) |
| `AGENT_BRIDGE_LINE_WEBHOOK_PATH` | No | `/line/webhook` | Webhook endpoint path (must start with `/`) |
| `AGENT_BRIDGE_LINE_API_BASE_URL` | No | `https://api.line.me` | Messaging API base URL (tests point this at a fake server) |

Every variable has a matching YAML key under `platforms.line.*` — see [docs/configuration.md](../configuration.md).

## Webhook Handling

Every request is verified before anything else: `X-Line-Signature` must equal `base64(HMAC-SHA256(channel_secret, raw request body))` (constant-time comparison). Invalid or missing signatures get `403` and are dropped.

Valid requests are **acked with `200` immediately** — LINE requires a fast response — and each qualifying event is processed in a background task. On shutdown the adapter stops accepting webhooks first, gives in-flight tasks a bounded grace window (5s) to finish, then cancels the rest.

## Session Semantics

**One chat = one agent session.** LINE has no threads; the chat room is the session scope.

```
line:{source_type}:{userId | groupId | roomId}
```

- 1:1 chats: `line:user:{userId}`
- Group chats: `line:group:{groupId}`
- Multi-person rooms: `line:room:{roomId}`
- Sessions expire after `AGENT_BRIDGE_SESSION_TTL_HOURS` (default 72h).

A per-session `asyncio.Lock` serializes messages in the same chat (a second message waits for the current run to finish); different chats process concurrently up to the bridge's global cap.

## Message Filtering

| Rule | Behavior |
|------|----------|
| Non-`message` events (follow, join, postback, beacon, …) | Ignored |
| Non-text messages (stickers, images, files, …) | Ignored |
| Empty text (after trimming) | Ignored |
| Source without a resolvable chat id | Ignored |
| Webhook verification requests (valid signature, empty `events`) | `200`, nothing processed |

There is no mention-gating: in groups the bot receives messages according to LINE's own delivery rules, and every delivered text message is answered.

### Prompt prefix and system prompt

- Prompts are tagged with the sender's LINE user id: `[{userId}]: text`. The display name is **not** resolved — that would cost an extra profile-API round-trip per message (a future extension could call the profile API and cache results). Group members who haven't added the bot as a friend arrive without a `userId` and are tagged `[unknown]`.
- The system prompt carries the LINE framing: the speaker-tag convention, plain-text-only output (LINE renders no Markdown), the 5000-character split behavior, the single-reply (no streaming) nature, and the chat scope (`user`/`group`/`room` + id).

## Event Rendering

LINE is **not streamed**: the reply token is single-use, so the whole answer is delivered once, at `Completion`.

| Event | Rendering |
|-------|-----------|
| `Processing` | Log only (no placeholder message) |
| `TextDelta` | **Buffered** — accumulated and delivered at `Completion` |
| `StatusUpdate` | Log only |
| `UserQuestion` | Rendered at completion as a plain-text question list with options; the user answers by sending the next message in the same chat |
| `Completion` | Final text (buffered deltas win over `Completion.text`) sent via the **Reply API** using the event's `replyToken`. `is_error=True` prefixes `❌ `. Empty text renders `No response from agent.` |

Delivery details:

- Text over 5000 characters is split at newline boundaries where possible; a single Reply call carries up to **5** messages, and any further chunks are sent via the **Push API** in batches of 5.
- If the reply fails (expired/already-used token — LINE returns 400 — or any other non-200/transport error), the entire answer **falls back to the Push API**, targeted at the source chat id.
- An event without a `replyToken` is delivered via Push directly.
- Messaging API errors are logged, never raised.

## Limitations

- **Public HTTPS URL required** — LINE only delivers webhooks to `https://`; put a reverse proxy or tunnel in front of the adapter's plain-HTTP server.
- **No streaming and no progress signal** — the user sees nothing until the agent finishes (the system prompt tells the agent this). A private-chat loading animation is a possible future nicety.
- **Sender identity is the raw user id**, not a display name (see above).
- Push fallback consumes the channel's push-message quota (free-plan message limits apply); replies are free.
- Redelivered webhook events (`deliveryContext.isRedelivery`) are not deduplicated by the adapter — enable the bridge's prompt dedupe if this matters in your deployment.
- No file/attachment handling (text messages only).

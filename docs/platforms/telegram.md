# Telegram Adapter

The Telegram adapter connects Agent Bridge to Telegram via the [Bot API](https://core.telegram.org/bots/api) using **long polling** — no public URL or webhook required (same philosophy as Slack Socket Mode). It calls the Bot API directly over `aiohttp`; no bot-framework dependency.

Source: `src/agent_bridge/platforms/telegram/`

Install the optional dependency:

```bash
pip install ai-agent-bridge[telegram]
```

## Setup

### 1. Create a bot with BotFather

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, choose a display name and a username (must end in `bot`)
3. Copy the token BotFather returns (`123456789:AA...`) — this is your `AGENT_BRIDGE_TELEGRAM_BOT_TOKEN`

### 2. Privacy mode (groups)

By default Telegram bots run with **privacy mode enabled**: in groups they only receive messages that @mention them, replies to their own messages, and commands. This matches the adapter's group behavior exactly, so **you can leave privacy mode on** (recommended).

If you disable it (`/setprivacy` → Disable with BotFather), the bot receives *all* group messages — the adapter still ignores everything that isn't an @mention or a reply to the bot, but every message is delivered to your machine. Note: changing the privacy setting requires removing and re-adding the bot to existing groups.

### 3. Environment variables

```bash
AGENT_BRIDGE_TELEGRAM_BOT_TOKEN=123456789:AAExampleToken
```

The token is **required** — without it the adapter is disabled at startup (logged, not an error).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_BRIDGE_TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from BotFather |
| `AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS` | No | — (allow all) | Comma-separated chat-id allow-list; messages from other chats are silently ignored |
| `AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS` | No | `30` | `getUpdates` long-poll wait; `0` = short polling |
| `AGENT_BRIDGE_TELEGRAM_STATE_PATH` | No | `./telegram.json` | Persists the last processed `update_id` across restarts |
| `AGENT_BRIDGE_TELEGRAM_API_BASE_URL` | No | `https://api.telegram.org` | Bot API base URL (tests point this at a fake server) |

Every variable has a matching YAML key under `platforms.telegram.*` — see [docs/configuration.md](../configuration.md).

### Optional: chat allow-list

```bash
AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS=-1001234567890,987654321
```

- Comma-separated **chat ids** (group ids are negative; use the raw id, not the @name).
- Empty / unset = every chat is allowed.
- Unlike Slack's allow-list there is no rejection reply — messages from non-allowed chats are **silently ignored** (a bot added to a random group shouldn't announce itself).

## Session Semantics

**One chat (or forum topic) = one agent session.**

```
telegram:{chat_id}:{message_thread_id | 0}
```

- Private chats and regular groups: thread segment is `0` — the whole chat is one session.
- Forum supergroups (topics enabled): each topic (`is_topic_message` + `message_thread_id`) is its own session.
- Sessions expire after `AGENT_BRIDGE_SESSION_TTL_HOURS` (default 72h).

A per-session `asyncio.Lock` serializes messages in the same session (a second message waits for the current run to finish); different chats process concurrently up to the bridge's global cap.

## Message Filtering

| Rule | Behavior |
|------|----------|
| Non-text messages (photos, stickers, …) | Ignored |
| Messages from bots | Ignored |
| Chat not in `ALLOW_CHATS` (when set) | Silently ignored |
| Private chat | Every text message is answered |
| Group / supergroup | Only answered when the text @mentions the bot (username resolved via `getMe` at startup, matched case-insensitively) **or** the message is a reply to one of the bot's messages |
| Channel posts | Ignored |
| Text empty after stripping the mention | Ignored |

The `@botusername` mention is stripped before the text is sent to the agent.

### Prompt prefix and system prompt

- Prompts are tagged with sender identity: `[{first_name} {last_name} ({user_id})]: text` (falls back to username, then `unknown`).
- The system prompt carries the Telegram framing: the speaker-tag convention, chat id/title/type, topic thread (if any), the bot's own mention, and a note that replies should prefer plain text and long replies are split.

## Event Rendering

| Event | Rendering |
|-------|-----------|
| `Processing` | Sends a `⏳ Processing...` placeholder as a reply to the triggering message (with `message_thread_id` in forum topics) |
| `StatusUpdate` | Edits the placeholder to `⏳ {status}` (throttled to one edit per 1.5s) |
| `TextDelta` | **Buffered** — Telegram rate-limits message edits hard, so deltas are accumulated and rendered once at `Completion` (the placeholder + status edits provide the progress signal) |
| `UserQuestion` | Rendered at completion as a plain-text question list with options; the user answers by sending the next message in the same chat/topic (in groups: mention the bot or reply to it) |
| `Completion` | Placeholder is edited into the final text. Over 4096 characters: the first chunk edits the placeholder, the rest are sent as follow-up messages (split at newline boundaries where possible). `is_error=True` prefixes `❌ `. Empty text renders `No response from agent.` |

Delivery details:

- Final messages are sent with `parse_mode: Markdown`; if Telegram rejects the message (Markdown parse error), it is **re-sent as plain text** — the reply always gets through.
- If editing the placeholder fails (e.g. it was deleted), the text falls back to a fresh reply message.
- A lone error `Completion` (capacity full — no `Processing` was emitted) is sent directly as a reply, no placeholder involved.

## Offset Persistence and Delivery Guarantees

The adapter long-polls `getUpdates` with `offset = last_update_id + 1` and `allowed_updates=["message"]`. After dispatching each batch it immediately writes `last_update_id` to `AGENT_BRIDGE_TELEGRAM_STATE_PATH` (same state-file pattern as heartbeat).

This is **at-most-once** by design: the offset is persisted when a batch is *dispatched*, not when the agent finishes. A crash mid-run loses that message rather than replaying it into the agent on restart — duplicated agent work (edits, commits, API calls) is worse than a lost chat message the user can resend.

Network or API errors in the poll loop never crash the adapter: they are logged and retried with exponential backoff (1s doubling to a 30s cap, reset on success). The `getMe` identity lookup happens inside the same retry loop, so startup succeeds even while Telegram is unreachable.

## Limitations

- **No streaming text** — replies appear when the agent finishes (see `TextDelta` above).
- **Group answers to `UserQuestion` must re-address the bot** (mention or reply); in private chats any next message works.
- The 4096-character split counts Python characters, which is a close-enough approximation of Telegram's limit for practical messages.
- No file/attachment handling (text messages only).

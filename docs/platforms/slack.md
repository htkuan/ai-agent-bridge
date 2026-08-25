# Slack Adapter

The Slack adapter connects Agent Bridge to Slack workspaces via [Socket Mode](https://api.slack.com/apis/socket-mode). It defines **thread = session**, manages per-session concurrency, and renders agent events as real-time Slack messages.

Source: `src/agent_bridge/platforms/slack/`

## Setup

### 1. Create Slack App

Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app.

### 2. Enable Socket Mode

- Go to **Socket Mode** in the left sidebar
- Toggle it **on**
- Generate an **App-Level Token** with `connections:write` scope
- Save the token (`xapp-...`) — this is your `AGENT_BRIDGE_SLACK_APP_TOKEN`

### 3. Bot Token Scopes

Under **OAuth & Permissions**, add these Bot Token Scopes:

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Receive @mention events in channels |
| `chat:write` | Send and update messages |
| `files:write` | Upload file snippets when response exceeds message length limit |
| `im:history` | Read DM message history |
| `im:read` | Access DM channels |
| `channels:read` | Resolve public channel names (`conversations.info`) |
| `groups:read` | Same, for private channels — only if the bot is invited to any |
| `mpim:read` | Same, for group DMs |
| `users:read` | Resolve sender display names (`users.info`) |
| `team:read` | Resolve the workspace name (`team.info`) |

The last five feed the context the agent receives (workspace / `#channel` / speaker
name) and the channel allow-list. Without them the adapter logs a
`missing_scope` warning per lookup and falls back to raw IDs — see
[Troubleshooting](#troubleshooting). After adding scopes, **reinstall the app**;
tokens issued earlier do not gain new scopes.

### 4. Event Subscriptions

Under **Event Subscriptions**, subscribe to these bot events:

| Event | Purpose |
|-------|---------|
| `app_mention` | Triggers when someone @mentions the bot in a channel |
| `message.im` | Triggers when someone sends a DM to the bot |

### 5. Install to Workspace

Install the app and copy the **Bot User OAuth Token** (`xoxb-...`) — this is your `AGENT_BRIDGE_SLACK_BOT_TOKEN`.

### 6. Environment Variables

```bash
AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-your-bot-token
AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-your-app-level-token
```

Both are **required**. The adapter raises `ValueError` at startup if either is missing.

### Optional: Startup Notification

Send a Slack message after Socket Mode connects successfully:

```bash
AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL=C12345678
AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE=Bot is online :white_check_mark:
```

Both must be set for the notification to fire. Useful for deploy pipelines that need confirmation the bot is actually alive and connected.

### Optional: Channel Allow-List

Restrict which channels can reach the agent:

```bash
AGENT_BRIDGE_SLACK_ALLOW_CHANNELS=ops-alerts,team-eng,incidents
```

- Comma-separated **channel names** (the `#name`, not the `C0123...` ID). Leading `#`, surrounding whitespace, and case are normalized away, so `#Ops-Alerts` and `ops-alerts` match the same channel.
- **Empty / unset = allow every channel** (backward compatible).
- When non-empty, only messages from listed channels reach the agent. Anything else gets a reply in-thread (see `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` below) and is then dropped before any agent work.
- `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` overrides that reply text; it defaults to a fixed English notice.
- **DMs are also gated.** A DM has no channel name (it falls back to the channel ID), so it never matches a name in the list — meaning a non-empty allow-list blocks all DMs. Leave the list empty if you want DMs to keep working.

The gate runs first in `_process_message`, resolving the channel name via the cached `conversations:info` lookup, so it costs at most one API call per distinct channel. That lookup needs `channels:read` (see [Bot Token Scopes](#3-bot-token-scopes)) — without it the name resolution fails and **every** channel is rejected, allow-listed or not. See [Troubleshooting](#troubleshooting).

### Optional: Per-Channel Claude Profiles

Route different channels to differently-configured Claude controllers — a different
`work_dir`, permission mode, model, etc. per channel. Point `AGENT_BRIDGE_PROFILES_PATH`
at a TOML file (template: `profiles.example.toml`):

```toml
[claude.profiles.backend]
work_dir = "/repos/backend"

[claude.profiles.infra]
work_dir = "/repos/infra"
permission_mode = "plan"
model = "claude-opus-5"

[slack.channel_profiles]
backend-team = "backend"
infra-ops = "infra"
```

- Keys under `[slack.channel_profiles]` are **channel names**, normalized like the
  allow-list (`#`, whitespace, and case ignored). Values are profile names defined under
  `[claude.profiles.*]` — see [the Claude agent docs](../agents/claude.md#named-profiles)
  for what a profile can set.
- **Unmapped channels (and DMs, which have no name) use the default env-built controller.**
- Startup fails fast on dead config: a mapping to an undefined profile, or — when
  `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` is set — a mapped channel missing from the
  allow-list.
- The mapping key is the channel *name*, so **renaming a channel drops it out of its
  mapping** (same caveat as the allow-list) — it falls back to the default controller
  until the file is updated and the bridge restarted.
- **Remapping a channel starts fresh sessions for its threads.** A session created under
  one profile's `work_dir` cannot be `--resume`d under another's, so the session manager
  detects the profile change, abandons the old session (its worktree is cleaned up by the
  periodic cleanup), and mints a new one.
- If the channel-name lookup fails transiently (Slack API error), the failure is not
  cached: that one message can't match a mapping (with an allow-list set it is rejected
  outright; otherwise it falls back to the default controller), and the next message
  retries the lookup.

### Optional: Usage / Cost Report

Append a usage/cost footer below the final agent reply:

```bash
AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED=true
```

- **Unset / false = no footer** (default).
- The data comes from `Completion.usage` (this turn) and `Completion.session_usage` (the session's running total), which the bridge assembles from the agent's reported usage. See [bridge.md](../bridge.md#usage-reporting) for the generic `Usage` structure and how the session total is accumulated.
- **The session line only appears when the bridge has tracked the session from its first turn.** After a process restart (the accumulator is in-memory) or for a pre-existing session, `session_usage` is `None` and only the current-turn line is shown — never a misleadingly-low total.

The footer is rendered as an italic Slack footnote beneath a labelled divider (a custom template is wrapped the same way). Default layout:

```
─────cost─────
_💰 $0.0123 · 🔢 1234 in / 456 out · 📦 3400 cached · ⏱️ 12.3s_
_📈 session: $0.0456 · 18700 tokens · 3 turns_
```

#### Custom template

Override the format with a `{placeholder}` template:

```bash
AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE=💵 {cost_usd} | {total_tokens} tokens | {duration_s}s
```

Unknown placeholders render blank and a malformed template degrades to its raw text (it never breaks the reply). Available placeholders — each also has a `session_`-prefixed variant for the running total (e.g. `{session_cost_usd}`); session placeholders render `0` when the session total isn't tracked:

| Placeholder | Meaning |
|-------------|---------|
| `{cost_usd}` | Cost in USD, 4 decimals |
| `{input_tokens}` / `{output_tokens}` | Token counts (exclude cache) |
| `{cache_read_tokens}` / `{cache_creation_tokens}` | Cache token counts |
| `{total_tokens}` | Real total: input + output + cache read + cache creation |
| `{num_turns}` | Agent turns |
| `{duration_ms}` / `{duration_s}` | Wall-clock duration |
| `{duration_api_ms}` | API-only duration |

## Session Semantics

**One Slack thread = one agent session.**

The session key format is:

```
slack:{channel_id}:{thread_ts}
```

- `channel_id` — the Slack channel or DM channel ID
- `thread_ts` — the thread's root message timestamp

If a message has no thread (standalone message), `thread_ts` falls back to the message's own `ts`, which starts a new thread/session.

This means:
- Every reply in the same thread continues the same agent session
- A new thread (or new standalone message) starts a fresh session
- Sessions expire after `AGENT_BRIDGE_SESSION_TTL_HOURS` (default 72h)

## Per-Session State Machine

Each session has its own state managed by `_SessionState`:

```
                    ┌──────────────────────┐
                    │       IDLE           │
                    │  processing = false  │
                    │  waiting = false     │
                    └──────────┬───────────┘
                               │ new message
                               ▼
                    ┌──────────────────────┐
          ┌────────│    PROCESSING        │◀──── drain pending
          │        │  processing = true   │
          │        └──────┬───────┬───────┘
          │               │       │
          │    completion  │       │ AskUserQuestion
          │               ▼       ▼
          │        ┌─────────┐  ┌──────────────────────┐
          │        │  IDLE   │  │  WAITING FOR ANSWER  │
          │        └─────────┘  │  waiting = true      │
          │                     └──────────┬───────────┘
          │                                │ user replies
          │                                ▼
          │                     ┌──────────────────────┐
          └────────────────────▶│    PROCESSING        │
   new message while            └──────────────────────┘
   processing → QUEUE
```

### States

| State | `processing` | `waiting_for_answer` | Behavior on new message |
|-------|:---:|:---:|------|
| **Idle** | `false` | `false` | Start processing immediately |
| **Processing** | `true` | `false` | Queue as pending (keep only latest) |
| **Waiting for answer** | `false` | `true` | Treat as answer, resume session |

### Pending message queue

When a session is processing and a new message arrives:

1. The new message replaces any existing pending message (only the **latest** is kept)
2. A `:hourglass: Waiting for previous task to finish...` message is posted
3. The previous pending's placeholder message is deleted
4. After the current processing finishes, the pending message is drained and processed

This prevents message pileup while ensuring the latest user intent is always honored.

## Event Handling

The adapter consumes `BridgeEvent`s from the bridge and renders them as Slack messages:

### Processing

Posts (or updates) an initial `:hourglass_flowing_sand: Processing...` message in the thread.

### TextDelta

Accumulates text chunks and updates the Slack message periodically (throttled to every **1.5 seconds** to respect Slack API rate limits of ~1 req/sec per method).

### StatusUpdate

Appends an italic status line (e.g. `_Using Read..._`) below the accumulated text. Also throttled.

### UserQuestion (AskUserQuestion)

When the agent needs user input:

1. Formats questions with options for Slack display
2. Updates (or posts) the message with the formatted question
3. Sets session state to `waiting_for_answer`
4. Processing pauses until the user replies in the thread

Example Slack output:

```
:question: *Claude needs your input*

Should I proceed with the refactoring?
  • `yes` — Apply all changes
  • `no` — Abort and revert
  • `partial` — Only apply safe changes

Reply in this thread to answer.
```

### Completion

Updates the message with the final response text. Error cases:

| Scenario | Display |
|----------|---------|
| Normal completion | Final agent text |
| Capacity full (new request) | `:no_entry: Too many requests being processed, please try again later.` |
| Capacity full (pending drained) | `:x: Your queued message could not be processed — please try again shortly.` |
| Any other error (timeout, missing CLI, non-zero exit, unknown agent) | `:warning: ` + the reason the completion reported, e.g. `:warning: Claude process timed out after 300.0s` — followed by whatever text streamed before the failure |
| Error with no reported reason | `:warning: The agent failed without reporting a reason.` |
| No response | `_No response from agent._` |
| Response too long (> ~3900 UTF-8 bytes) | Preview (up to 1000 bytes) + note, full content uploaded as `response.md` file snippet; if upload fails, user sees `(response too long; upload failed — please retry)` |

Only the bridge's capacity gate (`error_code = "capacity_full"`) gets a
platform-worded notice — it is about the bridge, not this turn. Every other
failure carries its own reason in `Completion.text`, so the adapter surfaces
that verbatim rather than a blanket notice: a 20s timeout must not read as a
concurrency problem, since the fix (raise `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS`)
is nothing like the fix for a full bridge (raise
`AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` or retry). The reason leads the message
so it survives truncation when a long partial reply follows it.

On a successful (non-error) completion, a usage/cost footer is appended when `AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED=true` (see [Usage / Cost Report](#optional-usage--cost-report)).

## File Attachments

When users upload files in their Slack message, the adapter:

1. Extracts file metadata: name, MIME type, and private download URL
2. Constructs a curl command hint with the bot token for authentication
3. Appends the hint + file list to the user's prompt text

The agent receives something like:

```
user's message text

[Slack attachments — download with: curl -H "Authorization: Bearer xoxb-..." "<url>" -o /tmp/<filename>]
- report.pdf (application/pdf): https://files.slack.com/files-pri/...
- screenshot.png (image/png): https://files.slack.com/files-pri/...
```

The agent can then decide whether to fetch and process the files.

## Context Resolution

The adapter resolves display names for Slack entities and uses them to build the prompt prefix and the agent's system prompt:

| Field | Source | Purpose |
|-------|--------|---------|
| `workspace` | `team_info()` API | Workspace name |
| `channel_id` | Event payload | Channel identifier |
| `channel_name` | `conversations_info()` API | Human-readable channel name |
| `thread_ts` | Event payload | Thread root timestamp |
| `user_id` | Event payload | Slack user ID |
| `user_name` | `users_info()` API | Display name or real name |
| `bot_user_id` | `auth.test()` API at startup | The bot's own Slack user ID — surfaced as `Your Slack mention: <@U…>` so the agent can detect when users @-mention it |

All resolutions are cached by `SlackInfoCache` to avoid repeated API calls. The bot user ID is fetched once during `start()` and reused for every request. The bot's display name is intentionally not surfaced — the Slack app's name and the AI agent's persona are independent concerns.

Every lookup degrades rather than fails: on `SlackApiError` the adapter logs a warning, substitutes the raw ID (or `""` for the workspace), and carries on. The fallback is cached too, so a failing lookup costs one API call per entity, not one per message — but it also means fixing the token requires a restart to re-resolve.

## Troubleshooting

### `Failed to resolve channel name for C…: missing_scope`

The bot token lacks the scope `conversations.info` needs for that conversation type — `channels:read` for public channels, `groups:read` for private ones, `mpim:read` for group DMs. Add the missing scope under **OAuth & Permissions**, **reinstall the app** to issue a new token, then restart the bridge. The warning text names the scope Slack asked for (`add bot token scope: …`).

Two consequences while the scope is missing:

- The agent's system prompt carries `Channel: C0123ABC` instead of `Channel: #ops-alerts` — degraded context, everything else still works.
- **If `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` is set, the gate rejects every channel.** The allow-list matches on names; with resolution failing, the name falls back to the channel ID, which never matches a listed name, so allowed channels get the "not available in this channel" reply. Look for `Rejecting message from non-allowed channel` alongside the warning.

The sibling warnings behave the same way: `Failed to resolve workspace name` wants `team:read`, `Failed to resolve user name` wants `users:read`.

### Prompt prefix and system prompt

The adapter (not the agent) owns chat-platform framing. Two static helpers shape what's sent to the bridge:

- `_tag_prompt(text, context)` — prefixes the user's message with `[user_name (user_id)]:` so the agent knows who is speaking. The tagged string is passed as `text` to `bridge.handle_message`.
- `_build_system_prompt(context)` — produces the Slack system prompt (workspace/channel/thread metadata, the bot's mention syntax, and the convention that every message is sender-tagged) and passes it as `system_prompt`.

This split keeps the agent platform-agnostic: a future platform that sends raw data (heartbeat, webhooks, queues, etc.) can produce its own prefix + system prompt without changing the agent.

## Stale Session Cleanup

The adapter's `cleanup()` override (the `PlatformAdapter` protocol's periodic
housekeeping hook, called by `app.py`'s cleanup loop every hour by default)
removes stale session state entries — sessions that:
- Are not currently processing
- Are not waiting for an answer
- Have no pending messages
- Have expired in the `SessionManager`

This prevents memory leaks from accumulated `_SessionState` objects.

## Implementing a New Platform Adapter

Use the Slack adapter as a reference. Subclass `BasePlatformAdapter[YourRunState]`
(`platforms/base.py`) — it owns the bridge call and the event dispatch. A platform
adapter must:

1. **Define session key format** — how messages map to sessions (`make_session_key`)
2. **Pre-process** — build a `BridgeRequest` from your platform's native event and call `process()`
3. **Post-process** — override the `on_*` hooks to render each event type (Slack: six hooks delegating to its `_render_*` helpers)
4. **Implement lifecycle** — `start()` and `stop()`
5. **Own per-session locking** — prevent concurrent processing of the same session
6. **Handle `UserQuestion`** — pause and wait for user's answer
7. **Manage pending messages** — decide queuing strategy (Slack keeps only the latest)

The bridge and agent require zero changes.

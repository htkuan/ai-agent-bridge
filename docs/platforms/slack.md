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
| No response | `_No response from agent._` |
| Response too long | First 300 chars + note, full content uploaded as `response.md` file snippet |

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

The adapter resolves display names for Slack entities and passes them as context to the agent:

| Field | Source | Purpose |
|-------|--------|---------|
| `platform` | Hardcoded `"slack"` | Agent knows which platform |
| `workspace` | `team_info()` API | Workspace name |
| `channel_id` | Event payload | Channel identifier |
| `channel_name` | `conversations_info()` API | Human-readable channel name |
| `thread_ts` | Event payload | Thread root timestamp |
| `user_id` | Event payload | Slack user ID |
| `user_name` | `users_info()` API | Display name or real name |

All resolutions are cached by `SlackInfoCache` to avoid repeated API calls.

This context is passed to the agent as a system prompt appendix, so the agent knows who is speaking and from where.

## Stale Session Cleanup

A periodic cleanup task (every hour by default) removes stale session state entries — sessions that:
- Are not currently processing
- Are not waiting for an answer
- Have no pending messages
- Have expired in the `SessionManager`

This prevents memory leaks from accumulated `_SessionState` objects.

## Implementing a New Platform Adapter

Use the Slack adapter as a reference. A platform adapter must:

1. **Define session key format** — how messages map to sessions
2. **Implement `PlatformAdapter` protocol** — `start()` and `stop()`
3. **Own per-session locking** — prevent concurrent processing of the same session
4. **Consume `BridgeEvent`s** — call `bridge.handle_message()` and render each event type
5. **Handle `UserQuestion`** — pause and wait for user's answer
6. **Manage pending messages** — decide queuing strategy (Slack keeps only the latest)

The bridge and agent require zero changes.

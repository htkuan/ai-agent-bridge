# Claude Code Agent

The Claude agent integrates [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as the AI backend. It spawns `claude -p` subprocesses, parses the stream-json output, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/claude/`

## How It Works

Each user message triggers a **one-shot subprocess**:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  [--session-id ID | --resume ID] \
  [--permission-mode MODE] \
  [--append-system-prompt "<context>"]
```

The process runs, streams events via stdout, and exits. Session continuity is handled by Claude Code's built-in `--session-id` (new) and `--resume` (continue) flags.

### Why one-shot (not long-running)?

- Simpler lifecycle — no idle process management
- No resource consumption between messages
- Crash isolation — one failure doesn't take down the service
- Claude Code handles session persistence internally

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_CLAUDE_WORK_DIR` | `.` | Working directory where Claude Code operates. This determines which codebase the agent has access to. Path is resolved to absolute at startup. |
| `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | `acceptEdits` | Controls what Claude can do without asking. |
| `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process is terminated on timeout. |

### Permission Modes

| Mode | Behavior |
|------|----------|
| `default` | Asks for permission on everything |
| `acceptEdits` | Auto-accepts file edits, asks for other actions |
| `auto` | Auto-accepts most actions |
| `plan` | Planning mode — suggests but doesn't execute |
| `dontAsk` | Don't ask questions, skip actions that would require permission |
| `bypassPermissions` | Bypass permission checks |
| `dangerously-skip-permissions` | Skip all permission checks (uses `--dangerously-skip-permissions` flag instead of `--permission-mode`) |

Validation happens at startup — invalid modes raise `ValueError`.

## Command Building

The controller builds the CLI command in `_build_command()`:

### Prompt tagging

User prompts are prefixed with sender identity:

```
[user_name (user_id)]: original message text
```

This lets Claude Code know who is speaking when multiple users interact in the same session.

### Session handling

| Scenario | Flag | Effect |
|----------|------|--------|
| New session | `--session-id {uuid}` | Creates a fresh Claude Code session |
| Existing session | `--resume {uuid}` | Continues from where the last message left off |

### System prompt

When context is available (platform, workspace, channel info), it's appended as a system prompt:

```
This conversation is from a chat platform. Each message is prefixed with [user_name (user_id)] to identify the speaker.
Platform: slack
Workspace: MyCompany
Channel: #engineering (C12345)
Thread: 1234567890.123456
```

## Event Flow

### Claude stream-json → BridgeEvent

The Claude CLI outputs one JSON object per line. The event parser (`events.py`) handles:

| Claude Event | BridgeEvent | Notes |
|-------------|-------------|-------|
| `system` (init) | *filtered* | Session init, model info — internal only |
| `assistant` (text) | `TextDelta` | Incremental text response |
| `assistant` (thinking) | *filtered* | Extended thinking — internal only |
| `assistant` (tool_use) | `StatusUpdate` | `"Using {tool_name}..."` |
| `assistant` (tool_use: AskUserQuestion) | `UserQuestion` | Special case — carries questions + options |
| `user` (tool_result) | *filtered* | Tool execution results — internal only |
| `result` | `Completion` | Final result with cost, duration, error status |

Key design: **agent-internal events never reach the platform**. Thinking, tool results, and init events are filtered out within this module.

### AskUserQuestion

When Claude uses the `AskUserQuestion` tool, it's intercepted and converted to a `UserQuestion` event:

```python
# Claude tool_use event
{"type": "assistant", "message": {"content": [
  {"type": "tool_use", "name": "AskUserQuestion", "input": {
    "questions": [
      {"question": "Which approach?", "options": ["A", "B"], "multiSelect": false}
    ]
  }}
]}}

# Converted to BridgeEvent
UserQuestion(questions=[{"question": "Which approach?", "options": ["A", "B"], "multiSelect": false}])
```

The platform adapter is responsible for rendering the question and collecting the user's answer.

### Multi-content blocks

A single Claude `assistant` message can contain multiple content blocks (e.g., thinking + text + tool_use). The parser handles all blocks in order, yielding separate events for each.

## Process Management

### Subprocess lifecycle

```
1. asyncio.create_subprocess_exec() — spawn claude process
2. Read stdout line-by-line (with overall timeout)
3. Background task drains stderr (prevents pipe buffer deadlock)
4. On completion: collect return code + stderr
5. On timeout: SIGTERM → wait 5s → SIGKILL
```

### Buffer size

The stdout line buffer is set to **10 MB** (default is 64 KB). Claude Code can produce very long single-line JSON objects (e.g., large tool results), and the default buffer causes `LimitOverrunError`.

### Timeout handling

- An overall deadline is set at `now + timeout_seconds`
- Each `readline()` call checks remaining time
- On timeout: yields an error `Completion`, terminates the process
- Separate from per-line timeout — it's a total wall-clock limit

### Error cases

| Scenario | Result |
|----------|--------|
| Process timeout | Error `Completion` with timeout message, process killed |
| Non-zero exit code | Error `Completion` with exit code, stderr logged |
| Invalid JSON line | Warning logged, line skipped |
| Pipe buffer overflow | Prevented by 10 MB buffer setting |

## Implementing a New Agent

Use the Claude agent as a reference. An agent controller must:

1. **Implement `AgentController` protocol**:
   ```python
   def run(self, session_id: str, prompt: str, is_new: bool,
           context: dict[str, str] | None = None) -> AsyncIterator[BridgeEvent]:
   ```

2. **Yield only `BridgeEvent`s** — define internal event types in your own `events.py`, convert them to generic events before yielding

3. **Handle session continuity** — use `session_id` and `is_new` to manage session state however your agent backend supports it

4. **Respect the contract**:
   - Yield `TextDelta` for incremental text
   - Yield `StatusUpdate` for progress indicators
   - Yield `UserQuestion` if you need user input
   - Always yield exactly one `Completion` at the end (success or error)

5. **Create a config** — `{Name}Config` with `from_env()` classmethod and `_validate()` method

The bridge and platform adapters require zero changes.

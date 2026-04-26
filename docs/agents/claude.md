# Claude Code Agent

The Claude agent integrates [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as the AI backend. It spawns `claude -p` subprocesses, parses the stream-json output, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/claude/`

## How It Works

Each user message triggers a **one-shot subprocess**:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  [-w <session_id>] \
  [--session-id ID | --resume ID] \
  [--permission-mode MODE] \
  [--append-system-prompt "<context>"]
```

`-w <session_id>` is included only when [Worktree Mode](#worktree-mode) is enabled.

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
| `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | `false` | Run each session in its own git worktree (see [Worktree Mode](#worktree-mode)). |

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

### Worktree Mode

When `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED=true`, every session runs in an isolated git worktree, so concurrent sessions never clobber each other's files.

**Layout**

```
<work_dir>/
├── .claude/
│   └── worktrees/
│       ├── <session_id_1>/      # checked-out branch: worktree-<session_id_1>
│       └── <session_id_2>/      # checked-out branch: worktree-<session_id_2>
└── ...
```

The path and branch names are deterministic: the controller passes `-w <session_id>` on every invocation. Claude Code creates the worktree on the first call and reuses it on `--resume`, automatically running commands with the worktree as cwd.

**Prerequisites (enforced at startup)**

- `work_dir` must be a git repository
- An `origin` remote must exist with a resolvable `origin/HEAD` — Claude uses it as the base branch. Run `git remote set-head origin --auto` if `symbolic-ref refs/remotes/origin/HEAD` is missing.

Startup fails with a clear error if these are not met.

**Lifecycle**

| Event | What happens |
|-------|--------------|
| First message in session | `claude -p -w <session_id> --session-id <session_id> ...` creates the worktree off `origin/HEAD` with branch `worktree-<session_id>`. |
| Follow-up messages | `claude -p -w <session_id> --resume <session_id> ...` — Claude auto-detects the existing worktree and runs in it. |
| Session expires (TTL) | Periodic cleanup calls `git worktree remove` then `git branch -D worktree-<session_id>`. |
| Worktree has uncommitted changes on expiry | Removal is skipped, path is logged. Inspect and clean up manually. |
| Manual `rm -rf` on the worktree dir | Controller calls `git worktree prune` before the next session so a fresh worktree can be recreated from the existing branch. |

**Limitations**

- Base branch is always `origin/HEAD`. Override by adding a [`WorktreeCreate` hook](https://docs.anthropic.com/en/docs/claude-code/hooks) in the repo.
- Gitignored dependencies (e.g., `node_modules`, `.venv`) are *not* shared between worktrees — Claude will install into each worktree separately.
- External state (databases, bound ports, credentials) stays shared — worktrees only isolate the filesystem.

## Command Building

The controller builds the CLI command in `_build_command()`:

### Prompt and system prompt: platform-driven

The controller is **platform-agnostic**: it never inspects `context` to construct prompts or system text. Whatever the platform supplies through `bridge.handle_message(text=..., system_prompt=...)` is forwarded as-is to `claude -p` and `--append-system-prompt` respectively.

This means each platform owns its own framing:

| Platform | What it puts in `text` | What it puts in `system_prompt` |
|----------|------------------------|----------------------------------|
| Slack    | `[user_name (user_id)]: original message` | "This conversation is from a chat platform…" + workspace/channel/thread metadata |
| Heartbeat | The configured prompt verbatim, no prefix | "This is a scheduled invocation, no user listening…" + `fired_at` |
| (new platform) | Whatever convention fits its sender semantics | Whatever directives fit its invocation model |

Adding a new platform means writing those two strings inside the new adapter — the Claude controller stays untouched.

### Session handling

| Scenario | Flag | Effect |
|----------|------|--------|
| New session | `--session-id {uuid}` | Creates a fresh Claude Code session |
| Existing session | `--resume {uuid}` | Continues from where the last message left off |

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
1. asyncio.create_subprocess_exec() — spawn claude process in its own process group
2. Read stdout line-by-line (with overall timeout)
3. Background task drains stderr (prevents pipe buffer deadlock)
4. On completion: collect return code + stderr
5. On timeout/cleanup: SIGTERM entire process group → wait 5s → SIGKILL entire group
```

### Process group cleanup

The subprocess is spawned with `start_new_session=True`, which places it in a dedicated process group. On cleanup, `os.killpg()` sends the signal to the **entire group** — the main `claude` process and all its children (language servers, subprocesses, etc.). This prevents orphan child processes from surviving after the bridge terminates a session.

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

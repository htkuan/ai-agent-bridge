# Codex Agent

The Codex agent integrates [OpenAI Codex CLI](https://developers.openai.com/codex/cli/reference) as the AI backend. It spawns `codex exec --json` subprocesses, parses the NDJSON event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/codex/`

Activated when `AGENT_BRIDGE_AGENT=codex`. The bridge, session manager, and platform adapters are unchanged — only the controller swaps in.

## How It Works

Each user message triggers a **one-shot subprocess**, modelled on the Claude agent:

```
codex exec [resume <thread_id>] --json \
  [--skip-git-repo-check] \
  --sandbox MODE --ask-for-approval MODE \
  -C <work_dir> [-m MODEL] [-c key=value ...] \
  -
```

The trailing `-` tells codex to read the prompt from stdin; the controller then writes the synthesized prompt and closes stdin.

## Why a thread map (and not just `--session-id`)?

Claude Code accepts an externally provided `--session-id`, so the bridge can mint a UUID up front and pass it through. **Codex does not** — it generates its own `thread_id` and reports it on the first event. The agent therefore keeps a small persistent mapping:

```
bridge_session_id (our UUID, owned by SessionManager)
    ↓
codex thread_id   (captured from `thread.started`, owned by ThreadMap)
```

| Scenario | Behaviour |
|----------|-----------|
| First call (`is_new=True`) | Run `codex exec` fresh, capture the `thread_id` from `thread.started`, store mapping. |
| Follow-up (`is_new=False`, mapping exists) | Run `codex exec resume <thread_id>` to continue the same codex thread. |
| Follow-up but mapping missing (purged / corrupted) | Fall back to a fresh codex thread, log a warning. |
| Bridge session expires (TTL) | Periodic cleanup calls `controller.cleanup_session(session_id)`, which drops the mapping entry. |

The map lives at `AGENT_BRIDGE_CODEX_THREAD_MAP_PATH` (default `./codex_threads.json`) and uses the same write-then-rollback pattern as `SessionManager`.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_CODEX_WORK_DIR` | `.` | Directory codex runs in; passed via `-C`. |
| `AGENT_BRIDGE_CODEX_SANDBOX` | `workspace-write` | One of `read-only`, `workspace-write`, `danger-full-access`. |
| `AGENT_BRIDGE_CODEX_APPROVAL` | `never` | One of `untrusted`, `on-request`, `never`. Headless deployments should keep `never`. |
| `AGENT_BRIDGE_CODEX_MODEL` | — | Optional override for `-m`. Empty = CLI default. |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | `600` | Wall-clock timeout per invocation. Process group is killed on expiry. |
| `AGENT_BRIDGE_CODEX_THREAD_MAP_PATH` | `./codex_threads.json` | Persistent mapping file. |
| `AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK` | `true` | Adds `--skip-git-repo-check` so codex runs outside git repos. |
| `AGENT_BRIDGE_CODEX_EXTRA_CONFIG` | — | Comma-separated `key=value` pairs forwarded as `-c key=value`. |

Validation runs in `CodexConfig._validate()` at startup — invalid sandbox/approval values raise `ValueError`.

## Prompt and system prompt: stdin-composed

Codex CLI has no `--append-system-prompt` equivalent. The platform-supplied `system_prompt` and `prompt` are joined into a single stdin payload with explicit tags:

```
<system>
{system_prompt}
</system>

<user>
{prompt}
</user>
```

This keeps per-call flexibility (every platform adapter can still send its own framing) without polluting the global `~/.codex/config.toml`. If `system_prompt` is empty the user prompt is sent on its own.

The controller never inspects `context` — it stays platform-agnostic, just like the Claude controller.

## Event Flow

### Codex `--json` envelope

Codex emits NDJSON events tagged with `type`:

```
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"...","type":"command_execution","command":"...","status":"in_progress"}}
{"type":"item.completed","item":{"id":"...","type":"command_execution","command":"...","status":"completed","exit_code":0}}
{"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
{"type":"turn.completed","usage":{"input_tokens":..., "output_tokens":..., ...}}
```

### Codex → BridgeEvent

| Codex event | BridgeEvent | Notes |
|-------------|-------------|-------|
| `thread.started` | *internal* | Capture `thread_id`, persist to ThreadMap. |
| `turn.started` | *internal* | Bridge already emitted `Processing()` when the slot was acquired. |
| `item.started` (`command_execution`) | `StatusUpdate("Running: <cmd>")` | First line of command, truncated to 80 chars. |
| `item.started` (`mcp_tool_call`) | `StatusUpdate("Using <server>.<tool>...")` | |
| `item.started` (`web_search`) | `StatusUpdate("Searching the web...")` | |
| `item.completed` (`agent_message`) | `TextDelta(text)` | Full message at completion (codex `--json` does not stream tokens). |
| `item.completed` (`todo_list`) | `StatusUpdate("Updated todo list")` | |
| `item.completed` (`file_change`) | `StatusUpdate("Edited files")` | |
| `item.*` (`reasoning`) | *internal* | Mirrors Claude's thinking blocks — never surfaced. |
| `turn.completed` | `Completion` | `text` = last `agent_message`; token usage stored in `metadata`. |
| `turn.failed` / `error` | `Completion(is_error=True)` | |

`cost_usd` is left at `0.0` because codex `--json` only reports token counts. Token usage is preserved in `Completion.metadata` so a future cost-table layer can attach pricing.

### Latency vs. Claude

Codex `--json` mainly emits each `agent_message` as a single `item.completed` rather than a token-by-token stream. Slack rendering still works (it accumulates `TextDelta`s), but first-character latency is higher than the Claude agent. If finer streaming is needed later, switch to also emitting `TextDelta` deltas from `item.updated` events that carry partial `text`.

## Process Management

Same model as the Claude controller:

- `asyncio.create_subprocess_exec(..., start_new_session=True)` — codex runs in its own process group.
- 10 MB stdout line buffer.
- stderr drained in a background task.
- Overall wall-clock deadline; on timeout the process group receives `SIGTERM` then `SIGKILL`.
- Non-zero exit code surfaces as an error `Completion` if the stream did not already produce one.

## Limitations

- **No worktree mode.** Claude has built-in `-w`; codex does not. Concurrent codex sessions share the same `work_dir`. Add a worktree layer here if isolation is required (mirror `ClaudeController.cleanup_session`).
- **No native cost.** `cost_usd=0.0`; usage tokens are in `Completion.metadata`.
- **No token-by-token streaming.** Each assistant message arrives whole at `item.completed`.
- **Authentication.** Either run `codex login` interactively once (stores credentials), or set the appropriate env var (`OPENAI_API_KEY`, etc.) per the Codex CLI docs. The bridge does not manage credentials.
- **No `AskUserQuestion` equivalent.** Codex has no first-class user-question tool; `UserQuestion` events are not emitted by this agent.

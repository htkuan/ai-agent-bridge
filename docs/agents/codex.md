# Codex Agent

The Codex agent integrates the OpenAI Codex CLI as an AI backend. It spawns
`codex exec --json` subprocesses, parses the JSONL event stream, and yields
generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/codex/`

Codex has **no env-built default controller**. It is reachable only through
named profiles (`[codex.profiles.<name>]` in the profiles file), routed to by
name like any other agent. The `AGENT_BRIDGE_CODEX_*` variables define the
base those profiles inherit from.

## How It Works

Each user message triggers a **one-shot subprocess**. New sessions use:

```bash
codex exec --json --sandbox <mode> \
  [-m <model>] \
  [-c 'model_reasoning_effort="<effort>"'] \
  [--skip-git-repo-check] -
```

Resume uses Codex's native thread id:

```bash
codex exec resume <thread_id> --json \
  -c 'sandbox_mode="<mode>"' \
  [-m <model>] \
  [-c 'model_reasoning_effort="<effort>"'] -
```

The **prompt is piped through stdin**, never argv. Codex uses the `-`
positional to read stdin, which keeps user text starting with `-` out of flag
parsing.

Codex has no system-prompt flag. When a platform provides `system_prompt`, the
controller prefixes it into stdin on every turn:

```xml
<system_directives>
...
</system_directives>

<prompt>
```

## Session Handles

The bridge mints UUID session ids, but Codex cannot resume from an externally
supplied id. Codex emits a native `thread_id` in `thread.started`; the
controller persists a map from bridge session id to Codex thread id.

By default the map lives at:

```text
<work_dir>/.agent-bridge/codex-sessions.json
```

Override it with `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` or
`session_map_path` in a profile when multiple profiles should share or isolate
Codex resume handles deliberately.

If a resume arrives and the map entry is missing (lost file, new host, manual
cleanup), the controller logs a warning and starts a fresh Codex session. The
next `thread.started` event records the new mapping.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_CODEX_WORK_DIR` | `.` | Working directory the agent operates in. Resolved to absolute at startup. |
| `AGENT_BRIDGE_CODEX_SANDBOX_MODE` | `workspace-write` | Codex sandbox: `read-only`, `workspace-write`, or `danger-full-access`. |
| `AGENT_BRIDGE_CODEX_MODEL` | — (codex's default) | Passed to `codex -m`. |
| `AGENT_BRIDGE_CODEX_EFFORT` | — (codex's default) | Passed as `-c model_reasoning_effort="..."`. Codex validates the value. |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process tree is killed on timeout. |
| `AGENT_BRIDGE_CODEX_CLI_PATH` | `codex` | Path to the Codex CLI executable. |
| `AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK` | `false` | Passes `--skip-git-repo-check` and skips the startup `.git` prerequisite. |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | derived | JSON map from bridge session id to Codex thread id. |

Named profiles set the same fields as TOML types in
`[codex.profiles.<name>]`; unset fields inherit the env-built base. See
`profiles.example.toml`.

## Permissions

Codex's sandbox is its permission model:

| Mode | Effect |
|------|--------|
| `read-only` | Read-only filesystem access. |
| `workspace-write` | Writes are constrained to the workspace. |
| `danger-full-access` | No sandbox. Treat this like a high-trust profile. |

Use separate named profiles for different Slack channels or webhook routes
when they need different sandbox behavior.

## Event Mapping

| codex event | BridgeEvent |
|-------------|-------------|
| `thread.started` | — (internal; records the Codex thread id handle) |
| `item.completed` (`agent_message`) | `TextDelta`; the last message becomes the final `Completion.text` |
| `item.started` (`command_execution`) | `StatusUpdate` (`Running a command...`) with command detail |
| `item.started` (`file_change`) | `StatusUpdate` (`Editing files...`) with changed path detail |
| `item.*` (`error`) | — (records/logs the message) |
| top-level `error` | — (records/logs the message) |
| `turn.completed` | Success `Completion` with mapped usage |
| `turn.failed` | Error `Completion` |
| `turn.started`, completed command/file items, unknown events | — (ignored) |

Codex reports no cost. `Completion.cost_usd` is `0.0`; token counts are mapped
into `Completion.metadata["usage"]` with cached input tokens split out as
`cache_read_tokens`, cache writes as `cache_creation_tokens`, and
`duration_api_ms = 0`.

## Failure Handling

| Scenario | Result |
|----------|--------|
| Process timeout | Error `Completion` with timeout message, process tree killed |
| `turn.failed` | Error `Completion` with Codex's error message |
| Top-level `error` before `turn.failed` | Message recorded/logged; `turn.failed` decides terminal failure |
| Process exited without `turn.completed` / `turn.failed` | Generic error `Completion` with exit code, stderr logged |
| Missing bridge-session to Codex-thread map on resume | Warning logged; starts a fresh Codex session and records the new thread id |
| Invalid JSON line | Warning logged, line skipped |

## Prerequisites

- `codex` CLI installed and authenticated with a ChatGPT account
- The work dir must exist
- Unless `AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK=true`, the work dir must be a
  git repository

The bridge probes only work dir and git prerequisites at startup. CLI auth,
model, provider, and account failures surface as error `Completion`s during
the agent turn.

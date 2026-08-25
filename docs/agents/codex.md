# Codex Agent

The Codex agent integrates the [OpenAI Codex CLI](https://github.com/openai/codex) as an AI backend (tested against codex-cli 0.147.0). It spawns `codex exec --json` subprocesses, parses the JSONL event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/codex/`

Codex has **no env-built default controller** — it is reachable only through
named profiles (`[codex.profiles.<name>]` in the profiles file), routed to by
name like any other agent. The `AGENT_BRIDGE_CODEX_*` variables define the
base those profiles inherit from.

## How It Works

Each user message triggers a **one-shot subprocess**. A new session runs:

```
codex exec --json --sandbox <mode> [-m MODEL] \
  [-c model_reasoning_effort="EFFORT"] [--skip-git-repo-check] -
```

A resumed session runs:

```
codex exec resume <thread_id> --json -c sandbox_mode="<mode>" \
  [-m MODEL] [-c model_reasoning_effort="EFFORT"] [--skip-git-repo-check] -
```

Note the quirk: the `resume` subcommand does **not** accept `--sandbox` — the
sandbox must go through a `-c sandbox_mode="…"` config override.
`--skip-git-repo-check`, however, is needed on **both** invocations: codex
re-runs the trusted-directory probe on resume too, so dropping the flag there
would strand every non-git work dir after its first turn.

The **prompt is piped through stdin** (the trailing `-` positional), never
argv, so user text starting with `-` can't parse as flags. Codex has no
system-prompt flag either: the platform's `system_prompt` is folded into the
stdin payload as a tagged block, on every turn:

```
<system_directives>
{system_prompt}
</system_directives>

{prompt}
```

## The Session Handle Map

The bridge mints its own session ids, but codex **cannot accept an external
session id** — `thread.started` carries the codex-minted `thread_id`, the only
resume handle. The controller therefore owns a persistent mapping
`bridge session_id → codex thread_id` (`SessionHandleStore`,
`src/agent_bridge/agents/handles.py`):

- Every `thread.started` upserts the mapping (idempotent — resume runs re-emit
  the same id).
- On resume, a **missing mapping degrades to a fresh session** with a warning
  instead of failing the turn; the new thread id is re-recorded immediately.
- Session expiry (`cleanup_session`) removes the entry.
- The file defaults to `<work_dir>/.agent-bridge/codex-sessions.json`
  (override with `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` / `session_map_path`).
  Profiles sharing a work_dir share the file; the store's read-merge-write
  mutations keep concurrent instances from losing each other's entries.
- A missing or corrupt store file starts empty with a warning — worst case,
  old threads are no longer resumable.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_CODEX_WORK_DIR` | `.` | Working directory the agent operates in. Resolved to absolute at startup. |
| `AGENT_BRIDGE_CODEX_SANDBOX_MODE` | `workspace-write` | One of `read-only`, `workspace-write`, `danger-full-access` (see [Permissions](#permissions)). |
| `AGENT_BRIDGE_CODEX_MODEL` | — (codex's default) | Passed to `codex -m`. Opaque — codex validates it. |
| `AGENT_BRIDGE_CODEX_EFFORT` | — (codex's default) | Passed as `-c model_reasoning_effort="…"`. Opaque — codex validates it. |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process tree is killed on timeout. |
| `AGENT_BRIDGE_CODEX_CLI_PATH` | `codex` | Path to the codex CLI executable. |
| `AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK` | `false` | Pass `--skip-git-repo-check`: codex refuses to run outside a git repository without it. |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | — (derived) | Where the session→thread map lives. Unset ⇒ `<work_dir>/.agent-bridge/codex-sessions.json`. |

Named profiles set the same fields (as TOML types — `skip_git_repo_check` is
a boolean) in `[codex.profiles.<name>]`; unset fields inherit the env-built
base. See `profiles.example.toml`.

## Permissions

The sandbox mode is codex's permission model:

| Mode | Behavior |
|------|----------|
| `read-only` | The agent can read but not write or run mutating commands |
| `workspace-write` | Writes confined to the workspace (the default) |
| `danger-full-access` | No sandbox — treat like a `dangerously-skip-permissions` Claude profile |

```toml
[codex.profiles.reviewer]
sandbox_mode = "read-only"
```

## Event Mapping

| codex event | BridgeEvent |
|-------------|-------------|
| `thread.started` | — (internal; the controller records session→thread here) |
| `item.completed` type `agent_message` | `TextDelta` (the **last** one is the final answer) |
| `item.started` type `command_execution` | `StatusUpdate` (`Running a command...` + the command) |
| `item.started` type `file_change` | `StatusUpdate` (`Editing files...` + comma-joined paths) |
| `item.*` type `error` | — (message recorded for the failure text) |
| `turn.completed` | `Completion` — success terminal, built from accumulated state |
| `turn.failed` | `Completion` — error terminal, with the error message |
| top-level `error` | — (message recorded; `turn.failed` makes the terminal call) |
| `turn.started`, `item.completed` for command/file items, unknown types | — (lifecycle noise; tolerant parser) |

Usage arrives on `turn.completed` and maps to the bridge's canonical keys:
`cached_input_tokens` is a subset of codex's `input_tokens` (so canonical
`input_tokens` = `input_tokens - cached_input_tokens`,
`cache_read_tokens` = `cached_input_tokens`), `cache_creation_tokens` =
`cache_write_input_tokens`, and `reasoning_output_tokens` stays inside
`output_tokens`. Codex reports **no cost** (`cost_usd` stays 0.0) and no API
duration (`duration_api_ms` stays 0).

## Failure Handling

| Scenario | Result |
|----------|--------|
| CLI could not be spawned (`cli_path` missing or not executable, `work_dir` gone) | Error `Completion` naming the OS error — the run ends before any process exists |
| Process timeout (no output) | Error `Completion` with timeout message, process tree killed |
| `turn.failed` | Error `Completion` with the error message |
| Process exited with no terminal event (e.g. unknown thread id on resume: exit 1, stderr `no rollout found…`, no JSON) | Error `Completion` with the exit code, stderr logged |
| Resume with no recorded thread id (lost/purged map) | Not an error: warning logged, fresh session started and re-recorded |
| Invalid JSON line | Warning logged, line skipped |

## Prerequisites

- `codex` CLI installed and logged in (ChatGPT account; `codex login`). The
  bridge probes only the work dir at startup — an auth failure surfaces as an
  error `Completion` on the first turn
- The work dir must exist, and must be a git repository unless
  `AGENT_BRIDGE_CODEX_SKIP_GIT_REPO_CHECK=true` (both checked at startup, per
  profile)

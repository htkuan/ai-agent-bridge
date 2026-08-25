# Opencode Agent

The Opencode agent integrates the [opencode CLI](https://opencode.ai) as an AI backend (tested against opencode 1.18.18). It spawns `opencode run --format json` subprocesses, parses the JSONL event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/opencode/`

Opencode has **no env-built default controller** — it is reachable only through
named profiles (`[opencode.profiles.<name>]` in the profiles file), routed to
by name like any other agent. The `AGENT_BRIDGE_OPENCODE_*` variables define
the base those profiles inherit from.

## How It Works

Each user message triggers a **one-shot subprocess**. A new session runs:

```
opencode run --format json --title bridge-<session_id> \
  [-m PROVIDER/MODEL] [--variant VARIANT]
```

A resumed session runs:

```
opencode run --format json -s <ses_id> [-m PROVIDER/MODEL] [--variant VARIANT]
```

The `--title` (new sessions only) is debuggability sugar: opencode's own
session lists show it.

The **prompt is piped through stdin**, never argv, so user text starting with
`-` can't parse as flags. Opencode has no system-prompt flag either: the
platform's `system_prompt` is folded into the stdin payload as a tagged block,
on every turn:

```
<system_directives>
{system_prompt}
</system_directives>

{prompt}
```

One environment quirk (found in live testing): opencode resolves its project
directory from the **`$PWD` environment variable**, not the process's actual
working directory. The shared engine pins `$PWD` to the work dir on every
spawn — spawning with `cwd=` alone would leave the bridge's own `$PWD`
visible and opencode would silently run in the wrong project.

## The Session Handle Map

The bridge mints its own session ids, but opencode **cannot accept an external
session id** — every stream event carries the opencode-minted `sessionID`
(`ses_…`), the only resume handle. The controller therefore owns a persistent
mapping `bridge session_id → opencode session id` (`SessionHandleStore`,
`src/agent_bridge/agents/handles.py`):

- The id is captured from the first stream event that carries it and upserted
  (idempotent — resume runs re-announce the same id).
- On resume, a **missing mapping degrades to a fresh session** with a warning
  instead of failing the turn; the new session id is re-recorded immediately.
- Session expiry (`cleanup_session`) removes the entry. Opencode's own session
  storage is outside the bridge's scope — sessions accumulate there and are
  managed with opencode's own tooling.
- The file defaults to `<work_dir>/.agent-bridge/opencode-sessions.json`
  (override with `AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH` /
  `session_map_path`). Profiles sharing a work_dir share the file; the store's
  read-merge-write mutations keep concurrent instances from losing each
  other's entries.
- A missing or corrupt store file starts empty with a warning — worst case,
  old sessions are no longer resumable.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_OPENCODE_WORK_DIR` | `.` | Working directory the agent operates in. Resolved to absolute at startup. |
| `AGENT_BRIDGE_OPENCODE_MODEL` | — (opencode's default) | Passed to `opencode -m` as a `provider/model` string. Opaque — opencode validates it. |
| `AGENT_BRIDGE_OPENCODE_VARIANT` | — (opencode's default) | Passed to `--variant` (provider-specific effort/variant). Opaque — opencode validates it. |
| `AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process tree is killed on timeout. |
| `AGENT_BRIDGE_OPENCODE_CLI_PATH` | `opencode` | Path to the opencode CLI executable. |
| `AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH` | — (derived) | Where the session→session map lives. Unset ⇒ `<work_dir>/.agent-bridge/opencode-sessions.json`. |

Named profiles set the same fields in `[opencode.profiles.<name>]`; unset
fields inherit the env-built base. See `profiles.example.toml`.

## Permissions

**An opencode profile is effectively full-access.** `opencode run` executes
file edits and commands without prompting and has no sandbox flags — treat it
like a `dangerously-skip-permissions` Claude profile. Restriction lives in the
user's own `opencode.json` permission config, outside the bridge's scope:
point `work_dir` only at directories the agent may freely modify.

## Event Mapping

Every stream line carries a top-level `sessionID` (recorded for the handle
map) plus a `part` payload:

| opencode event | BridgeEvent |
|----------------|-------------|
| `text` | `TextDelta` (the **last** one is the final answer) |
| `tool_use` | `StatusUpdate` (`Using {tool}...` — tool names are lowercase: read, glob, bash, …) |
| `step_finish` | — (usage/cost accumulated; see below) |
| top-level `error` | — (message recorded for the failure text) |
| `step_start`, unknown types | — (lifecycle noise; tolerant parser) |

**There is no terminal event** — the stream simply ends at EOF. The
controller's `on_stream_end` synthesizes the final `Completion`:

| Exit | Result |
|------|--------|
| `0` with a `text` event seen | Success `Completion` from the accumulated state (last text, summed usage/cost) |
| `error` event recorded, no reply (any exit code — the CLI has been seen exiting 0 mid-run) | Error `Completion` with that message |
| `0` with nothing at all | Error `Completion` (`stream ended without a reply`) — a real turn always ends in a `text` event |
| non-zero, nothing recorded | The engine's generic exit-code error (e.g. `Error: Session not found` prints to stderr with no JSON at all) |

Usage arrives per step on `step_finish` and is **summed across steps** into
the bridge's canonical keys: `tokens.input` (already excludes cache) →
`input_tokens`, `tokens.output` → `output_tokens`, `tokens.cache.read` →
`cache_read_tokens`, `tokens.cache.write` → `cache_creation_tokens`, `cost` →
`cost_usd`, one `num_turns` per step. `duration_api_ms` is not reported and
stays 0.

## Failure Handling

| Scenario | Result |
|----------|--------|
| CLI could not be spawned (`cli_path` missing or not executable, `work_dir` gone) | Error `Completion` naming the OS error — the run ends before any process exists |
| Process timeout (no output) | Error `Completion` with timeout message, process tree killed (opencode spawns an internal server child per run; the group kill reaps it) |
| `error` event + non-zero exit | Error `Completion` with the error message |
| Non-zero exit with no JSON (e.g. unknown session id on resume: exit 1, stderr `Error: Session not found`) | Error `Completion` with the exit code, stderr logged |
| Resume with no recorded session id (lost/purged map) | Not an error: warning logged, fresh session started and re-recorded |
| Invalid JSON line | Warning logged, line skipped |

## Prerequisites

- `opencode` CLI installed and authenticated (`opencode auth login`). The
  bridge probes only the work dir at startup — an auth failure surfaces as an
  error `Completion` on the first turn
- The work dir must exist (checked at startup, per profile). Opencode has no
  git-repository requirement

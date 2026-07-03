# Codex Agent

The Codex agent integrates the [OpenAI Codex CLI](https://developers.openai.com/codex) as the AI backend. It spawns `codex exec --json` subprocesses, parses the JSONL event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/codex/`

Select it with `agent: codex` (YAML) or `AGENT_BRIDGE_AGENT=codex`.

## Prerequisites

- Codex CLI installed: `npm install -g @openai/codex` (or `brew install --cask codex`)
- Authenticated: run `codex login` (ChatGPT account) or `codex login --with-api-key` (OpenAI API key) once on the host — the bridge spawns the CLI under the same user and reuses its credentials

## Verified CLI interface

The implementation targets **`@openai/codex` 0.130.0** and was verified against:

- the official non-interactive reference (`developers.openai.com/codex/noninteractive`)
- the official TypeScript SDK type definitions (`github.com/openai/codex`, `sdk/typescript` — `ThreadEvent` / `ThreadItem` unions), which parse the same `codex exec --json` stream

Assumed interface (re-verify with `codex exec --help` when bumping the CLI):

- `codex exec [flags] "<prompt>"` — new run; `codex exec resume <thread_id> [flags] "<prompt>"` — continue a thread
- `--json` — JSONL events on stdout
- `--sandbox {read-only|workspace-write|danger-full-access}`
- `-m/--model <name>` — optional model override
- `--skip-git-repo-check` — allow running outside a git repo

The parser is deliberately tolerant: **unknown event types and unknown item types are logged and skipped**, so newer CLI versions that add event types degrade gracefully instead of breaking the bridge.

## How It Works

Each user message triggers a **one-shot subprocess** (same lifecycle philosophy as the [Claude agent](claude.md#why-one-shot-not-long-running)):

```
# New session
codex exec --json --sandbox <mode> --skip-git-repo-check [-m <model>] "<prompt>"

# Follow-up in the same session
codex exec resume <thread_id> --json --sandbox <mode> --skip-git-repo-check [-m <model>] "<prompt>"
```

The process runs with `cwd = work_dir`, streams JSONL events on stdout, and exits.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_CODEX_WORK_DIR` | `.` | Working directory where Codex operates. Resolved to absolute at startup; must exist. |
| `AGENT_BRIDGE_CODEX_MODEL` | — (CLI default) | Optional model passed as `-m <model>`. |
| `AGENT_BRIDGE_CODEX_SANDBOX` | `workspace-write` | Codex sandbox mode: `read-only`, `workspace-write`, or `danger-full-access`. Validated at startup. |
| `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process group is terminated on timeout. |
| `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | `./codex-sessions.json` | File persisting the bridge-session → codex-thread mapping. |

YAML keys: `agents.codex.{work_dir,model,sandbox,timeout_seconds,session_map_path}` — see [docs/configuration.md](../configuration.md).

## Session Mapping

Unlike Claude Code, the Codex CLI does **not** accept an externally supplied session id — it mints its own thread id and reports it in the `thread.started` event. The controller therefore keeps a persisted mapping:

```json
// codex-sessions.json
{
  "<bridge_session_id>": "<codex_thread_id>"
}
```

| Scenario | Behavior |
|----------|----------|
| New session (`is_new=True`) | Run `codex exec`, capture `thread_id` from `thread.started`, persist the mapping |
| Follow-up (`is_new=False`) | Look up the thread id, run `codex exec resume <thread_id>` |
| Mapping missing on follow-up (lost file, restart before first write) | Log a warning and **fall back to a fresh thread** — conversation context is lost, but the message is still served (no error `Completion`); the new thread id is written back to the map |
| Session expires (TTL) | `cleanup_session` removes the entry from the map file; never raises |
| Corrupt / non-object map file | Warning logged, controller starts with an empty map |
| Map write failure | Error logged, run continues — worst case the next restart falls back to a fresh thread |

## System Prompt Handling

`codex exec` has **no stable system-prompt flag** (config overrides for instructions are experimental). Following the bridge-wide contract that agents forward platform text as-is, the controller prepends the platform's `system_prompt` to the prompt with an explicit delimiter:

```
<platform-directives>
{system_prompt}
</platform-directives>

{prompt}
```

When `system_prompt` is empty/None, the prompt is passed verbatim.

## Event Flow

### Codex JSONL → BridgeEvent

| Codex event | BridgeEvent | Notes |
|-------------|-------------|-------|
| `thread.started` | *filtered* | Thread id captured for the session map |
| `turn.started` | *filtered* | Internal |
| `item.started` (`command_execution`) | `StatusUpdate` | `"Running command..."` + truncated command as detail |
| `item.started` (`mcp_tool_call`) | `StatusUpdate` | `"Using {server}.{tool}..."` |
| `item.started` (`web_search`) | `StatusUpdate` | `"Searching the web..."` + query |
| `item.started` (`file_change`) | `StatusUpdate` | `"Applying file changes..."` + paths |
| `item.completed` (`agent_message`) | `TextDelta` | Full message text (deltas via `item.updated` are ignored to avoid duplication) |
| `item.*` (`reasoning`, `todo_list`, other phases) | *filtered* | Internal |
| `turn.completed` | `Completion` | Terminal — success, carries usage |
| `turn.failed` | `Completion` (error) | Terminal — `error.message` as text |
| `error` | `Completion` (error) | Terminal — fatal stream error |

The read loop stops at the first terminal event (`turn.completed` / `turn.failed` / `error`) instead of waiting for EOF — same orphan-grandchild rationale as the [Claude agent](claude.md#stream-termination-break-on-result-not-eof).

`turn.completed` carries **only usage** — no final text. The controller fills `Completion.text` with the last `agent_message` (which is also what platforms saw as the final `TextDelta`).

Codex has no `AskUserQuestion` equivalent in exec mode, so this agent never yields `UserQuestion`.

### Usage extraction

Codex reports OpenAI-style usage on `turn.completed`:

```json
{"type":"turn.completed","usage":{"input_tokens":1200,"cached_input_tokens":1000,"output_tokens":300,"reasoning_output_tokens":100}}
```

`input_tokens` **includes** the cached prefix (`cached_input_tokens` is a subset), while the bridge's canonical keys expect input/output to *exclude* cache. Mapping:

| Codex `usage` field | Canonical key in `metadata["usage"]` |
|---------------------|--------------------------------------|
| `input_tokens - cached_input_tokens` (clamped ≥ 0) | `input_tokens` |
| `output_tokens` | `output_tokens` (includes reasoning tokens) |
| `cached_input_tokens` | `cache_read_tokens` |
| — (no such concept) | `cache_creation_tokens` = `0` |
| — (exec runs one turn) | `num_turns` = `1` |

The CLI reports **no cost**, so `Completion.cost_usd` stays `0` — a Slack usage-report template showing `{cost_usd}` will render `$0.0000` for Codex sessions. `duration_ms` is measured wall-clock by the controller (the CLI reports no durations either).

## Process Management

Identical to the [Claude agent's subprocess pattern](claude.md#process-management): `start_new_session=True` process group isolation, 10 MB stdout line buffer, background stderr drain, overall wall-clock timeout, up-front group SIGTERM → 5s → SIGKILL cleanup, and suppression of non-zero exit codes once a terminal event was streamed.

### Error cases

| Scenario | Result |
|----------|--------|
| Process timeout | Error `Completion` with timeout message, process group killed |
| Non-zero exit *before* a terminal event | Error `Completion` with exit code, stderr logged |
| Non-zero exit *after* a terminal event | Suppressed — the turn finished; the exit is our own teardown |
| `turn.failed` / `error` event | Single error `Completion` with the reported message |
| Invalid JSON line / unknown event type | Logged, line skipped |

## Limitations

- **No cost reporting** — the CLI does not expose spend; `cost_usd` is always 0.
- **No streaming text deltas** — text arrives per completed `agent_message`, so long answers appear in one chunk (fine for the current platforms, which buffer `TextDelta`s anyway).
- **System prompt is prompt-prepended**, not a true system message — the model sees it inside the user turn (see above).
- **Resume continuity depends on the session map file** — losing it degrades to fresh threads (served, but without memory). Codex also persists its own rollout files under `$CODEX_HOME`; deleting those breaks `resume` on the Codex side.
- `--skip-git-repo-check` is always passed: the bridge `work_dir` is not necessarily a git repo, and the sandbox mode is the relevant guardrail in this deployment shape.

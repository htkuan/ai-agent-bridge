# OpenCode Agent

The OpenCode agent integrates the [OpenCode CLI](https://opencode.ai) as the AI backend. It spawns `opencode run --format json` subprocesses, parses the JSONL event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/opencode/`

Select it with `agent: opencode` (YAML) or `AGENT_BRIDGE_AGENT=opencode`.

## Prerequisites

- OpenCode CLI installed: `npm install -g opencode-ai` (or `brew install anomalyco/tap/opencode`, or the install script from opencode.ai)
- A provider authenticated: run `opencode auth login` once on the host — the bridge spawns the CLI under the same user and reuses its credentials
- If you use a non-default model, remember OpenCode's model syntax is `provider/model` (e.g. `anthropic/claude-sonnet-4-5`)

## Verified CLI interface

The implementation targets **`opencode-ai` 1.17.x** and was verified against:

- the official CLI reference (`opencode.ai/docs/cli`)
- the `run` command source (`github.com/anomalyco/opencode`, `packages/opencode/src/cli/cmd/run.ts`), which is where the `--format json` events are emitted

Assumed interface (re-verify with `opencode run --help` when bumping the CLI):

- `opencode run [flags] "<message>"` — message is positional
- `--format json` — JSONL events on stdout (`default` is human-formatted text)
- `-s/--session <id>` — continue an **existing** session; the CLI exits 1 with "Session not found" for unknown ids (it never creates a session with a caller-chosen id)
- `-m/--model <provider/model>` — optional model override

The parser is deliberately tolerant: **unknown event types are logged and skipped**, so newer CLI versions that add event types degrade gracefully instead of breaking the bridge.

## How It Works

Each user message triggers a **one-shot subprocess** (same lifecycle philosophy as the [Claude agent](claude.md#why-one-shot-not-long-running)):

```
# New session
opencode run --format json [--model <provider/model>] "<prompt>"

# Follow-up in the same session
opencode run --format json --session <ses_id> [--model <provider/model>] "<prompt>"
```

The process runs with `cwd = work_dir` (OpenCode uses the working directory as its project root), streams JSONL events on stdout, and exits when the session goes idle.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_OPENCODE_WORK_DIR` | `.` | Working directory where OpenCode operates. Resolved to absolute at startup; must exist. |
| `AGENT_BRIDGE_OPENCODE_MODEL` | — (CLI default) | Optional model passed as `--model`. Must use the `provider/model` form; validated at startup. |
| `AGENT_BRIDGE_OPENCODE_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process group is terminated on timeout. |
| `AGENT_BRIDGE_OPENCODE_SESSION_MAP_PATH` | `./opencode-sessions.json` | File persisting the bridge-session → opencode-session mapping. |

YAML keys: `agents.opencode.{work_dir,model,timeout_seconds,session_map_path}` — see [docs/configuration.md](../configuration.md).

## Session Mapping

Like Codex — and unlike Claude Code — the OpenCode CLI does **not** accept an externally supplied session id for a *new* session: `--session` only continues an existing one, and ids (`ses_...`) are minted by OpenCode itself. Every JSON event carries the session id at the top level (`sessionID`), so the controller captures it from the first event and keeps a persisted mapping:

```json
// opencode-sessions.json
{
  "<bridge_session_id>": "<opencode_session_id>"
}
```

| Scenario | Behavior |
|----------|----------|
| New session (`is_new=True`) | Run `opencode run`, capture `sessionID` from the first event, persist the mapping |
| Follow-up (`is_new=False`) | Look up the session id, run `opencode run --session <ses_id>` |
| Mapping missing on follow-up (lost file, restart before first write) | Log a warning and **fall back to a fresh session** — conversation context is lost, but the message is still served (no error `Completion`); the new session id is written back to the map |
| Mapping present but stale (OpenCode's own storage cleared) | The CLI prints "Session not found" and exits 1 without emitting events → error `Completion` with the exit code, stderr logged |
| Session expires (TTL) | `cleanup_session` removes the entry from the map file; never raises |
| Corrupt / non-object map file | Warning logged, controller starts with an empty map |
| Map write failure | Error logged, run continues — worst case the next restart falls back to a fresh session |

## System Prompt Handling

`opencode run` has **no system-prompt flag** (`--agent` selects a pre-configured OpenCode agent, which is not a pass-through). Same solution as [Codex](codex.md#system-prompt-handling): the controller prepends the platform's `system_prompt` to the prompt with an explicit delimiter:

```
<platform-directives>
{system_prompt}
</platform-directives>

{prompt}
```

When `system_prompt` is empty/None, the prompt is passed verbatim.

## Event Flow

### OpenCode JSONL → BridgeEvent

Every line is `{"type", "timestamp", "sessionID", ...}`. A "step" is one LLM call — a run that uses tools spans several steps, so `step_start`/`step_finish` occur **multiple times per run**.

| OpenCode event | BridgeEvent | Notes |
|----------------|-------------|-------|
| `step_start` | *filtered* | Marks a step boundary (used for final-text selection) |
| `text` | `TextDelta` | A completed text part (`part.text`); empty/whitespace-only parts skipped |
| `tool_use` | `StatusUpdate` | Retrospective — the CLI emits it only once a tool call has `completed` (`"Ran {tool}"`) or `error`ed (`"Tool {tool} failed"`), with `part.state.title` as detail |
| `step_finish` | *filtered* | Usage (`part.tokens`) and cost (`part.cost`) accumulated into the final `Completion` |
| `reasoning` | *filtered* | Only emitted with `--thinking`, which the bridge never passes |
| `error` | `Completion` (error) | Terminal — the session aborts and the CLI exits 1; message from `error.data.message` |
| *(unknown type)* | *filtered* | Logged and skipped |

### Stream termination: EOF, not a terminal event

Unlike Claude (`result`) and Codex (`turn.completed`), **`opencode run` emits no success terminal event** — the process simply exits once the session goes idle. The controller therefore reads to stdout EOF and *synthesizes* the final `Completion` itself:

- `Completion.text` = the text parts of the **last text-producing step**, joined by a blank line (intermediate narration from earlier steps was already streamed as `TextDelta`s but is not the final answer)
- usage/cost = sums across all `step_finish` events (see below)
- `duration_ms` = controller wall-clock (the CLI reports no durations)
- A clean exit (code 0) at EOF → success `Completion`; a non-zero exit without a prior `error` event → error `Completion` with the exit code

An `error` event short-circuits the read loop (the run is aborted CLI-side), and the resulting non-zero exit code is suppressed so only one error `Completion` is yielded.

OpenCode has no user-question mechanism in non-interactive mode, so this agent never yields `UserQuestion`.

### Usage extraction

Each `step_finish` reports the step's cost and tokens:

```json
{"type":"step_finish","sessionID":"ses_…","part":{"type":"step-finish","reason":"stop","cost":0.001,
  "tokens":{"input":671,"output":8,"reasoning":0,"cache":{"read":21415,"write":0}}}}
```

`tokens.input` already **excludes** the cached prefix (cache counts are separate), so values map directly onto the bridge's canonical keys, summed across all steps of the run:

| OpenCode `part.tokens` field (Σ steps) | Canonical key in `metadata["usage"]` |
|----------------------------------------|--------------------------------------|
| `input` | `input_tokens` |
| `output` | `output_tokens` |
| `cache.read` | `cache_read_tokens` |
| `cache.write` | `cache_creation_tokens` |
| — (count of `step_finish` events) | `num_turns` |

`tokens.reasoning` has no canonical key and is ignored (it is already reflected in OpenCode's own `cost`). `Σ part.cost` becomes `Completion.cost_usd` — unlike Codex, OpenCode **does** report spend.

## Process Management

Identical to the [Claude agent's subprocess pattern](claude.md#process-management): `start_new_session=True` process group isolation, 10 MB stdout line buffer, background stderr drain, overall wall-clock timeout, up-front group SIGTERM → 5s → SIGKILL cleanup, and suppression of non-zero exit codes once an `error` event was streamed.

### Error cases

| Scenario | Result |
|----------|--------|
| Process timeout | Error `Completion` with timeout message, process group killed |
| Non-zero exit without an `error` event (e.g. stale `--session` id) | Error `Completion` with exit code, stderr logged |
| `error` event | Single error `Completion` with the reported message (subsequent exit code 1 suppressed) |
| Clean exit, no events at all | Empty success `Completion` (no usage attached) |
| Invalid JSON line / unknown event type | Logged, line skipped |

## Permissions

In non-interactive mode (`opencode run`), OpenCode **auto-rejects** any permission request that its config marks as "ask" (the CLI's `--auto` auto-approve flag is not passed by the bridge). With OpenCode's default configuration nothing is gated, so this rarely matters — but if you have `permission` rules in your `opencode.json`, configure them to `allow` for the actions the bridge agent needs.

## Limitations

- **No streaming deltas** — `text` events carry completed parts, so long answers appear in coarse chunks (fine for the current platforms, which buffer `TextDelta`s anyway).
- **System prompt is prompt-prepended**, not a true system message — the model sees it inside the user turn (see above).
- **Resume continuity depends on the session map file** — losing it degrades to fresh sessions (served, but without memory). OpenCode also persists its own session storage; clearing it makes mapped ids stale (error `Completion`, see table above).
- **Permission "ask" rules are auto-rejected** in non-interactive mode (see above).
- `opencode run` spawns a local OpenCode server internally on a random port; the process-group cleanup tears it down with the run.

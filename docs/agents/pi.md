# Pi Agent

The Pi agent integrates the [pi coding agent](https://github.com/earendil-works/pi) as an AI backend. It spawns `pi -p --mode json` subprocesses, parses the JSON event stream, and yields generic `BridgeEvent`s.

Source: `src/agent_bridge/agents/pi/`

Pi has **no env-built default controller** — it is reachable only through named
profiles (`[pi.profiles.<name>]` in the profiles file), routed to by name like
any other agent. The `AGENT_BRIDGE_PI_*` variables define the base those
profiles inherit from.

## How It Works

Each user message triggers a **one-shot subprocess**:

```
pi -p --mode json --session-id <ID> \
  [--provider NAME] \
  [--model ID] \
  [--thinking LEVEL] \
  [--tools a,b] [--exclude-tools c] \
  [--append-system-prompt "<context>"]
```

The **prompt is piped through stdin**, never argv: pi takes the message as a
positional argument, where user text starting with `-` would parse as flags.
Print mode reads piped stdin as the message, so stdin delivery sidesteps the
whole class of flag-injection problems.

Session continuity is a single flag: `--session-id` **creates the session when
the id is unknown and resumes it when it exists**. A resume whose session file
was lost (pruned storage, new host) degrades to a fresh session with a stderr
warning instead of failing the turn — strictly more graceful than an error.
Session files live under `~/.pi/agent/sessions/`, keyed by working directory,
with the bridge's session id embedded in the filename.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BRIDGE_PI_WORK_DIR` | `.` | Working directory the agent operates in. Resolved to absolute at startup. |
| `AGENT_BRIDGE_PI_PROVIDER` | — (pi's default) | Passed to `pi --provider`. |
| `AGENT_BRIDGE_PI_MODEL` | — (pi's default) | Passed to `pi --model`. |
| `AGENT_BRIDGE_PI_THINKING` | — (pi's default) | Thinking level: one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `AGENT_BRIDGE_PI_TIMEOUT_SECONDS` | `600` | Maximum time (seconds) for a single invocation. Process tree is killed on timeout. |
| `AGENT_BRIDGE_PI_CLI_PATH` | `pi` | Path to the pi CLI executable. |
| `AGENT_BRIDGE_PI_TOOLS` | — (all tools) | Comma-separated tool allowlist (see [Permissions](#permissions)). |
| `AGENT_BRIDGE_PI_EXCLUDE_TOOLS` | — | Comma-separated tool denylist. |

Named profiles set the same fields (as TOML types — `tools` is a string
array) in `[pi.profiles.<name>]`; unset fields inherit the env-built base.
See `profiles.example.toml`.

## Permissions

Pi has **no sandbox and no permission prompts** in print mode — `bash`,
`edit`, and `write` run unrestricted. The tool allowlist/denylist is its
permission model:

```toml
[pi.profiles.readonly]
tools = ["read", "grep", "find", "ls"]
```

Built-in tools: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`.
Treat a profile without a `tools` restriction like a
`dangerously-skip-permissions` Claude profile.

Pi also loads `AGENTS.md` / `CLAUDE.md` context files from the work dir by
default, matching how the Claude agent behaves in the same directory.

## Event Mapping

| pi event | BridgeEvent |
|----------|-------------|
| `session` header | — (internal; the id echoes the bridge's session id) |
| `message_end` (assistant, with text) | `TextDelta` (also accumulates usage/cost) |
| `message_end` (assistant, tool-call only) | — (usage accumulated silently) |
| `message_end` (user / toolResult) | — (internal) |
| `tool_execution_start` | `StatusUpdate` (`Using {tool}...`) |
| `turn_end` | — (counts `num_turns`) |
| `agent_end` (`willRetry: true`) | — (pi retries; keep reading) |
| `agent_end` | `Completion` — synthesized from accumulated state |
| `message_start` / `message_update` / lifecycle events | — (deltas and noise; `message_end` is authoritative) |

Pi's stream has **no terminal result payload** — unlike Claude's `result`
line, `agent_end` is bare. The `Completion` is therefore assembled by the
parser state: `text` is the last assistant text, `cost_usd` and the token
counts sum over every assistant message (pi reports usage per message,
tokens and cost included), and `num_turns` counts `turn_end` events.
`duration_api_ms` is not reported by pi and stays 0.

## Failure Handling

| Scenario | Result |
|----------|--------|
| CLI could not be spawned (`cli_path` missing or not executable, `work_dir` gone) | Error `Completion` naming the OS error — the run ends before any process exists |
| Process timeout (no output) | Error `Completion` with timeout message, process tree killed |
| Process exited without `agent_end` (bad model, provider auth failure, crash) | Error `Completion` with the exit code, stderr logged |
| `agent_end` with `willRetry: true` | Not terminal — the stream continues into the retry |
| Unknown session id on resume | Not an error: pi creates a fresh session (stderr warning) |
| Invalid JSON line | Warning logged, line skipped |

## Prerequisites

- `pi` CLI installed and its provider authenticated (`pi auth`); the bridge
  probes only the work dir at startup — a broken provider surfaces as an
  error `Completion` on the first turn
- The work dir must exist (checked at startup, per profile)

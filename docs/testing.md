# Test Framework

How the test suite is organised, what the shared fakes are, and the rules
that keep interface-driven testing honest. Agreed design (2026-08),
implemented across the `tests/` tree.

## Principles

1. **The layout mirrors `src/`.** Every test lives in the directory matching
   the layer it exercises. Finding the tests for a module means swapping
   `src/agent_bridge/` for `tests/`.
2. **Test against the defined interfaces.** Components are driven through
   the protocols in `src/agent_bridge/bridge/protocols.py` (`AgentController`,
   `MessageRouter`, `PlatformAdapter`) and the platform's public entry
   points — not by poking internals. Unit tests of private helpers are fine
   *within* a layer; crossing a layer boundary always goes through the
   protocol.
3. **Fakes are hand-written, typed, and implement the protocol.** No
   `MagicMock` for protocol seams: a mock accepts any misspelled method and
   silently drifts from the real signature. The fakes in `tests/fakes/` are
   checked by pyright (strict) against the protocols, and record their calls
   so tests can assert on them.
4. **Contract suites pin fakes to reality.** For each protocol with both a
   real and a fake implementation, `tests/contracts/` runs the *same*
   assertions against both. If the real `Bridge` changes behaviour, the
   contract fails for the fake until it is updated — behavioural drift is
   caught, not discovered in production.
5. **Contract suites also pin the ports.** The bridge's storage/strategy
   ports (`SessionStore`, `DedupeCache`, `CapacityLimiter`) each have a
   contract suite; every implementation — built-in, fake, or a future one
   (RDBMS store, Redis cache) — joins by adding a fixture param and runs the
   same spec.

## Layout

```
tests/
├── conftest.py              # shared fixtures: fake_claude factory, session_manager
├── support.py               # tiny shared helpers (wait_until)
├── fakes/                   # typed doubles for the protocol seams (pyright strict)
│   ├── agents.py            # FakeAgentController — scripted event replay, records calls
│   ├── bridge.py            # FakeBridge — MessageRouter: capacity_full / unknown agent,
│   │                        #   plus gate (park a turn in flight) and raises (blow up)
│   ├── events.py            # ALL_EVENTS / CUT_STREAM — the canonical render stimulus
│   ├── platforms.py         # FakePlatformAdapter — lifecycle + cleanup recorder
│   ├── slack.py             # FakeSlackClient / FakeBoltApp / event payload builders
│   ├── claude_cli.py        # scripted claude CLI stand-in + the shared step runner
│   └── {pi,codex,opencode}_cli.py # the same runner, each CLI's own line builders
├── contracts/               # real implementation and its fake run the same suite
│   ├── test_agent_controller.py
│   ├── test_message_router.py
│   ├── test_platform_adapter.py
│   ├── test_session_store.py    # SessionStore port: JsonSessionStore + InMemorySessionStore
│   ├── test_dedupe_cache.py     # DedupeCache port: claim lifecycle spec
│   └── test_capacity_limiter.py # CapacityLimiter port: lease semantics spec
├── test_env.py              # the typed env readers
├── test_config.py           # AppConfig — the aggregate app.py builds from
├── app/                     # app.py wiring + lifecycle
├── bridge/                  # core layer: test_router.py = bridge input→output spec,
│   │                        # test_pipeline.py = compose/core, session/dedupe/stores/config
│   └── middleware/          # each pipeline stage in isolation vs scripted downstreams
├── agents/                  # test_base.py: the CliAgentController engine itself
├── agents/{claude,pi,codex,opencode}/ # config + controller + stream parser, per agent
├── platforms/               # test_base.py: BasePlatformAdapter's shared dispatch flow
│                            # harness.py: the PlatformHarness shape every platform implements
├── platforms/slack/         # adapter behaviour, one concern per file
├── platforms/heartbeat/
└── e2e/                     # full-stack scenarios (real components + fake CLI)
    ├── stack.py             # the rigs: Slack/webhook adapter → Bridge → controller
    ├── test_live_platforms.py   # platform over its REAL transport, FakeBridge behind it
    ├── conftest.py          # live_* fixtures: real claude/pi/codex/opencode CLIs
    ├── live_matrix.py       # the declarative live spec: FLAG_SPECS + LIVE_MATRIX
    ├── test_live_matrix_spec.py # the matrix's own invariants (CI, no CLI)
    ├── test_live_controllers.py # bare controller x every agent (opt-in, --live)
    ├── test_live_claude.py  # live Slack-rig scenarios (opt-in, --live)
    └── test_live_webhook.py # live webhook scenarios, claude + pi (opt-in, --live)
```

## The seams and their doubles

| Boundary | Interface | Double |
|----------|-----------|--------|
| wiring → adapter | `PlatformAdapter` protocol | `FakePlatformAdapter` |
| adapter → bridge | `MessageRouter` protocol | `FakeBridge` |
| bridge → agent | `AgentController` protocol | `FakeAgentController` |
| controller → claude CLI | argv + stream-json contract (parsed by `agents/claude/events.py`) | `tests/fakes/claude_cli.py` via `ClaudeConfig.cli_path` |
| adapter → Slack Web API | the method subset the adapter calls | `FakeSlackClient` (records calls, mints `ts`, tracks visible message state, injectable `SlackApiError`) |
| adapter → bolt `AsyncApp` | `@app.event(...)` registration | `FakeBoltApp` (captures handlers for direct invocation) |
| session policy → storage | `SessionStore` port | `InMemorySessionStore` |

Real, cheap components are used directly instead of doubled: `SessionManager`
(against `tmp_path`), `PromptDedupeCache`, and the event dataclasses. The
pipeline stage tests keep small scripted doubles (recording dedupe cache,
recording limiter/lease) local to their test modules — they instrument one
stage's obligations, not a cross-layer seam.

## Configuration in tests

Tests never set environment variables to change behaviour — they construct the
component's config object and pass it in. Validation runs on construction
(`__post_init__`), so a config built in a test is checked exactly like one read
from the environment.

Env parsing is covered on its own, per config class, in the `test_config.py`
modules. Those call `from_env({...})` with an explicit mapping, which keeps them
hermetic — the process environment and any local `.env` are out of the picture:

```python
config = ClaudeConfig.from_env({"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path)})
```

Each config class also carries a `from_env({}) == Config()` test, so an env
default can't drift away from its dataclass default.

## The fake claude CLI

`ClaudeController` is tested against a real subprocess — the scripted CLI in
`tests/fakes/claude_cli.py` — so the stream-reading, timeout, and
process-group-kill paths run for real. The `fake_claude` fixture materialises
a scenario and returns a ready `ClaudeConfig` (its `cli_path` points at an
executable wrapper):

```python
from tests.fakes import claude_cli


async def test_reply(fake_claude):
    cli = fake_claude(claude_cli.reply_steps("hello"))
    controller = ClaudeController(cli.config)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    assert cli.invocations()[0]  # argv of the spawned run, for flag assertions
```

Scenario steps cover the failure modes the controller must survive: error
results, non-zero exits, malformed JSON lines, mid-stream cuts (`exit` before
`result`), hangs (`sleep`, for timeout-kill tests), and stderr noise. The
step schema is documented in the module docstring — keep the builders in
sync with what `agents/claude/events.py` parses.

## The platform harness

Every platform ships one harness (`tests/platforms/{name}/harness.py`) that
exposes an adapter's three seams, so no test has to reach into the adapter
for them. The shape is declared as a protocol in
`tests/platforms/harness.py`:

| Member | Seam | Is |
|--------|------|----|
| `adapter` | — | the real adapter, wired to doubles |
| `await deliver()` | trigger (inbound) | one turn, driven the way production drives it |
| `requests()` | router | every `BridgeRequest` that reached `MessageRouter` |
| `output()` | surface (outbound) | what the platform's consumer is left with |

`deliver()` takes no arguments on purpose: what a turn *contains* is decided
when the harness is built, and each platform adds richer verbs of its own
(`post(conversation_id=…)`, `send(text, ts=…)`) for its own tests. Builders
are async context managers — `webhook_harness(...)`,
`heartbeat_harness(tmp_path, ...)` — taking `events` / `capacity_full` /
`known_agents` / `raises` / `config`. `output()` is per archetype: Slack
returns the visible message texts, webhook the callback payloads, heartbeat
the log lines its recorder captured.

Slack's `build_harness` implements the same three members but is not yet an
async context manager and still bypasses `SlackAdapter.__init__` — both wait
on the adapter taking an injectable app factory.

**No test-local router doubles.** `FakeBridge` covers scripted streams,
capacity rejection, unknown-agent rejection, parking a turn in flight
(`gate`) and blowing up mid-stream (`raises`). Extend it — and its contract
suite — rather than writing a one-off in a test module: a local double drifts
from the protocol silently, and one of them had been passing a test on a
`TypeError` from a signature the protocol never allowed.

The full design, including the standard for new platforms, is
[docs/design/platform-adapters.md](design/platform-adapters.md).

## The live-platform tier (real transport, no bridge, no agent)

Between the in-process tests and the e2e stack sits one more tier, in
`tests/e2e/test_live_platforms.py` (marker `live_platform`):

```
real transport  →  real Adapter  →  FakeBridge (scripted events)
```

It is the mirror of `test_live_controllers.py` — that file drives a bare
controller with no bridge or platform in front; this one drives a bare
adapter with no bridge or agent behind. The router seam stays the same fake
used one tier down on purpose: a failure here then has exactly one possible
cause, which is that reality diverged from what the fakes claim.
`tests/contracts/` pins *our* fakes to *our* implementations; nothing else
pins a fake of somebody else's API.

**Webhook** needs no credentials — its external platform is HTTP, so both
edges are hosted locally: inbound is a real `HttpServer` (embedded uvicorn,
OS-assigned port) reached by a real `httpx.AsyncClient` over a real socket,
outbound is the adapter's *production* httpx client (no `callback_transport`)
POSTing to a second real server. It therefore runs in CI's e2e job, which is
affordable because `tests/server/test_http_server.py` already boots real
uvicorn there.

The marker names the tier but gates nothing by itself: each platform's rig
skips itself when its prerequisite is missing, the same way a missing CLI
skips one agent's `live` scenarios. Platforms whose transport needs
third-party credentials (Slack) will read them from a gitignored TOML behind
a `--live-platform-config PATH` flag — never from flags or `os.environ` — so
without that flag no test can reach a real workspace.

## The live e2e (real agent CLIs)

The scripted CLIs can only prove we handle the stream shapes *we wrote
down*. `tests/e2e/test_live_controllers.py`, `tests/e2e/test_live_claude.py`
and `tests/e2e/test_live_webhook.py` spawn the real agent CLIs (claude, pi,
codex, opencode), so the argv we build, the streams we parse and the session
ids we resume are checked against the thing itself. It costs money and is
not deterministic, so it is opt-in and excluded from CI:

```bash
uv run pytest -m live --live --no-cov -v
```

`--live` switches the scenarios on, `-m live` narrows the run to just them
(`pytest --live` alone runs them alongside everything else). Requirements:
each agent's CLI on PATH and authenticated — `claude login` /
`ANTHROPIC_API_KEY`, `pi auth`, `codex login`, `opencode auth` — for that
agent's scenarios.

| Flag | Default | Meaning |
|---|---|---|
| `--live` | off | run the `live` scenarios |
| `--live-cli PATH` | `claude` | which claude binary to spawn |
| `--live-pi-cli PATH` | `pi` | which pi binary to spawn |
| `--live-codex-cli PATH` | `codex` | which codex binary to spawn |
| `--live-opencode-cli PATH` | `opencode` | which opencode binary to spawn |
| `--live-timeout SECONDS` | `300` | per-turn budget |
| `--live-tier N` | `2` | highest tier to run (see below); `0` spends nothing |
| `--live-model AGENT=MODEL` | — | run the model-override row for that agent; repeatable |

**A flag, not an env var.** The switch has to come from outside the test —
but reading it from the environment would break the rule that no test reads
`os.environ`, and would leave a stray variable able to silently start
spending tokens. The flags are declared in `tests/conftest.py`
(`pytest_addoption`) and the gate lives in one place: the same
`pytest_collection_modifyitems` that applies layer markers skips anything
marked `live` unless `--live` is set, with the reason spelled out (`-rs`
prints it). A missing CLI binary skips too, rather than failing — probed by
that agent's `live_*_config` fixture, so a missing `claude` doesn't take
down the pi scenarios or vice versa — because `pytest --live` runs the whole
suite and the rest is still worth reporting on. CI's e2e job runs
`-m "e2e and not live"`, so the gate is belt-and-braces.

### Tiers: how much of it runs

Every live scenario carries a `live_tier(n)` marker — on the function for the
hand-written ones, on the parametrization for the matrix rows — and
`--live-tier` skips anything above it. An **untagged** live scenario counts as
tier 2, because every one of them spends tokens: the default has to be "costs
money", or `--live-tier=0` would quietly run the whole paid suite.

| Tier | Cost | What it pins |
|---|---|---|
| 0 | none | the CLI exists, its version is recorded, and its own `--help` still lists every flag `build_command` emits — on the new-session *and* the resume branch |
| 1 | one turn per row | one config knob flipped: the real CLI accepts the argv we build for it |
| 2 | one or two turns | behaviour: the knob restricts what it claims to, the stream parses, resume reattaches, tool use surfaces |
| 3 | several turns | prompt-shape robustness. Reserved; opt in with `--live-tier=3` |

Tier 0 is the run to do after upgrading an agent CLI — it spends nothing and
catches the drift class that has actually bitten us:

```bash
uv run pytest -m live --live --live-tier=0 --no-cov
```

It ends with a `live agent CLI versions` summary (`claude: 2.1.241 …`), so a
failing live run can be blamed on — or cleared of — a version bump.

### The matrix

The controller layer is declarative: `tests/e2e/live_matrix.py` holds one row
set per agent (`LIVE_MATRIX`) plus each agent's tier-0 spec (`FLAG_SPECS`), and
`test_live_config_axis` runs every row. Adding a config knob means adding a
row, not writing a test function. A row is `LiveCase(name, tier, …)`; unset
fields mean "one accepted-argv turn against the `PONG` prompt", so the cheap
default costs one line:

```python
LiveCase("effort_override_accepted", tier=1, mutate=lambda c: replace(c, effort="low"))
```

The fields that change the shape: `expect="no_write"` (the knob restricted the
agent — `forbidden.txt` is absent), `strict_empty` (and nothing else appeared
either), `is_new=False` (the resume branch), `base` (an alternative config
fixture, for a world `replace()` can't build), `tolerate_error` (a CLI-side
rejection skips rather than fails — model/variant reachability is
account-local), and `model_from_option` (the row waits for `--live-model`).

`tests/e2e/test_live_matrix_spec.py` enforces the matrix's own invariants —
**in CI, with no CLI and no tokens**, since nothing behind `--live` can stop a
new agent from shipping with an empty live spec: every agent has a flag spec
and rows, both command branches are probed, an agent whose resume needs a
stored handle also pins what happens when that handle is gone, and a missing
restriction axis is a written entry in `NO_RESTRICTION_AXIS` rather than an
oversight. It also unit-tests the two helpers tier 0 leans on, `build_flags`
and `lists_flag` (a substring match would let tier 0 pass on a flag the CLI no
longer has).

The flags feed the `live_{agent}_config` / `live_{agent}_controller`
fixtures and the rigs built on them (`live_controller_rig`, `live_stack`,
`live_webhook_stack`) in `tests/e2e/conftest.py`. All four agents are
sandboxed in throwaway `tmp_path` work dirs, so an agent running with
`acceptEdits` (claude), unrestricted tools (pi) or a writable sandbox
(codex, opencode) can never touch the repo; the claude config pins
`effort=low` (cheap: these assert plumbing, not reasoning), the others leave
model/effort to the CLI's own settings, and codex/opencode keep their
session maps *outside* the work dir so "nothing appeared in the work dir"
assertions see only what the agent wrote.

`test_live_controllers.py` — every agent's bare controller (claude, pi,
codex, opencode: `live_controller_rig` runs each scenario once per agent),
prompt in → `BridgeEvent`s out, no bridge or platform in between:

| Scenario | Agents | Pins |
|---|---|---|
| `..._run_streams_exactly_one_completion_with_usage` | all | the real stream parses, the engine's exactly-one-`Completion` guarantee holds, and real usage numbers reach `Usage.from_completion` (cost asserted where the agent reports it: claude, pi) |
| `..._run_resumes_the_same_agent_session` | all | resume really reattaches — `--resume` (claude), `--session-id` (pi), the `SessionHandleStore` round-trip (codex `exec resume`, opencode `-s`) |
| `..._system_prompt_reaches_the_model` | all | the platform-built system prompt is part of what the model reads — native flag (claude, pi) or stdin folding (codex, opencode) |
| `..._tool_use_writes_in_work_dir_and_streams_status` | all | a real tool call runs, confined to the sandboxed work dir, and surfaces as a `StatusUpdate` mid-stream |

Two more run once per agent and are hand-written because they don't fit a row:
`..._timeout_kills_the_run_and_reports_error` (`timeout_seconds` against the
real CLI: process tree killed at the deadline, stream still ends with exactly
one error `Completion`) and `..._claude_worktree_mode_isolates_the_session`
(`worktree_enabled`: the CLI builds the session worktree off `origin/HEAD`,
files land there and not in the repo root, `cleanup_session` reclaims it).

Everything else on the config axis is a `LIVE_MATRIX` row, run by
`test_live_config_axis[<agent>-<row>]`:

| Row | Agents | Tier | Pins |
|---|---|---|---|
| `permission_mode_default_blocks_writes` | claude | 2 | `permission_mode="default"` in print mode has no one to ask — the write is CLI-denied |
| `model_alias_accepted` | claude | 1 | `--model` carries a value the real CLI resolves (`haiku`: a stable alias, not a bet on the account) |
| `skip_permissions_argv_accepted` | claude | 1 | the `--dangerously-skip-permissions` branch of `build_command` is argv the CLI takes |
| `exclude_tools_blocks_writes` | pi | 2 | `--exclude-tools` really restricts (the allowlist's other half is pinned through the webhook rig) |
| `thinking_level_accepted` | pi | 1 | `--thinking` takes the levels our config validates |
| `readonly_sandbox_blocks_writes` | codex | 2 | `sandbox_mode="read-only"` really restricts: the file cannot appear, CLI-enforced |
| `effort_override_accepted` | codex | 1 | the `-c model_reasoning_effort="…"` spelling is one the real CLI accepts |
| `git_work_dir_needs_no_skip_flag` | codex | 1 | the production default (git work dir, no skip flag) passes codex's trusted-directory probe |
| `variant_accepted` | opencode | 1 | `--variant` is argv the CLI takes (provider-specific, so a rejection skips) |
| `resume_without_stored_handle` | codex, opencode | 2 | a resume with nothing in the handle map degrades to a fresh session instead of failing the turn |
| `model_override_accepted` | all | 1 | `--model`/`-m` carries the model `--live-model <agent>=<model>` names; skipped without the flag |

Opencode has no tier-2 restriction row: `opencode run` has no sandbox and
`OpencodeConfig` exposes no permission knob, so there is nothing to flip. That
absence is an entry in `NO_RESTRICTION_AXIS`, not a gap — the spec test fails
if an agent has neither a restriction row nor a written reason.

The suite has already earned its keep: its first run caught codex dropping
`--skip-git-repo-check` on the resume branch — sessions could start in a
non-git work dir but never resume there. Tier 0 now catches that same class of
drift for free, from the CLI's own help.

`test_live_claude.py` — the Slack rig on top of the bare controller, one
scenario per thing the fake cannot prove:

| Scenario | Pins |
|---|---|
| `..._thread_resumes_the_same_claude_session` | two turns in one Slack thread land in one Claude session, keyed by thread |
| `..._tool_use_reaches_slack` | a real tool call runs in the sandbox and renders as a status update mid-stream |

`test_live_webhook.py` — the webhook rig (`WebhookStack`: in-process ASGI
POST in, `MockTransport`-captured callback out, everything between real).
The webhook adapter always routes to the bridge's default controller, so the
shared scenarios are parametrized to run once per agent:

| Scenario | Agents | Pins |
|---|---|---|
| `..._delivers_a_real_completion` | both | 202-then-callback carries a real completion with real cost/duration, and the conversation is persisted |
| `..._conversation_resumes_the_agent_session` | both | two POSTs of one `conversation_id` land in one agent session (claude `--resume`; pi `--session-id` reattach) |
| `..._sender_reaches_the_agent` | both | the `[sender]:` pre-tag is part of what the model reads |
| `..._tool_use_writes_in_the_work_dir` | both | a real tool call runs, confined to the sandboxed work dir |
| `..._non_resumable_turn_leaves_no_session` | both | `resumable=false` mints an ephemeral id the store never sees, and the real CLI accepts it |
| `..._pi_tool_allowlist_blocks_writes` | pi | `--tools` read-only really restricts: no file can appear, CLI-enforced |

Prompts force a token to assert on (`PONG`, `BANANA47`, `DONE`); never
assert on the model's prose.

## Conventions

- **Time**: throttle/sleep behaviour is tested by monkeypatching
  `time.monotonic` / `asyncio.sleep` in the module under test. No clock
  seam in `src/` (revisit only if this proves brittle).
- **Typing**: `tests/fakes/` and `tests/contracts/` are in pyright's strict
  `include`; new test code should be type-clean even where not gated.
- **Legacy style**: older tests using `MagicMock` / `__new__` bypasses are
  migrated opportunistically when touched — new tests use the fakes.
- **Markers**: every test carries a layer marker. `e2e` is auto-applied to
  `tests/e2e/` and `unit` to anything unmarked (both in `tests/conftest.py`);
  `integration` is declared per module (`pytestmark`) where tests cross a
  process boundary, e.g. spawning the scripted CLI. Select layers with `-m`
  (`uv run pytest -m "not e2e"`). `live` is an orthogonal opt-in on top of
  `e2e`, not a layer, and needs `--live` as well as its marker.
  `live_platform` is also orthogonal to the layers but is *not* flag-gated —
  each platform's rig skips itself when its prerequisite is absent. Markers are
  registered in `pyproject.toml` and `--strict-markers` rejects typos.
- **Running**: `uv run pytest -q` (full suite, coverage gate applies);
  single files or `-m` subsets need `--no-cov`. In CI the version matrix runs
  `-m "not e2e"` with the coverage gate; a separate 3.12-only job runs
  `-m "e2e and not live"` without coverage. The `live` scenarios are never
  run by CI.

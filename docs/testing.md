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

## Layout

```
tests/
├── conftest.py              # shared fixtures: fake_claude factory, session_manager
├── fakes/                   # typed doubles for the protocol seams (pyright strict)
│   ├── agents.py            # FakeAgentController — scripted event replay, records calls
│   ├── bridge.py            # FakeBridge — implements MessageRouter, capacity_full mode
│   ├── platforms.py         # FakePlatformAdapter — lifecycle + cleanup recorder
│   ├── slack.py             # FakeSlackClient / FakeBoltApp / event payload builders
│   └── claude_cli.py        # scripted claude CLI stand-in + scenario schema
├── contracts/               # real implementation and its fake run the same suite
│   ├── test_agent_controller.py
│   ├── test_message_router.py
│   └── test_platform_adapter.py
├── test_env.py              # the typed env readers
├── test_config.py           # AppConfig — the aggregate app.py builds from
├── app/                     # app.py wiring + lifecycle
├── bridge/                  # router.py, session.py, dedupe.py, config.py (core layer)
├── agents/claude/           # controller + stream-json parser
├── platforms/               # test_base.py: BasePlatformAdapter's shared dispatch flow
├── platforms/slack/         # adapter behaviour, one concern per file
├── platforms/heartbeat/
└── e2e/                     # full-stack scenarios (real components + fake CLI)
    ├── stack.py             # the rigs: Slack/webhook adapter → Bridge → controller
    ├── conftest.py          # live_* fixtures: same rigs, real claude + pi CLIs
    ├── test_live_claude.py  # live Slack/controller scenarios (opt-in, --live)
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

Real, cheap components are used directly instead of doubled: `SessionManager`
(against `tmp_path`), `PromptDedupeCache`, and the event dataclasses.

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

## The live e2e (real agent CLIs)

The scripted CLIs can only prove we handle the stream shapes *we wrote
down*. `tests/e2e/test_live_claude.py` and `tests/e2e/test_live_webhook.py`
spawn the real agent CLIs (Claude Code and pi), so the argv we build, the
streams we parse and the session ids we resume are checked against the thing
itself. It costs money and is not deterministic, so it is opt-in and
excluded from CI:

```bash
uv run pytest -m live --live --no-cov -v
```

`--live` switches the scenarios on, `-m live` narrows the run to just them
(`pytest --live` alone runs them alongside everything else). Requirements:
`claude` on PATH and authenticated (`claude login` or `ANTHROPIC_API_KEY`)
for the claude scenarios; `pi` on PATH with its provider authenticated
(`pi auth`) for the pi ones.

| Flag | Default | Meaning |
|---|---|---|
| `--live` | off | run the `live` scenarios |
| `--live-cli PATH` | `claude` | which claude binary to spawn |
| `--live-pi-cli PATH` | `pi` | which pi binary to spawn |
| `--live-timeout SECONDS` | `300` | per-turn budget |

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

The flags feed the `live_claude_*` / `live_pi_*` / `live_stack` /
`live_webhook_stack` fixtures in `tests/e2e/conftest.py`. Both agents are
sandboxed in throwaway `tmp_path` work dirs, so an agent running with
`acceptEdits` (claude) or unrestricted tools (pi) can never touch the repo;
the claude config pins `effort=low` (cheap: these assert plumbing, not
reasoning) and the pi config leaves provider/model to pi's own settings.

`test_live_claude.py` — the Slack rig and the bare controller, one scenario
per thing the fake cannot prove:

| Scenario | Pins |
|---|---|
| `..._controller_streams_a_real_completion` | the real stream-json shape parses, and `result` carries the usage/cost fields the Slack footer reads |
| `..._thread_resumes_the_same_claude_session` | `--session-id` then `--resume` really reattaches — turn 2 recalls turn 1's code word |
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
  `e2e`, not a layer, and needs `--live` as well as its marker. Markers are
  registered in `pyproject.toml` and `--strict-markers` rejects typos.
- **Running**: `uv run pytest -q` (full suite, coverage gate applies);
  single files or `-m` subsets need `--no-cov`. In CI the version matrix runs
  `-m "not e2e"` with the coverage gate; a separate 3.12-only job runs
  `-m "e2e and not live"` without coverage. The `live` scenarios are never
  run by CI.

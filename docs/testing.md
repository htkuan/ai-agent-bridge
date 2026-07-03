# Testing — layout, helpers, and playbooks

Agent Bridge's tests are split into a fast **unit** layer that mirrors the source tree and an **integration** layer that wires real components end to end. Everything runs offline: external CLIs are faked with generated shell scripts, chat platforms are faked with in-memory stubs, and no test needs real tokens or network access.

## Test pyramid

| Layer | Location | Scope | Doubles used |
|-------|----------|-------|--------------|
| Unit | `tests/unit/` | One class/function per test file, mirroring `src/agent_bridge/` | `FakeAgentController`, `FakeBridge`, mocked platform clients |
| Integration | `tests/integration/` | Several real components wired together (config chain, fake CLI → real controller → real bridge) | Fake CLI scripts on `PATH`, tmp YAML/env |

Unit tests stay cheap and precise — one behavior each, no subprocesses unless the subprocess *is* the unit (fake CLI). Integration tests prove the seams: that a YAML file actually reaches every component config, that a real controller run flows through a real bridge into the right event sequence.

```
tests/
├── conftest.py            # shared fixtures (session_manager, prepend_path, clean_agent_bridge_env)
├── helpers/               # importable test utilities — NOT test files
│   ├── agents.py          #   FakeAgentController + RunCall
│   ├── bridges.py         #   FakeBridge
│   ├── events.py          #   collect_events, event_types
│   ├── fake_cli.py        #   install_fake_cli + claude stream-json line builders
│   └── http_server.py     #   FakeApiServer (record-and-respond aiohttp fake)
├── unit/
│   ├── bridge/            # bridge, session, dedupe, events, config loader, registries
│   ├── agents/claude/     # controller + stream-json parsing
│   └── platforms/
│       ├── slack/
│       ├── telegram/
│       ├── line/
│       └── heartbeat/
└── integration/           # end-to-end, marked `integration`
```

Every directory under `tests/` carries an `__init__.py` (package mode): test modules get unique dotted names (two `test_events.py` can coexist) and `tests.helpers` is importable from any test.

## Running

```bash
uv run pytest                       # everything (default)
uv run pytest -m "not integration"  # fast path: unit tests only
uv run pytest -m integration        # end-to-end tests only
uv run pytest tests/unit/platforms/slack -v   # one component
```

The `integration` marker is registered in `pyproject.toml`. Integration test files opt in with a single module-level line:

```python
pytestmark = pytest.mark.integration
```

## Shared fixtures (`tests/conftest.py`)

| Fixture | What it gives you |
|---------|-------------------|
| `session_manager` | A `SessionManager` backed by a tmp-path JSON store |
| `prepend_path` | `prepend_path(bin_dir)` — prepends a directory to `PATH` so fake CLI scripts shadow the real binary for the duration of the test |
| `clean_agent_bridge_env` | Deletes every ambient `AGENT_BRIDGE_*` env var — required by any test that reads live `os.environ` (e.g. `ConfigSource` without an injected `env`), so a developer's shell or `.env` can't leak in |

## Helpers (`tests/helpers/`)

All exported from `tests.helpers`:

### `FakeAgentController`

A full `AgentController` implementation (including `cleanup_session`). Default behavior echoes the prompt as one `TextDelta` + `Completion`; pass `events=[...]` for a scripted sequence and `delay=` to simulate slow agent work (concurrency/capacity tests).

```python
from tests.helpers import FakeAgentController

controller = FakeAgentController(events=[Completion(text="ok", cost_usd=0.01)])
bridge = Bridge(session_manager, controller, max_concurrent=2)
...
assert controller.calls == ["expected prompt"]        # prompts, in order
assert controller.runs[0].is_new is True              # full RunCall records
assert controller.last_system_prompt == "directives"
assert controller.cleaned_up == ["session-id"]
```

Scripted events are yielded as deep copies — the bridge annotates `Completion` in place (usage), and that must not leak between runs.

### `FakeBridge`

A bridge stand-in for platform adapter tests. Records every `handle_message()` call as a kwargs dict in `.calls` and yields the configured events (default: one successful `Completion`).

```python
from tests.helpers import FakeBridge

adapter._bridge = FakeBridge([Processing(), Completion(text="done")])
...
assert bridge.calls[0]["session_key"] == "slack:C1:1.0"
assert bridge.calls[0]["resumable"] is False
```

### `collect_events` / `event_types`

```python
events = await collect_events(bridge.handle_message("key", "hi"))
assert event_types(events) == [Processing, TextDelta, Completion]
```

### `install_fake_cli`

Generates an executable shell script that impersonates an agent CLI. This is the standard pattern for testing any subprocess-based controller (Claude today; Codex/OpenCode reuse it as-is with their own output-line builders).

```python
from tests.helpers import install_fake_cli, claude_result_line

install_fake_cli(
    tmp_path / "bin",
    name="claude",                       # binary name to shadow
    lines=[claude_result_line("done")],  # stdout lines, in order
    line_delay=0.0,                      # sleep before each line (slow streaming)
    exit_code=0,                         # non-zero simulates CLI failure
    args_log=tmp_path / "args.log",      # one argv line appended per invocation
    orphan_pidfile=None,                 # set to leave a backgrounded child holding stdout
)
prepend_path(tmp_path / "bin")
```

Knob-to-scenario map:

| Knob | Scenario it exercises |
|------|----------------------|
| `lines` | Happy path, malformed output (garbage lines), missing terminal event |
| `line_delay` > controller timeout | Timeout path (`Completion(is_error=True)`) |
| `exit_code != 0` with no result line | Non-zero-exit error completion |
| `args_log` | Flag assertions: `--session-id` on first turn, `--resume <id>` on the second |
| `orphan_pidfile` | Grandchild holding the stdout pipe open — controller must break on the terminal event and reap the process group |

`claude_assistant_line(text)` / `claude_result_line(...)` build valid Claude stream-json lines (including usage payloads).

### `FakeApiServer`

A record-and-respond aiohttp server for faking HTTP APIs (Telegram Bot API and LINE Messaging API today; any webhook/REST platform reuses it). Register an async handler per `(method, path)`; every request is recorded as a `RecordedRequest(method, path, payload, headers)`. A handler normally returns a JSON payload; return a full `web.Response` instead to simulate non-200 statuses (e.g. LINE's 400 on an expired reply token). `start()` binds an ephemeral localhost port and returns the base URL — point the adapter's `api_base_url`-style config at it.

```python
from tests.helpers import FakeApiServer

server = FakeApiServer()
server.route("POST", "/bot123:abc/sendMessage", lambda payload: ...)
base_url = await server.start()
...
assert server.requests_for("/bot123:abc/sendMessage")[0].payload["text"] == "hi"
await server.stop()
```

API-specific behavior (e.g. Telegram's `getUpdates` returning one batch then empties) stays in the test file that needs it — only the generic server lives in helpers (same philosophy as `install_fake_cli` vs. the Claude line builders).

## Playbook: testing a new platform adapter

Unit tests live in `tests/unit/platforms/{name}/`; wire the adapter to a `FakeBridge` so no agent runs. Checklist:

1. **Config validation** — `from_source(ConfigSource({...}, env={}))` for YAML values, env-override wins, missing required fields raise `ValueError` naming the env var, disabled/unconfigured returns a disabled config.
2. **Session key mapping** — every inbound message shape produces the documented `{platform}:{scope}:{identifier}` key; distinct scopes never collide.
3. **Prompt/system-prompt construction** — sender tagging (`[name (id)]: text`), platform directives present, missing context fields degrade gracefully.
4. **Event rendering** — feed a `FakeBridge` scripted with each event type (`Processing`, `StatusUpdate`, `TextDelta`, `UserQuestion`, `Completion` success + `is_error=True`) and assert the platform-native output, including message-length truncation rules.
5. **Locking / ordering** — two concurrent messages in the same session serialize; different sessions don't block each other.
6. **Error paths** — platform API call failures are logged, never raised; the `resumable` flag matches the platform's session semantics.

Template:

```python
from tests.helpers import FakeBridge

def _make_adapter(events=None):
    adapter = MyAdapter.__new__(MyAdapter)     # skip __init__ (no real client)
    adapter._config = MyConfig(token="x")
    adapter._bridge = FakeBridge(events)
    adapter._client = ...                      # AsyncMock the platform SDK
    return adapter

async def test_message_reaches_bridge_with_session_key():
    adapter = _make_adapter()
    await adapter._process_message({...fake inbound payload...})
    assert adapter._bridge.calls[0]["session_key"] == "myplatform:room1:0"
```

For the integration layer:

- **Webhook-style platforms** (LINE-like): start the adapter's real HTTP server on an ephemeral port (`webhook_port=0`), POST real signed payloads at it (valid + invalid signature), and assert the HTTP status plus the outbound API calls recorded by a `FakeApiServer`. Reference: `tests/integration/test_line_end_to_end.py`.
- **Polling-style platforms** (Telegram-like): run a `FakeApiServer` on an ephemeral port, point the adapter's `api_base_url` config at it, and assert the messages the adapter sends back after a full poll → bridge → `FakeAgentController` cycle. Reference: `tests/integration/test_telegram_end_to_end.py`.

## Playbook: testing a new agent controller

Unit tests live in `tests/unit/agents/{name}/`; never require the real CLI. Checklist:

1. **Command construction** — a `_build_command`-style unit test per flag: new session vs resume, model/sandbox options, system-prompt pass-through (verbatim, omitted when empty). The controller must not parse `context` or rewrite `prompt`.
2. **Output parsing** (`events.py`) — each native event type maps to the right `BridgeEvent`; unknown event types are skipped with a log, not raised; malformed JSON lines return no events; agent-internal events (thinking, tool results) map to `None`.
3. **Happy path via fake CLI** — `install_fake_cli` with a scripted event stream → `controller.run()` yields the documented sequence ending in exactly one `Completion` (with usage when the CLI reports it).
4. **Timeout** — `line_delay` beyond a small `timeout_seconds` → error `Completion`, process tree killed.
5. **Non-zero exit / bad output** — `exit_code=1` with no terminal line → error `Completion` mentioning the exit code.
6. **Session mapping** — `args_log` proves first call passes a new-session flag and the second passes the resume flag; controllers that persist a `bridge_session_id → native_id` map must round-trip it through a tmp file.
7. **`cleanup_session`** — no-op when the feature is off; removes per-session state when on; never raises.

Integration: mirror `tests/integration/test_claude_end_to_end.py` — fake CLI → real controller → real `Bridge` + `SessionManager` → assert the full event sequence and the resume flags across two turns.

## Ground rules

- **Offline always** — no network, no real tokens, no real agent CLIs (CI enforces nothing is installed).
- **Tmp everything** — stores, state files, fake binaries all live under `tmp_path`.
- **Env hygiene** — tests that read live `os.environ` take `clean_agent_bridge_env`; prefer `ConfigSource(data, env={...})` with an injected dict wherever possible.
- **Helpers over copies** — a fake needed by two test files belongs in `tests/helpers/`; a fixture needed by two directories belongs in `tests/conftest.py`. One-off fakes stay local to their test file.
- **New source module ⇒ mirrored test module** — `src/agent_bridge/platforms/foo/adapter.py` gets `tests/unit/platforms/foo/test_*.py` (plus an `__init__.py` in each new test directory).

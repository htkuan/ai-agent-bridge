# Architecture — the three-layer contract

Agent Bridge connects chat platforms to AI agents through a strict three-layer separation. This document is the **contract**: what each layer owns, the exact semantics of every event and parameter crossing a layer boundary, and the steps to add a new component. If a change would violate something written here, the change is wrong (or this document must be revised first — deliberately).

```
Platform Adapter  ←→  Bridge  ←→  Agent Controller
(session owner)       (router)    (purely invoked)
```

| Layer | Owns | Must not know |
|-------|------|---------------|
| **Platform Adapter** (`platforms/`) | Session semantics (what a "session" is), per-session locking, prompt construction (sender tagging), system-prompt flavoring, event rendering | Which agent runs, how the agent executes |
| **Bridge** (`bridge.py`) | `session_key → session_id` resolution, global concurrency cap, optional cross-session dedupe, usage assembly, event forwarding | Anything platform- or agent-specific |
| **Agent Controller** (`agents/`) | Executing `(session_id, prompt)` and translating its native output into generic `BridgeEvent`s | Where the prompt came from, how events are rendered |

## Event model

Everything an agent produces reaches the platform as one of five generic events (`src/agent_bridge/events.py`). Agent-internal events (thinking, tool results, raw stream frames) are translated inside the agent module and never cross this boundary.

| Event | Fields | Semantics |
|-------|--------|-----------|
| `Processing` | — | A concurrency slot was acquired and the agent is starting. Emitted by the **Bridge** (never by controllers). Platforms typically render a "working…" placeholder. Exactly one per successful call, before any other event. |
| `TextDelta` | `text` | An incremental chunk of the agent's answer. 0..N per call. Platforms may stream-render or buffer; concatenating all deltas approximates the final text, but the authoritative final text is `Completion.text`. |
| `StatusUpdate` | `status`, `detail` | The agent is performing an action (tool use, long-running step). `status` is a short human-readable label, `detail` optional elaboration. Progress signal only — carries no answer content and may be dropped by platforms that can't render it. |
| `UserQuestion` | `questions: list[dict]` | The agent needs user input to proceed. The platform should surface the question(s) and route the user's answer back as the next message in the same session. |
| `Completion` | `text`, `is_error`, `cost_usd`, `duration_ms`, `metadata`, `usage`, `session_usage` | The terminal event — exactly one per call, always last. `text` is the final rendered answer (or error description when `is_error=True`). `metadata` carries structured extras (`error_code`, `dedupe`, raw `usage` counts). `usage`/`session_usage` are typed `Usage` reports assembled by the Bridge (see below). |

### Event ordering guarantee

```
Processing, (TextDelta | StatusUpdate | UserQuestion)*, Completion
```

Short-circuit paths (dedupe hit, capacity full) yield a **single `Completion`** with no `Processing`. Platforms must therefore treat `Completion` — not `Processing` — as the only event guaranteed to arrive.

### Usage reporting

Agents report raw token counts in `Completion.metadata["usage"]` using canonical keys (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `num_turns`, `duration_api_ms`). The Bridge assembles them into the typed `Completion.usage` (this turn) and `Completion.session_usage` (running total for sessions tracked from their first turn). Platforms only render. Details: [docs/bridge.md](bridge.md#usage-reporting).

## `Bridge.handle_message` — parameter semantics

```python
async def handle_message(
    session_key: str,
    text: str,
    context: dict[str, str] | None = None,
    system_prompt: str | None = None,
    resumable: bool = True,
) -> AsyncIterator[BridgeEvent]
```

| Parameter | Semantics |
|-----------|-----------|
| `session_key` | Platform-defined session identity, conventionally `{platform}:{scope}:{identifier}` (e.g. `slack:C0123:1712345.6789`, `heartbeat:tick:<iso-ts>`). The Bridge treats it as opaque except for one derivation: the dedupe scope drops the last `:`-segment, so keys should put the *most volatile* part (thread ts, message id) last. |
| `text` | The prompt, fully built by the platform. If the platform has sender identity, it pre-tags it (e.g. `[Alice (U123)]: …`) — the Bridge and agent never add or parse identity. |
| `context` | Optional, platform-defined, **opaque** `dict[str, str]` metadata (audit/log breadcrumbs like workspace or channel names). It passes through the Bridge to the agent unchanged. **Agents must not parse platform-specific keys out of `context` to change behavior** — anything an agent needs must arrive in `text` or `system_prompt`. |
| `system_prompt` | Platform-flavored directives (chat framing, scheduled-tick framing, formatting rules). Pass-through: the Bridge forwards it verbatim; the agent applies it as its system prompt without interpreting it. `None` means "no platform directives". |
| `resumable` | `True` (conversations): `SessionManager` maps `session_key → session_id` persistently, so the same key later resumes the same session, surviving restarts. `False` (one-shot triggers, e.g. heartbeat ticks): the Bridge mints a fresh ephemeral UUID per call, touches no disk, and skips dedupe — every call is independent even with an identical key. |

Full routing behavior (dedupe, capacity gate, failure paths): [docs/bridge.md](bridge.md).

## Protocols

Defined in `src/agent_bridge/protocols.py`. Both are structural (`typing.Protocol`) — implement the methods, no inheritance required.

### `AgentController`

```python
class AgentController(Protocol):
    def run(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        context: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[BridgeEvent]: ...

    async def cleanup_session(self, session_id: str) -> None: ...
```

- `run` — execute one turn. `session_id` is a Bridge-issued UUID; `is_new=False` means "resume the conversation you previously ran under this id". Yields only the five generic events, ending with exactly one `Completion`. Failures (timeout, non-zero exit, API error) become `Completion(is_error=True)` — `run` should not raise for expected failure modes.
- `cleanup_session` — release any per-session resources (worktrees, native session files) when the Bridge purges an expired session. A no-op implementation is valid. Must never raise for a session it doesn't recognize.
- The controller treats `prompt` and `system_prompt` as **opaque strings** and `context` as opaque metadata. It stays platform-agnostic by construction.

### `PlatformAdapter`

```python
class PlatformAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

- `start` — begin receiving events (connect socket, start polling/server, schedule first tick). Must return; long-running work goes into background tasks.
- `stop` — graceful shutdown: cancel background tasks, close connections.
- Everything else — session key format, per-session `asyncio.Lock`, pending-message policy, rendering — is the adapter's internal business.
- Optional hook: an adapter may expose `cleanup_stale_sessions() -> int` to drop per-session state whose `SessionManager` entry has expired; the app's periodic cleanup discovers it via `getattr` and calls it if present.

## Registries and wiring

Components register in explicit dicts — no entry-point magic (`src/agent_bridge/agents/registry.py`, `src/agent_bridge/platforms/registry.py`):

```python
# agents/registry.py
AGENT_BUILDERS: dict[str, AgentBuilder]        # name -> build(source) -> AgentController

# platforms/registry.py
PLATFORM_BUILDERS: dict[str, PlatformBuilder]  # name -> build(source, bridge, session_manager)
                                               #         -> PlatformAdapter | None
```

- An **agent builder** constructs its config via `from_source(source)` and returns a ready controller. Selecting an unknown agent name fails startup with the list of available names.
- A **platform builder** returns `None` when its platform is not configured/enabled (missing required tokens → log + `None`; opt-in platforms like heartbeat require an explicit `enabled: true`). Non-`None` adapters are started by the app.
- The entry point (`src/agent_bridge/app.py`) is component-agnostic: load `ConfigSource` → build the selected agent (`agent:` key / `AGENT_BRIDGE_AGENT`, default `claude`) → build Bridge + SessionManager (+ dedupe) → iterate the platform registry → run lifecycle (signals, periodic cleanup).

## Adding a new platform adapter

1. Create `platforms/{name}/config.py` — frozen dataclass with `from_source(source)` (+ delegating `from_env()`) and `_validate()`. Map every field per [docs/configuration.md](configuration.md).
2. Create `platforms/{name}/adapter.py` — implement `PlatformAdapter`.
3. Define the session key format: `{name}:{scope}:{identifier}`, most volatile segment last.
4. Own per-session locking (typically one `asyncio.Lock` per session key).
5. Build `text`: pre-tag with sender identity if the platform has one (`[name]: text`).
6. Build `system_prompt`: platform-flavored directives, forwarded verbatim.
7. Choose `resumable`: `True` for conversations, `False` for one-shot triggers.
8. Consume events from `bridge.handle_message(...)` and render them (remember: a lone `Completion` is possible).
9. Register a builder in `platforms/registry.py` (return `None` when unconfigured; import optional third-party deps lazily inside the builder).
10. Document in `docs/platforms/{name}.md`; update `.env.example`, `agent-bridge.example.yaml`, and the env tables (README, CLAUDE.md, configuration.md).

## Adding a new agent controller

1. Create `agents/{name}/config.py` — same config pattern as above, keys under `agents.{name}.*`.
2. Create `agents/{name}/controller.py` — implement `AgentController` (both `run` and `cleanup_session`).
3. Create `agents/{name}/events.py` — parse the agent's native output into `BridgeEvent`s; tolerate unknown native event types (log + skip). Report usage via `Completion.metadata["usage"]` canonical keys.
4. Treat `prompt`/`system_prompt` as opaque; never parse platform-specific `context` keys.
5. Register in `agents/registry.py` under its selection name.
6. Document in `docs/agents/{name}.md`; update the config tables as above.

Neither addition touches the Bridge, the entry point, or any existing component.

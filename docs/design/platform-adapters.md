# Platform Adapters — Design

**Status:** proposed (2026-08); phase 0 and phase 7's host-mounted half are
implemented — see the migration table at the end. This document records the
*why* and the standard; the *what* per platform lives in
[docs/platforms/](../platforms/). Companion to
[bridge-pipeline.md](./bridge-pipeline.md), which does the same for the
bridge's interior.

## Context

Three platforms exist and each connects differently: Slack dials out and
holds a websocket, heartbeat drives itself off a timer, webhook contributes
routes to a shared HTTP server. The `PlatformAdapter` protocol
(`start`/`stop`/`cleanup`) already absorbs all three, and
`BasePlatformAdapter.process()` already unifies the turn. What is *not*
unified is everything around it:

- **`app.py` special-cases the ingress platform.** `_build_adapters` has a
  hand-written branch per platform, and webhook's needs an extra
  `if http_server is None: raise` plus a manual `include_router` call. A
  fourth platform means a fourth branch; a WebSocket or MCP platform means
  a *fifth kind* of branch.
- **Per-session concurrency is reinvented per platform.** Slack: lock +
  `processing` + single pending slot. Webhook: `running` flag + 409.
  Heartbeat: nothing. All three also hand-roll a `dict[key, State]` with an
  idle purge in `cleanup()`.
- **The outbound edge is injectable in one platform out of three.** Webhook
  takes `callback_transport`; Slack builds a real `AsyncApp` eagerly in
  `__init__`, which forces tests to bypass the constructor with `__new__`
  and hand-copy every field — currently in four places.
- **Testing has three unrelated shapes.** See the audit in
  [docs/testing.md](../testing.md); the short version is that the same nine
  behaviours (session key shape, capacity rejection, unknown agent, cut
  stream, raised turn, …) are asserted three times, three different ways,
  and one platform's local bridge double has a signature that does not
  match the protocol at all.
- **A config field can ship without ever being read.** `test_config.py`
  proves env → dataclass; nothing proves dataclass → behaviour. Two Slack
  tests exist purely because that gap produced a real bug.
- **Nothing pins our fakes of other people's APIs.** `tests/contracts/`
  pins `FakeBridge` to `Bridge` and `InMemorySessionStore` to
  `JsonSessionStore`, but `FakeSlackClient` fakes *Slack*, and no test
  anywhere checks that Slack agrees.

The goal: a new platform — Discord (dial-out gateway), a WebSocket endpoint,
an MCP server, a Telegram long-poller — should be **one config class, one
adapter class, one harness, and a row in a registry**, with nine
conformance tests it gets for free.

## The one axis that actually varies: who owns the socket

The three platforms look like three architectures. They are one architecture
with three answers to a single question — *what makes a turn start, and who
owns the file descriptor it arrived on?*

| Kind | Who owns the I/O | `start()` does | Existing | Future |
|------|------------------|----------------|----------|--------|
| **Self-driven** | nobody — no external input | spawn the driving task | heartbeat | cron, queue drain, file watcher |
| **Self-connected** | the adapter | dial out, hold the connection | Slack (Socket Mode) | Discord gateway, Telegram long-poll, IMAP IDLE, MCP over stdio |
| **Host-mounted** | the shared `HttpServer` | auxiliary resources only | webhook | WebSocket endpoint, MCP over streamable-HTTP, inbound Slack Events API |

Everything else a platform varies — session semantics, rendering, error
envelope, concurrency policy — is *independent* of this axis and already
expressible through `BridgeRequest` + the `on_*` hooks. So the contract does
not need to grow a kind-per-shape hierarchy. It needs exactly one thing it
currently lacks: **host-mounted adapters must be able to declare what they
need mounted, instead of `app.py` knowing which platform is which.**

## Architecture: Trigger → Turn → Surface

Every adapter, regardless of kind, is three parts with two seams:

```
   ┌─────────┐   native      ┌──────────────┐  BridgeRequest  ┌────────┐
   │ Trigger │ ────event───► │     Turn     │ ──────────────► │ Bridge │
   └─────────┘               │              │ ◄──BridgeEvent──└────────┘
   self-driven /             │  pre-process │
   self-connected /          │  process()   │
   host-mounted              │  on_* hooks  │
                             └──────┬───────┘
                                    │ rendered output
                                    ▼
                              ┌──────────┐
                              │ Surface  │   Slack Web API / callback POST /
                              └──────────┘   ws.send / MCP response / logger
```

| Part | What it is | Rule |
|------|-----------|------|
| **Trigger** | whatever turns a native event into a call to `process()` | one of the three kinds above; declared, not improvised |
| **Turn** | `BasePlatformAdapter.process()` + the platform's pre-process and hooks | never overridden — the platform supplies the two ends, not the middle |
| **Surface** | the outbound client the hooks write to | **must be constructor-injectable**; the adapter never builds it from config alone |

The Surface rule is the one with teeth. It is what makes the testing standard
possible, and it is the single change that retires the `__new__` bypass.

## Contract changes

### `PlatformAdapter` stays exactly as it is

`start` / `stop` / `cleanup` remain the whole contract. Nothing below adds a
required method. Everything new is either optional (capabilities), or reuse
(helpers), or convention (the standard).

### Capabilities: declare what the host must mount

```python
# platforms/capabilities.py — fastapi types under TYPE_CHECKING only,
# so importing this module never pulls the optional [http] extra.


@runtime_checkable
class RouterMountable(Protocol):
    """Adapter contributes routes to the shared HttpServer."""

    @property
    def router(self) -> APIRouter: ...


@runtime_checkable
class AsgiMountable(Protocol):
    """Adapter contributes a sub-application (path, ASGI app) — the shape an
    SDK-provided server (e.g. MCP streamable-HTTP) hands you."""

    @property
    def asgi_mount(self) -> tuple[str, ASGIApp]: ...
```

`app.py` then mounts structurally, with no platform names in the branch:

```python
for adapter in adapters:
    if isinstance(adapter, RouterMountable | AsgiMountable):
        if http_server is None:
            raise ValueError(
                f"{type(adapter).__name__} needs AGENT_BRIDGE_HTTP_ENABLED=true"
            )
        http_server.mount_adapter(adapter)
```

`HttpServer` grows one method (`mount_adapter`, dispatching to the existing
`include_router` or a new `mount`). It still knows nothing about platforms —
it takes a router or a sub-app, same as today.

WebSocket needs nothing new: a FastAPI `APIRouter` carries
`@router.websocket(...)` endpoints, so a WS platform is `RouterMountable`.

### Registry: one row per platform, no if-chain

```python
# platforms/registry.py


@dataclass(frozen=True)
class PlatformDeps:
    config: AppConfig
    bridge: MessageRouter
    session_manager: SessionManager


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    # Returns None when this platform is not configured. Imports its adapter
    # lazily, so an optional dependency stays optional.
    build: Callable[[PlatformDeps], PlatformAdapter | None]


PLATFORMS: tuple[PlatformSpec, ...] = (
    PlatformSpec("slack", _build_slack),
    PlatformSpec("heartbeat", _build_heartbeat),
    PlatformSpec("webhook", _build_webhook),
)
```

`_build_adapters` becomes a loop plus the "at least one" check. Adding a
platform stops touching `app.py` at all.

Deliberately *not* done: entry-point / plugin discovery for out-of-tree
platforms. In-tree registration is the requirement; discovery is a different
problem with a security surface, and can be added behind the same tuple
later.

### Shared state bookkeeping

Slack and webhook both keep `dict[key, State]` plus an idle purge. Extract
the bookkeeping only — not the policy:

```python
# platforms/state.py


class SessionStates[StateT]:
    def __init__(self, factory: Callable[[], StateT]) -> None: ...
    def get(self, key: str) -> StateT: ...  # setdefault
    def keys(self) -> Iterable[str]: ...
    async def purge(
        self, disposable: Callable[[str, StateT], Awaitable[bool]]
    ) -> int: ...
```

Both `cleanup()` implementations collapse to a predicate plus one call. The
predicate stays in the platform, because "disposable" means different things
(Slack asks the `SessionManager`; webhook checks an idle clock).

### Concurrency policy is declared, not shared

Slack's latest-wins pending slot involves posting and deleting Slack
placeholder messages; webhook's rejection is an HTTP 409. There is no honest
shared mechanism, and forcing one would be worse than the duplication. What
*is* shared is the vocabulary — every adapter declares one of:

```python
class TurnPolicy(StrEnum):
    NONE = "none"  # turns can't collide (heartbeat)
    REJECT = "reject"  # second concurrent turn refused (webhook)
    LATEST_WINS = "latest"  # queued, only the newest survives (Slack)
    QUEUE_ALL = "queue"  # every turn eventually runs (none yet)
```

as a class attribute. It documents intent, and — the actual payoff — it lets
the shared conformance suite pick the right assertion for "deliver twice on
one key" without knowing which platform it is testing.

## The turn, standardised

### Pre-process: what a `BridgeRequest` must carry

| Field | Rule |
|-------|------|
| `session_key` | always `make_session_key("{platform}", scope, id)`; the platform's session semantics are *defined* by what goes in `scope`/`id` and documented in `docs/platforms/{name}.md` |
| `text` | pre-tagged with sender identity **iff** the platform has one (`[alice (U123)]: …`). Proactive triggers do not tag |
| `system_prompt` | platform framing, built by the platform: what surface this is, whether a human is present, what the agent may assume about follow-up |
| `resumable` | `False` for one-shot triggers where every call must be a fresh untracked session |
| `agent` | the platform's routing decision (channel map, config field, request field) — never the agent's |
| `context` | flat `dict[str, str]` for logging/diagnostics. **Never** read back by an agent |

### Event rendering: three archetypes

A platform picks one archetype and deviates only where it must. This is the
"how does this app talk about what the agent is doing" standard.

| Event | **Streaming** (Slack, WS, TUI) | **Single-reply** (webhook, MCP tool) | **Unattended** (heartbeat, cron) |
|-------|-------------------------------|--------------------------------------|----------------------------------|
| `Processing` | post/reset the placeholder to render into | ignore (or ack) | `info` log |
| `TextDelta` | accumulate + throttled update | accumulate; only the total is delivered | `debug` log |
| `StatusUpdate` | show progress alongside the text | optional progress notification, else ignore | `info` log |
| `UserQuestion` | render the question, enter waiting state | **cannot be answered** → `warning`, and the system prompt must already tell the agent not to ask | `warning` log |
| `Completion` | rewrite the message to the final text | deliver the single payload | `info` (or `error`) log |
| `on_stream_end` | close the surface out (strip transient status) | deliver an error envelope (`no_completion`) | `warning` log |

Two obligations regardless of archetype:

1. **The consumer always reaches a terminal state.** Every path out of
   `process()` — Completion, cut stream, raised exception, cancellation —
   must leave the consumer with something final. A placeholder that stays up
   forever is a bug, and it is one of the conformance tests.
2. **The error envelope belongs to the platform.** `process()` propagates;
   the platform decides what to reset and what the consumer sees. Slack does
   this in two layers (`_stream_response` closes the visual, `_process_message`
   resets session state) — that layering is the reference pattern, not an
   accident.

## Implementation standard — a new platform

1. `platforms/{name}/config.py` — frozen dataclass, `from_env(env)` +
   `from_env_optional(env)` + `_validate()` from `__post_init__`. Parse only
   through `agent_bridge/env.py`. Filesystem/network probes go in
   `check_prerequisites()`.
2. `platforms/{name}/adapter.py` — subclass `BasePlatformAdapter[RunStateT]`.
   Declare:
   - `PLATFORM: ClassVar[str]` — the session-key prefix
   - `TURN_POLICY: ClassVar[TurnPolicy]`
   - the trigger kind, in the class docstring, in the vocabulary above
3. **Inject the Surface.** The outbound client is a constructor parameter
   with a production default (`callback_transport` in webhook is the
   template). No adapter constructs an unswappable network object in
   `__init__`.
4. Host-mounted platforms expose `router` / `asgi_mount` and keep
   `start`/`stop` for auxiliary resources only.
5. Pre-process into a `BridgeRequest` per the table above; call
   `await self.process(request, state)`.
6. Override the `on_*` hooks for your archetype. Do not override `process()`.
7. Use `SessionStates[…]` if you keep per-key state, and implement
   `cleanup()` as a disposability predicate.
8. Register in `platforms/registry.py`.
9. Tests per the standard below.
10. `docs/platforms/{name}.md`, `.env.example`, and the CLAUDE.md table.

## Testing standard

### Three seams, three doubles — for every platform

| Seam | Production | Test double | Recorded as |
|------|-----------|-------------|-------------|
| Trigger (inbound) | socket / timer / HTTP | driven by the harness's `deliver()` | — |
| Router | `Bridge` | `FakeBridge` (shared, typed) | `bridge.calls: list[BridgeRequest]` |
| Surface (outbound) | Slack API / httpx / ws / logger | injected fake or `caplog` | `harness.output()` |

**No local bridge doubles.** `FakeBridge` covers scripted streams, capacity
rejection, and unknown-agent rejection; extend *it* (and its contract suite)
rather than writing one in a test module. The current heartbeat `_StubBridge`
and Slack `_ExplodingBridge` are exactly what this rule prevents — the latter
has a signature the protocol never allows (`handle_message(**kwargs)` vs the
positional `request`), so the test it backs passes on a `TypeError` instead
of the failure it means to simulate.

### Every platform ships one harness

The protocol lives in `tests/platforms/harness.py`; each implementation sits
next to the platform it drives.

```python
class PlatformHarness[OutputT](Protocol):
    adapter: PlatformAdapter

    async def deliver(self) -> None:
        """Make one turn happen, the way the platform's trigger does, and
        return once the turn is observable (awaited, drained, or polled)."""

    def requests(self) -> list[BridgeRequest]:
        """Every BridgeRequest that reached the router, in order."""

    def output(self) -> OutputT:
        """What the platform's consumer is left looking at."""
```

Three members, one per seam — trigger, router, surface — which is the whole
reason the suite below can be written once.

`deliver()` takes **no arguments**. An earlier draft gave it `text` and
`session`; both turned out to be lies for the self-driven kind, whose prompt
comes from config and whose session key is minted per tick. What a turn
*contains* is decided when the harness is built, and platform-specific
richer verbs (`post(conversation_id=…)`, `send(text, ts=…)`) live on the
concrete harness for that platform's own tests.

The router seam is exposed as `requests()` rather than a `bridge` attribute,
so a harness can be handed a non-default router (a gated `FakeBridge`, or a
real `Bridge` in the e2e rigs) without the protocol claiming otherwise.

Built by `{name}_harness(*, events=None, capacity_full=False, known_agents=…,
raises=False, config=None)` as an async context manager, so `start`/`stop`
are part of the shape. Three rules:

- construct the adapter through its **real** constructor (this is why the
  Surface must be injectable),
- `deliver()` goes through the real trigger path — bolt handler, HTTP POST,
  timer fire — never straight into a private render method,
- the same harness is reused by the e2e rig (`tests/e2e/stack.py`), which
  swaps `FakeBridge` for a real `Bridge`.

### The shared conformance suite

`tests/contracts/test_platform_turn.py`, parametrized over every harness
factory. Nine behaviours, written once:

| Scenario | Assertion |
|----------|-----------|
| one delivery | exactly one `BridgeRequest`; `session_key` starts with `{PLATFORM}:`; `system_prompt` non-empty |
| happy path | `output()` carries the agent's reply |
| `capacity_full` | consumer sees an error, not silence |
| unknown agent | same, when the platform lets a caller name one |
| stream ends without `Completion` | consumer reaches a terminal state |
| bridge raises mid-stream | consumer sees an error envelope |
| …and recovers | a second delivery still works (no wedged state) |
| two deliveries, same key | assertion selected by `TURN_POLICY` |
| `start`/`stop`/`cleanup` | cycle completes; `cleanup()` ≥ 0 and idempotent |

This subsumes the existing `tests/contracts/test_platform_adapter.py` (its
two lifecycle tests become the last row) and replaces ~30 hand-written
near-duplicates across the three platforms.

### Testing the platform's own event logic

The part that cannot be shared is *how* each platform renders — so
standardise the stimulus instead of the assertion. One canonical script in
`tests/fakes/events.py`:

```python
ALL_EVENTS: list[BridgeEvent] = [
    Processing(),
    TextDelta(text="hello"),
    StatusUpdate(status="Using Bash...", detail="ls"),
    UserQuestion(questions=[{"question": "which?"}]),
    Completion(text="done"),
]
```

Every platform has a `test_rendering.py` that drives this script (plus the
cut-stream variant) through its harness and asserts its surface as a table:

```python
@pytest.mark.parametrize(("event", "expected"), RENDER_CASES)
async def test_renders_event(event, expected): ...
```

`RENDER_CASES` is per-platform and *is* the platform's rendering spec — the
one place a reviewer reads to learn what this app shows its users. Anything
beyond it (Slack's throttling and byte-truncation, webhook's callback
retries, heartbeat's state file) is a platform-specific file, one concern
each, as today.

### Config is a tested axis, not just a parsed one

`test_config.py` proves the environment reaches the dataclass. It does not
prove the dataclass reaches the code. That gap is not hypothetical — two
existing Slack tests carry the docstring *"pins that the adapter reads the
config field rather than the constant it was extracted from"*, because a
field was once added next to a module constant that the code kept using.

So every platform also gets `tests/platforms/{name}/test_config_effects.py`,
built on three rules:

1. **Assert a delta, never an absolute.** One field, two configs, one
   stimulus, two different observations. Asserting `sleep == 2.5` proves
   nothing if 2.5 is also the default; asserting *baseline ≠ variant* proves
   the value travelled.

   ```python
   async def test_throttle_window_comes_from_config():
       async with build_harness(config=slack_config()) as h:
           default = await observe_completion_wait(h)
       async with build_harness(config=slack_config(update_throttle_seconds=3.0)) as h:
           tuned = await observe_completion_wait(h)
       assert tuned > default
   ```

2. **Declare coverage explicitly.** The module exports two sets:

   ```python
   COVERED: frozenset[str] = frozenset({"update_throttle_seconds", "msg_max_bytes", ...})
   NO_EFFECT: dict[str, str] = {
       "app_token": "credential — only reaches the socket handler, covered by the live-platform tier",
   }
   ```

3. **Guard completeness in a shared contract.**
   `tests/contracts/test_config_coverage.py` is parametrized over every
   platform's `(ConfigCls, COVERED, NO_EFFECT)` and asserts

   ```python
   {f.name for f in fields(ConfigCls)} == COVERED | NO_EFFECT.keys()
   ```

   Adding a config field then fails the suite until it is either given a
   behaviour test or explicitly written off with a reason. This is the
   behavioural sibling of the existing `Config.from_env({}) == Config()`
   drift guard.

**Combinations only where fields genuinely interact.** The cross product is
not worth testing; named couplings are. There is already a real one —
`test_footer_does_not_push_inline_reply_to_upload` covers
`usage_report_enabled` × `msg_max_bytes`. Those get their own named tests and
count toward `COVERED` for both fields.

**Cross-config constraints live in `tests/app/test_wiring.py`.** A config
combination that is invalid across components (webhook enabled with the HTTP
server off, `default_agent` naming an unregistered profile) is not a platform
concern — it is an assembly concern, and it fails at `app.py`.

### Independent platform integration — the live-platform tier

Everything above stops at the adapter's own boundary: the transport is
simulated (`FakeBoltApp`, `ASGITransport`, a directly-called timer) and the
Surface is a fake. Nothing anywhere proves that `FakeSlackClient` behaves
like Slack. `tests/contracts/` pins *our* fakes to *our* implementations;
nobody pins a fake of somebody else's API.

That is the missing symmetry. `tests/e2e/test_live_controllers.py` drives
each agent's bare controller with no bridge and no platform in between; the
mirror image is a bare **adapter** with no bridge and no agent in between:

```
real transport  →  real Adapter  →  FakeBridge (scripted events)
```

No `Bridge`, no `SessionManager`, no controller, no agent tokens spent. The
router seam is the same `FakeBridge` used everywhere else — what changes is
that the *transport* and the *Surface* are real. `tests/e2e/test_live_platforms.py`,
marker `live_platform`, orthogonal to `live` exactly as `live` is orthogonal
to `e2e`.

Which platforms get this tier falls straight out of the trigger taxonomy:

| Kind | Live-platform tier | Why |
|------|-------------------|-----|
| self-driven | not applicable | no external party; the timer and state file are already fully covered |
| self-connected | required | the dial-out, the auth handshake, and the API fake all need pinning |
| host-mounted | required, and self-contained | "the external platform" is HTTP — hostable locally, no credentials |

**Host-mounted (webhook, future WS/MCP-HTTP) needs nobody's account.** Start
the real `HttpServer` on port 0, POST over a real socket with a real httpx
client, and receive the callback on a throwaway local server. Today's webhook
tests never touch a real socket or real uvicorn — `ASGITransport` and
`MockTransport` short-circuit both ends. This tier is cheap enough that it
could eventually run in CI.

**Self-connected (Slack, future Discord) splits into two levels:**

- *Outbound conformance* — bot credentials only, unilaterally verifiable:
  `start()` really connects Socket Mode, `auth_test` resolves the bot id,
  channel/user/team lookups return the shapes `SlackInfoCache` parses, and a
  post → update → delete → snippet-upload round trip behaves the way
  `FakeSlackClient` claims (including the real `msg_too_long` threshold,
  which the byte-truncation logic is built around).
- *Full round trip* — additionally needs a user token and the bot present in
  the test channel, so the test can post a message mentioning the bot and
  have a genuine `app_mention` arrive over the live socket. This is the only
  way the inbound half is ever exercised for real.

Two hard requirements for this tier: it **mutates a real workspace**, so the
target channel must be named explicitly with no default and the test deletes
what it posted; and it is **never run by CI**, same as `live`.

**Credentials come from a file, not flags or the environment.** A
`--live-platform-config PATH` flag pointing at a gitignored TOML keeps the
"no test reads `os.environ`" rule intact *and* keeps secrets out of shell
history and the process list — neither of which a `--live-slack-bot-token`
flag would manage. It also matches how the app already takes structured
config (`AGENT_BRIDGE_PROFILES_PATH`).

**The gate is per-platform, not per-tier.** The `live_platform` marker names
the tier but does not by itself skip anything; each platform's rig skips
itself when its prerequisite is missing, exactly as a missing CLI skips one
agent's `live` scenarios today. Webhook has no prerequisite, so it simply
runs — including in CI's e2e job, which is affordable because
`tests/server/test_http_server.py` already boots real uvicorn there. Slack's
prerequisite is the credentials file, so without `--live-platform-config`
nobody can reach a real workspace by accident, and no separate opt-in flag
is needed for the tier as a whole.

### The full ladder

| Tier | Transport | Router | Agent | Runs in CI |
|------|-----------|--------|-------|-----------|
| unit | — | — | — | yes |
| contract (`test_platform_turn`) | simulated | `FakeBridge` | — | yes |
| config effects | simulated | `FakeBridge` | — | yes |
| **live-platform** | **real** | `FakeBridge` | — | host-mounted: yes; self-connected: no |
| e2e | simulated | real `Bridge` | scripted CLI | yes |
| live | simulated | real `Bridge` | real CLI | no |

Each row changes exactly one thing from a neighbour, which is what makes a
failure diagnosable: a green contract row plus a red live-platform row means
the fake drifted from the real API, and nothing else.

### What each layer owns

| Layer | Owns |
|-------|------|
| `tests/platforms/test_base.py` | `process()` dispatch, hook fallbacks, request forwarding — tested once, never re-tested per platform |
| `tests/contracts/test_platform_turn.py` | the nine behaviours, every platform |
| `tests/platforms/{name}/test_rendering.py` | that platform's rendering spec |
| `tests/platforms/{name}/test_*.py` | genuinely unique mechanics |
| `tests/platforms/{name}/test_config.py` | env parsing + validation (unchanged) |
| `tests/platforms/{name}/test_config_effects.py` | one delta test per config field, plus `COVERED` / `NO_EFFECT` |
| `tests/contracts/test_config_coverage.py` | no config field escapes without a test or a written reason |
| `tests/e2e/test_live_platforms.py` | real transport × real Surface, `FakeBridge` behind it |
| `tests/e2e/` | the platform's `{Name}Stack` against a real `Bridge` + scripted CLI |

Every platform gets a `wire_{name}()` / `{Name}Stack` in `tests/e2e/stack.py`,
agent-agnostic so `--live` fixtures reuse it. Two gaps close here: webhook's
`WebhookStack` currently has no scripted (non-live) scenario, and heartbeat's
e2e hand-wires its own bridge instead of using a rig.

## Worked examples

**WebSocket chat endpoint** — host-mounted, streaming archetype, `LATEST_WINS`.
`router` carries `@router.websocket("/v1/chat")`; `RunStateT` holds the
connection; hooks `await ws.send_json(...)`; the Surface is the connection
object, injected in tests as a recorder with a `sent` list. Session key
`ws:{connection_id}:{thread}`. Everything else is inherited.

**MCP server** — two deployments, one adapter. Over stdio it is
self-connected (`start()` spawns the serve loop); over streamable-HTTP it is
host-mounted (`asgi_mount`). One agent turn = one tool call, so it is the
single-reply archetype: `TextDelta` accumulates, `StatusUpdate` maps to a
progress notification, `Completion` becomes the tool result. `UserQuestion`
is the interesting one — MCP elicitation means it is the first platform where
a "single-reply" surface *can* answer, so it would declare the streaming
archetype's waiting behaviour instead. The conformance suite covers it
either way because it asserts "consumer reaches a terminal state", not "the
consumer is a human".

**Discord** — self-connected, streaming, `LATEST_WINS`. Structurally Slack
with a different Surface; the shared conformance suite plus a rendering table
is most of its test suite.

## Decisions

### Why not a base class per trigger kind

`SelfDrivenAdapter` / `MountedAdapter` base classes were considered and
rejected: the kinds differ only in what `start()` does (5–15 lines each) and
share nothing else, while a hierarchy would force a choice for hybrids — MCP
is host-mounted *or* self-connected depending on deployment, with identical
turn logic. Kinds stay vocabulary; capabilities stay structural.

### Why capabilities are structural (`isinstance`) not declared

An explicit `requires = ("http",)` attribute would need every adapter to
carry it. A `runtime_checkable` Protocol makes the declaration *be* the
implementation: an adapter that has a `router` is mountable, and pyright
strict already checks the property's type at the definition site.

### Why `TURN_POLICY` is a declaration and not an implementation

Extracting Slack's pending-slot machinery would drag Slack's placeholder
message lifecycle into shared code. The declaration buys the thing worth
buying — a shared test — at zero coupling cost.

### Why config effects assert a delta rather than a value

An absolute assertion (`assert waited == 2.5`) passes just as happily when
the code ignores the config and the default happens to match. Two configs
and a difference is the only form that can distinguish "read from config"
from "read from the constant next to it" — which is the bug this file
exists to catch.

### Why the live-platform tier keeps `FakeBridge`

Putting a real `Bridge` behind it would make a failure ambiguous — a red test
could mean the transport broke, the session policy broke, or the agent broke.
Holding the router seam fixed at the same fake used one tier down means a
live-platform failure has exactly one possible cause: reality diverged from
the fake. That is also why the tier is worth having at all; it is the contract
suite for the fakes we write of *other people's* APIs, which nothing else
covers.

### Why live-platform credentials come from a file

Flags put secrets in shell history and `ps` output; `os.environ` is barred by
the suite-wide rule that no test reads it. A gitignored TOML behind one
`--live-platform-config PATH` flag satisfies both, and mirrors how the app
already loads structured config.

### Why the harness is per-platform but its protocol is shared

`deliver()` cannot be shared (each trigger differs) and `output()` cannot be
shared (each surface differs). Their *signatures* can, and that is exactly
enough for the conformance suite to be written once.

## Migration path

Independently shippable, in order. Nothing below is a big-bang rewrite.

| Phase | Change | Unblocks |
|-------|--------|----------|
| 0 ✅ | `tests/fakes/events.py`; `PlatformHarness` shape; move webhook's in-file harness to `harness.py`; heartbeat drops `_StubBridge` for `FakeBridge`; fix `_ExplodingBridge`'s signature | the conformance suite |
| 1 | `SlackAdapter(..., app_factory=…)`; delete the four `__new__` copies | Slack joins the conformance suite; `test_adapter_io.py` / `test_allow_channels.py` drop `MagicMock` |
| 2 | `tests/contracts/test_platform_turn.py`; retire the duplicated per-platform scenarios | new platforms cost ~1 test file |
| 3 | `platforms/capabilities.py` + `registry.py`; `HttpServer.mount_adapter`; `_build_adapters` becomes a loop | WS / MCP land without touching `app.py` |
| 4 | `platforms/state.py`; Slack + webhook `cleanup()` collapse | — |
| 5 | scripted webhook e2e; `HeartbeatStack`; `wire_*` for every platform | e2e parity |
| 6 | `test_config_effects.py` per platform + `test_config_coverage.py` | a new config field cannot ship untested |
| 7 ◐ | `test_live_platforms.py`: host-mounted **done**; Slack outbound and the user-token round trip still to come | the API fakes get pinned to reality |

Phases 0–2 are pure test work and pay for themselves immediately. Phase 3 is
the one that has to exist before the fourth platform, not after it. Phase 6
is cheap and can be pulled forward — it depends only on harnesses taking a
`config=` argument, which Phase 0 delivers. Phase 7's host-mounted half is
also independent of everything else and is the best place to start if the
Slack credentials for a test workspace are not available yet.

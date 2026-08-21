# CHANGELOG

<!-- version list -->

## v0.9.0 (2026-08-21)

### Documentation

- Fix stale coverage gate value in tech stack table
  ([`aa5b52e`](https://github.com/htkuan/ai-agent-bridge/commit/aa5b52ef7037cbb566d358c9b1d994fc824c832e))

### Features

- **agents**: Add pi agent controller with named profiles
  ([`acf52c7`](https://github.com/htkuan/ai-agent-bridge/commit/acf52c7ed51b2dc23f22f0ffc14971d48c8a5b87))

### Refactoring

- **agents**: Extract shared CliAgentController subprocess engine
  ([`fd745bd`](https://github.com/htkuan/ai-agent-bridge/commit/fd745bd6a1cd85da98b81025a57bab5fbcef0628))


## v0.8.0 (2026-08-19)

### Features

- Route slack channels to named claude controller profiles
  ([`fe1999d`](https://github.com/htkuan/ai-agent-bridge/commit/fe1999d57703371a28ea57c155823ef38303462e))


## v0.7.0 (2026-08-16)

### Features

- **server**: Add shared HTTP server hosting console and platform routers
  ([`10ad1e6`](https://github.com/htkuan/ai-agent-bridge/commit/10ad1e684d7930a4ff6b95784f91f8a10b12fd44))

- **webhook**: Add HTTP webhook platform with 202-plus-callback delivery
  ([`b1eeff8`](https://github.com/htkuan/ai-agent-bridge/commit/b1eeff89ec458736e55822b4d435b9f2283e2176))


## v0.6.0 (2026-08-15)

### Documentation

- Document the live e2e and its flags
  ([`bbefff0`](https://github.com/htkuan/ai-agent-bridge/commit/bbefff0edad453ba705d621aacf42e005e89fd61))

- Keep the bridge.md protocol snippet within ruff's line width
  ([`d346e1a`](https://github.com/htkuan/ai-agent-bridge/commit/d346e1ad7f62d9193f6585057a1473e54312c116))

- Rewrite the platform extension guide around BasePlatformAdapter
  ([`d4371c9`](https://github.com/htkuan/ai-agent-bridge/commit/d4371c9049c71a6b6a0a578e64ae65a6ed54b26f))

### Features

- **platforms**: Add BasePlatformAdapter shared flow
  ([`e473856`](https://github.com/htkuan/ai-agent-bridge/commit/e47385619b8256c5a244a523ed68d747e2b8311d))

- **platforms**: Require cleanup() on the PlatformAdapter protocol
  ([`f90cd5d`](https://github.com/htkuan/ai-agent-bridge/commit/f90cd5d2fade8c55fc4da914641979f66f7fd66d))

### Refactoring

- **heartbeat**: Adopt BasePlatformAdapter
  ([`c15f2bc`](https://github.com/htkuan/ai-agent-bridge/commit/c15f2bcea1d8d435b7211e3b9fd6d5ff2ee58c64))

- **slack**: Adopt BasePlatformAdapter
  ([`3de88d4`](https://github.com/htkuan/ai-agent-bridge/commit/3de88d4e46ceaef6ac1a20bbdb5889f18cea3ca1))

### Testing

- **e2e**: Add live scenarios against the real claude CLI
  ([`ee5f110`](https://github.com/htkuan/ai-agent-bridge/commit/ee5f1102ae908e3b676fe9cccfb31c4af24702fd))


## v0.5.0 (2026-08-15)

### Bug Fixes

- **config**: Close the fail-fast gaps found reviewing the config refactor
  ([`d56aea0`](https://github.com/htkuan/ai-agent-bridge/commit/d56aea025a9743b5e9330e8b41973a71b607cfda))

### Documentation

- Document the config architecture
  ([`a2b7098`](https://github.com/htkuan/ai-agent-bridge/commit/a2b7098d0cd749214b9b3de0a700656f1f42abe9))

### Refactoring

- Give every component its own config and centralise env reading
  ([`6f4e1bb`](https://github.com/htkuan/ai-agent-bridge/commit/6f4e1bb36045a9603fdd335be0705b00d0b66671))

### Breaking Changes

- Config classes and component constructors changed shape. Bridge(config, session_manager,
  controller, dedupe=...), SessionManager(SessionConfig(...)), PromptDedupeCache(DedupeConfig(...)),
  BridgeConfig fields are nested, HeartbeatConfig lost its `enabled` field (absence is
  AppConfig.heartbeat is None), and main() delegates to run(config). Setting only one of the two
  Slack tokens is now a startup error instead of silently disabling Slack, and boolean env values
  outside true/1/yes/on/false/0/no/off are rejected rather than read as false.


## v0.4.1 (2026-08-13)

### Bug Fixes

- **slack**: Name the missing OAuth scope in resolution warnings
  ([`89e5279`](https://github.com/htkuan/ai-agent-bridge/commit/89e52798fa6bf5b19113aba86f4981b62a19eb79))

### Documentation

- **slack**: Document the scopes conversations.info needs
  ([`53b911b`](https://github.com/htkuan/ai-agent-bridge/commit/53b911bdce4091775e4a18189e94321806a2b677))


## v0.4.0 (2026-08-13)

### Build System

- Raise coverage ratchet to 98
  ([`d7f69d8`](https://github.com/htkuan/ai-agent-bridge/commit/d7f69d888c9038b51dcf14b274b65e4cf8d4a1e9))

### Continuous Integration

- Run e2e scenarios in a dedicated 3.12-only job
  ([`f2054f4`](https://github.com/htkuan/ai-agent-bridge/commit/f2054f47e2153aefd43c7c2c257e60adb3698f24))

### Documentation

- Document layer markers and the raised coverage gate
  ([`a6c9b41`](https://github.com/htkuan/ai-agent-bridge/commit/a6c9b418daee5de10c831970cfe09748782fa780))

### Refactoring

- Split src into agents/, bridge/, platforms/ packages
  ([`abcfe4c`](https://github.com/htkuan/ai-agent-bridge/commit/abcfe4ce87b0e8ea8ecc4ab71f88b2953596ecb1))

### Testing

- Add unit/integration/e2e layer markers
  ([`77d1438`](https://github.com/htkuan/ai-agent-bridge/commit/77d1438eafa1e8c2e4795604977b6114c492a79c))

- **app**: Cover wiring, periodic cleanup, and signal shutdown
  ([`74b232c`](https://github.com/htkuan/ai-agent-bridge/commit/74b232cdc2586a7e5d715c4813f20974d0ad45a8))

- **e2e**: Add full-stack rig and seven bridge scenarios
  ([`e3c6e5f`](https://github.com/htkuan/ai-agent-bridge/commit/e3c6e5fb5230f91decd980a71e5080ce8c075b9e))

- **slack**: Add adapter test harness wired to typed fakes
  ([`61b71ff`](https://github.com/htkuan/ai-agent-bridge/commit/61b71ff2c178eaa61594b94e165571ada6d8bc46))

- **slack**: Cover info cache, session state, handlers, and context
  ([`c8b6c72`](https://github.com/htkuan/ai-agent-bridge/commit/c8b6c72763a7e7be16730f2f84cbffe402e7f188))

- **slack**: Cover message flow, rendering, throttle, and lifecycle
  ([`2a9be81`](https://github.com/htkuan/ai-agent-bridge/commit/2a9be81e330a9ca2d62f54d4c7ee2b5722772f65))

### Breaking Changes

- Bridge-layer modules moved from agent_bridge.<name> to agent_bridge.bridge.<name>, and the app
  entry point from agent_bridge to agent_bridge.app. The agent-bridge console script is unchanged.


## v0.3.1 (2026-08-09)

### Bug Fixes

- **claude**: Complete the stream when the process exits without a result
  ([`20a3ec1`](https://github.com/htkuan/ai-agent-bridge/commit/20a3ec1d60e2c447af71f706106efb1034e9ca9a))

### Build System

- Type-check test fakes and contracts with pyright
  ([`34e9b67`](https://github.com/htkuan/ai-agent-bridge/commit/34e9b67d537ffdf70689a09976e05fbf1c116371))

### Documentation

- Document the test framework design
  ([`82b512a`](https://github.com/htkuan/ai-agent-bridge/commit/82b512a1c799fbddacd02e5722bb3776ecde49c0))

### Testing

- Add typed protocol fakes and a scripted claude cli
  ([`4e24eb2`](https://github.com/htkuan/ai-agent-bridge/commit/4e24eb2b1bd904e959a8d62ecdecfb3ccd56f436))

- Cover bridge config parsing and session-store failure paths
  ([`1c34a7c`](https://github.com/htkuan/ai-agent-bridge/commit/1c34a7c17a32570b4aa966113cd6d2518ec0d401))

- Cover controller timeout, kill, and worktree failure paths
  ([`b24d0d8`](https://github.com/htkuan/ai-agent-bridge/commit/b24d0d8e0a83829a4f10f3a9e9b87a981ae7cbda))

- Extend the fake cli with process-behaviour steps
  ([`2d2ad05`](https://github.com/htkuan/ai-agent-bridge/commit/2d2ad05b8c0855a2ae34121254f6261dea1a007d))

- Mirror the src layout in the test tree
  ([`53fe56f`](https://github.com/htkuan/ai-agent-bridge/commit/53fe56fc20e12e5422630ee2ba425355ad13ddc0))

- Pin fakes to real behaviour with contract suites
  ([`ead4054`](https://github.com/htkuan/ai-agent-bridge/commit/ead4054c4067f984cd0adf680dcae280be340c16))


## v0.3.0 (2026-08-09)

### Build System

- Add pytest-cov with a 75% coverage ratchet
  ([`57c0154`](https://github.com/htkuan/ai-agent-bridge/commit/57c0154653cc14860d5ca1c6eb9f9820cef4251e))

- Clear known vulnerabilities from the locked dependency set
  ([`c1d23b0`](https://github.com/htkuan/ai-agent-bridge/commit/c1d23b0c441a0b4c3da27d05aa11625b77de5e8a))

### Continuous Integration

- Audit locked dependencies with pip-audit
  ([`f973580`](https://github.com/htkuan/ai-agent-bridge/commit/f973580fce3c8446bb18b3e7b50177ed3bf4ee1f))

- Scan for secrets with gitleaks
  ([`a7dd7d8`](https://github.com/htkuan/ai-agent-bridge/commit/a7dd7d895c03b8e43ce2e6777e625931696a6113))

### Documentation

- Document secrets scanning in the developer guide
  ([`d41ee2f`](https://github.com/htkuan/ai-agent-bridge/commit/d41ee2fc3cfa17c5bb91c9306e545a4b8789a281))

- Document the coverage gate in the developer guide
  ([`50b76fd`](https://github.com/htkuan/ai-agent-bridge/commit/50b76fdc0b3c4c8e1e8b9a8205a3f842dc264802))

- Document the dependency audit in the developer guide
  ([`905b567`](https://github.com/htkuan/ai-agent-bridge/commit/905b5674bef637ccf88e6f20d2c7aa176e2c3d5b))

- Note that no complexity exemptions remain
  ([`1eb462a`](https://github.com/htkuan/ai-agent-bridge/commit/1eb462aa543d1c56b4470a3b6acc393a3b147369))

### Features

- **claude**: Configurable cli executable path
  ([`614dc11`](https://github.com/htkuan/ai-agent-bridge/commit/614dc11d14a49ad970c6e48a5fbdef34fd7b6312))

### Refactoring

- Decompose main startup wiring
  ([`8cbf71b`](https://github.com/htkuan/ai-agent-bridge/commit/8cbf71b48e8765e68542505e2021154c37b49eb3))

- Decompose slack message intake and stream rendering
  ([`0b32ffa`](https://github.com/htkuan/ai-agent-bridge/commit/0b32ffaf691da007ae183a5ffebbb02c536ff29a))

- Extract dedupe claim and release helpers in bridge
  ([`d36439d`](https://github.com/htkuan/ai-agent-bridge/commit/d36439dcd3e7dc0a7393aa0353b2301292bac45c))

- Route adapters through a MessageRouter protocol
  ([`61a1831`](https://github.com/htkuan/ai-agent-bridge/commit/61a1831ff66f7ba79d2832960aadec4c22bbc85d))

- Split parse_stream_line by event type
  ([`8e5b234`](https://github.com/htkuan/ai-agent-bridge/commit/8e5b234c2192db6fe369e973be3b0a0dbf397429))


## v0.2.2 (2026-08-08)

### Bug Fixes

- **agents**: Drop dead work_dir param breaking the controller protocol
  ([`d22bc94`](https://github.com/htkuan/ai-agent-bridge/commit/d22bc943aff8a427282bbee67722f5033c18dbde))

### Build System

- Add pyright strict type checking config
  ([`1c074fc`](https://github.com/htkuan/ai-agent-bridge/commit/1c074fcf7efe290c16905bf0cad17265e8348b02))

- Add ruff as dev dependency with lint and format config
  ([`8c4960e`](https://github.com/htkuan/ai-agent-bridge/commit/8c4960e3eebdb7b6eda6d048a41d72fbeab04589))

- Refresh uv.lock after 0.2.1 release
  ([`1e59067`](https://github.com/htkuan/ai-agent-bridge/commit/1e59067b10ec055e087294400b79b751ae538683))

- Refresh uv.lock during semantic-release version bump
  ([`8bb7157`](https://github.com/htkuan/ai-agent-bridge/commit/8bb7157fdccb307ec2936c080f1d5d53b044e176))

### Code Style

- Apply ruff formatting and lint fixes
  ([`e8344dd`](https://github.com/htkuan/ai-agent-bridge/commit/e8344dd7427d0676de4607d4d904d035a32b954b))

### Continuous Integration

- Run pyright in ci and pre-commit
  ([`dd6fc25`](https://github.com/htkuan/ai-agent-bridge/commit/dd6fc254269756fd5239a79227f81fdff8c7cdb1))

- Run ruff lint and format checks in ci and pre-commit
  ([`da7cda5`](https://github.com/htkuan/ai-agent-bridge/commit/da7cda543b83a1231fd6baf3e4f50efd9d1fa276))

### Documentation

- Document pyright workflow in developer guide
  ([`14c1007`](https://github.com/htkuan/ai-agent-bridge/commit/14c10071dfbcd712472869e5d6a589eed1be9f44))

- Document ruff workflow in developer guide
  ([`fd9c247`](https://github.com/htkuan/ai-agent-bridge/commit/fd9c24781c009cc52123024f0c5e1ffd6ca66ef4))

- Mark mixed json/python example block as text
  ([`ff8a733`](https://github.com/htkuan/ai-agent-bridge/commit/ff8a733245fc4ba213995488c65bc1183fada424))

### Refactoring

- Annotate implicit any and bare generic types
  ([`1f54916`](https://github.com/htkuan/ai-agent-bridge/commit/1f549161ec0e11bd7bf752bfa8c5788469b345d2))

- Resolve pyright strict findings
  ([`87ddf51`](https://github.com/htkuan/ai-agent-bridge/commit/87ddf5101c995f3373958a239342303958df23a9))


## v0.2.1 (2026-08-08)

### Bug Fixes

- Refresh stale uv.lock after 0.2.0 version bump
  ([`f335e3d`](https://github.com/htkuan/ai-agent-bridge/commit/f335e3d8b256e4efe53dfbc96390b53640e030de))

### Continuous Integration

- Pin setup-uv to full v9.0.0 tag
  ([`1a9acef`](https://github.com/htkuan/ai-agent-bridge/commit/1a9acef76cfbd5c799ddb2f327f230b1aea7797c))

- Run tests on prs and pushes to main
  ([`100efe3`](https://github.com/htkuan/ai-agent-bridge/commit/100efe3e03f97511e07acc3689358405d570b7dd))


## v0.2.0 (2026-06-05)

### Continuous Integration

- Automate releases from Conventional Commits
  ([`7206ec4`](https://github.com/htkuan/ai-agent-bridge/commit/7206ec46879e21ab0a41fe1dadb11244efa3c5cc))

### Documentation

- Trim one-time bootstrap from releasing guide
  ([`8558179`](https://github.com/htkuan/ai-agent-bridge/commit/8558179a8681193aaa27d6b8b6e0327c452814d5))

### Features

- Add optional startup notification after Socket Mode connects
  ([`4996cd9`](https://github.com/htkuan/ai-agent-bridge/commit/4996cd93abcc505f5277d7d555bf6e71f68dac13))


## v0.1.0 (2026-06-05)

- Initial Release

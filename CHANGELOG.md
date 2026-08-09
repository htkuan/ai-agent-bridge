# CHANGELOG

<!-- version list -->

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

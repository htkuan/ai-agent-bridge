# CHANGELOG

<!-- version list -->

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

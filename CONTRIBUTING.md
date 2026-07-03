# Contributing to Agent Bridge

Thanks for your interest in improving Agent Bridge! This guide covers the local
workflow; the design rules live in the
[architecture contract](https://htkuan.github.io/ai-agent-bridge/architecture/).
By participating you agree to our
[Code of Conduct](https://github.com/htkuan/ai-agent-bridge/blob/main/CODE_OF_CONDUCT.md).
Security issues go through
[private vulnerability reporting](https://github.com/htkuan/ai-agent-bridge/blob/main/SECURITY.md),
never public issues.

## Development setup

Prerequisites: Python **3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/htkuan/ai-agent-bridge.git
cd ai-agent-bridge
uv sync                    # runtime + dev deps (pytest, ruff, pre-commit, all platform extras)
uv run pre-commit install  # git hooks: ruff lint/format + commitlint on every commit
```

`uv sync` installs every optional platform extra, so the full test suite runs
out of the box. No tokens or credentials are needed for development — the test
suite is fully offline.

> If `pre-commit install` fails with `core.hooksPath` set, clear the override
> first: `git config --unset core.hooksPath`.

## Running tests

```bash
uv run pytest                          # everything: unit + integration (all offline)
uv run pytest -m "not integration"     # fast path: unit tests only
uv run pytest tests/unit/platforms/slack -v   # one component
```

Integration tests are marked with the `integration` marker; they wire real
components together (fake CLI → real controller → real bridge) but never touch
the network. The [testing guide](https://htkuan.github.io/ai-agent-bridge/testing/)
explains the layout, the shared helpers (`FakeAgentController`,
`install_fake_cli`, `FakeApiServer`), and the checklist for testing each kind of
component.

## Lint & format

[Ruff](https://docs.astral.sh/ruff/) handles both linting and formatting
(line length 100, rules configured in `pyproject.toml`):

```bash
uv run ruff check .            # lint (add --fix to auto-fix)
uv run ruff format .           # format in place
uv run ruff format --check .   # what CI runs
```

The pre-commit hook runs both on staged files, so an installed hook normally
keeps you clean without thinking about it.

## Commit conventions

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with
**lowercase** types — the full rules are in
[CLAUDE.md § Commits](https://github.com/htkuan/ai-agent-bridge/blob/main/CLAUDE.md#commits):

```
feat(telegram): support forum topics as sessions
fix: release dedupe slot on error
docs: clarify session TTL semantics
```

Two things people trip over:

1. **Every commit on the branch must conform**, not just the PR title — PRs are
   merged with merge commits, and both the `commitlint` CI check and the release
   tooling read individual commits.
2. **The type drives the automated release**: `feat:` cuts a MINOR release,
   `fix:`/`perf:` a PATCH, a `BREAKING CHANGE:` footer or `feat!:` a breaking
   bump (MINOR while in 0.x). `docs:`/`chore:`/`refactor:`/`test:`/`ci:` cut no
   release. Never hand-edit `[project].version` — see the
   [release process](https://htkuan.github.io/ai-agent-bridge/releasing/).

## Adding a new platform adapter or agent

Agent Bridge is built for this — a new component never requires touching the
bridge core or the other components. Read the
[architecture contract](https://htkuan.github.io/ai-agent-bridge/architecture/)
first: it defines the protocols, the event model, and the exact
`handle_message` semantics, plus a step-by-step checklist for each component
type (mirrored in
[CLAUDE.md](https://github.com/htkuan/ai-agent-bridge/blob/main/CLAUDE.md)).

In short, a new component ships as one PR containing:

- `platforms/{name}/` (`config.py` + `adapter.py`) or `agents/{name}/`
  (`config.py` + `controller.py` + `events.py`)
- A registry entry (`platforms/registry.py` / `agents/registry.py`)
- Unit + integration tests, fully offline (fake HTTP server / fake CLI — see the
  [testing guide](https://htkuan.github.io/ai-agent-bridge/testing/))
- A docs page (`docs/platforms/{name}.md` / `docs/agents/{name}.md`) and, for
  new env vars, updates to `.env.example`, the README table, the CLAUDE.md
  table, and the [configuration reference](https://htkuan.github.io/ai-agent-bridge/configuration/)

Opening a [feature request](https://github.com/htkuan/ai-agent-bridge/issues/new/choose)
before you start is a good way to confirm the design (session semantics,
connection mode, offline testability) up front.

## Documentation

Docs and code move together — the
[documentation maintenance rules](https://github.com/htkuan/ai-agent-bridge/blob/main/CLAUDE.md#documentation-maintenance)
say which page each kind of change must update. The docs site is MkDocs
Material:

```bash
uv sync --group docs
uv run mkdocs serve            # live preview at http://127.0.0.1:8000
uv run mkdocs build --strict   # what CI runs — broken links fail the build
```

## Releases

Releases are fully automated from commit messages on `main` — no manual
version bumps, tags, or PyPI uploads. Details:
[release process](https://htkuan.github.io/ai-agent-bridge/releasing/).

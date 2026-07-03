<!-- Thanks for contributing! See CONTRIBUTING.md for the full workflow. -->

## What & why

<!-- What does this PR change, and why? Link related issues: Fixes #123 -->

## Checklist

- [ ] **Conventional Commits** — every commit on this branch (not just the PR title)
      follows the format in [CLAUDE.md § Commits](https://github.com/htkuan/ai-agent-bridge/blob/main/CLAUDE.md#commits):
      lowercase type (`feat:`, `fix:`, ...), imperative subject. The `commitlint`
      check enforces this; the commit type drives the automated release.
- [ ] **Tests pass** — `uv run pytest` is green locally (the suite is fully
      offline; no tokens or network needed).
- [ ] **Lint clean** — `uv run ruff check .` and `uv run ruff format --check .`
      pass (or just let the pre-commit hook fix them).
- [ ] **Docs in sync** — per the [documentation maintenance rules](https://github.com/htkuan/ai-agent-bridge/blob/main/CLAUDE.md#documentation-maintenance):
      - platform adapter changes → `docs/platforms/{name}.md`
      - agent changes → `docs/agents/{name}.md`
      - core bridge/event/session changes → `CLAUDE.md` + `README.md`
      - new env vars → `.env.example` **and** the README table **and** the CLAUDE.md
        table (plus `docs/configuration.md` for the YAML key mapping)
- [ ] **New components ship complete** — a new platform/agent includes its config,
      unit + integration tests (offline: fake server / fake CLI), registry entry,
      and a `docs/platforms/{name}.md` / `docs/agents/{name}.md` page.

## Notes for reviewers

<!-- Anything non-obvious: design decisions, trade-offs, follow-ups. -->

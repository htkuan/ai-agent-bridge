# Releasing

Releases are **fully automated** from [Conventional Commits](https://www.conventionalcommits.org/)
by [python-semantic-release](https://python-semantic-release.readthedocs.io/) (PSR).
You never edit the version by hand — you write good commit messages, and merging
to `main` does the rest.

## How a version is chosen

PSR reads every commit since the last `v*` tag and picks the highest bump implied:

| Commit message                                    | Bump        | Example         |
|---------------------------------------------------|-------------|-----------------|
| `fix: ...` / `perf: ...`                           | **PATCH**   | 0.1.0 → 0.1.1   |
| `feat: ...`                                        | **MINOR**   | 0.1.0 → 0.2.0   |
| `feat!: ...` or a `BREAKING CHANGE:` footer        | breaking    | 0.1.0 → 0.2.0\* |
| `docs:` `chore:` `refactor:` `test:` `ci:` `style:`| none        | no release      |

\* **While in 0.x, breaking changes only bump the MINOR** (`major_on_zero = false`
in `pyproject.toml`). They will jump to a new MAJOR (→ 1.0.0, 2.0.0) only after you
either flip `major_on_zero = true` or hand-bump to `1.0.0` to declare the API stable.

### Commit format (enforced on PRs)

Types must be **lowercase** — `feat:`, not `Feat:`. The `commitlint` PR check
(`.github/workflows/commitlint.yml`) rejects non-conforming commits. Because PRs
are merged with merge commits, the **individual commits** on a branch are what PSR
reads, so each commit (not just the PR title) must conform.

#### Local commit-msg hook (catch it before pushing)

`.pre-commit-config.yaml` runs the **same** commitlint engine + `commitlint.config.mjs`
as the CI check, so what passes locally passes CI. Enable it once per clone:

```bash
uv sync                    # installs the pre-commit tool (dev group)
uv run pre-commit install  # writes .git/hooks/commit-msg (commit-msg stage)
```

`pre-commit` refuses to install if `core.hooksPath` is set. If you hit
`Cowardly refusing to install hooks with 'core.hooksPath' set`, clear the (usually
redundant) override first: `git config --unset core.hooksPath`. The Node toolchain
the hook needs is managed by pre-commit itself — no manual `npm`/Node install.

```
feat(slack): add channel allow-list gate
fix: break Claude stdout loop on result event
feat!: drop the [slack] extra in favour of [chat]

BREAKING CHANGE: importers must switch to the new extra name.
```

## The pipeline (on every push to `main`)

`.github/workflows/release.yml` runs two jobs:

1. **release** — PSR computes the next version, bumps `[project].version`, updates
   `CHANGELOG.md`, commits, tags `vX.Y.Z`, creates the GitHub Release, and builds
   the wheel + sdist (`uv build`). If no releasable commit is found, it stops here.
2. **deploy** — only if a release was cut: downloads the built distributions and
   publishes them to PyPI via **Trusted Publishing (OIDC)** — no tokens stored.

The version-bump commit PSR pushes back to `main` is made with `GITHUB_TOKEN`, which
does **not** re-trigger workflows, so there is no release loop.

## Maintainer setup

Publishing uses **PyPI Trusted Publishing (OIDC)** — no API tokens are stored. One
time, a maintainer adds a Trusted Publisher on PyPI for the project (a *pending
publisher* if the project does not exist yet, otherwise project → Manage →
Publishing) bound to this repo's `release.yml` workflow and the `pypi` environment.
Anyone forking and publishing under their own project name does the same for their
fork. If `main` is ever branch-protected, also allow the release job to push the
version-bump commit + tag (a bypass for the GitHub Actions actor, or a
`contents: write` PAT passed as `github_token`).

From here on, every `feat:` / `fix:` merged to `main` releases automatically.

## Preview locally (no changes pushed)

```bash
uv sync                                # installs python-semantic-release (dev group)
uv run semantic-release version --print   # prints the version the next release WOULD cut
uv run semantic-release version --noop     # full dry-run, no commits/tags/push
```

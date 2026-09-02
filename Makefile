# Agent Bridge — one entry point for every check, local and in CI.
#
# The rule: CI runs nothing that isn't a make target, so `make check` locally
# reproduces the merge gate exactly (.github/workflows/ci.yml calls `make lint`,
# `make typecheck`, `make test` and `make test-e2e`; audit.yml calls `make audit`).
# Tool configuration itself stays in pyproject.toml — this file only decides
# which commands run, never how they behave.
#
# Knobs (all overridable per invocation):
#   make test PYTEST_ARGS="-k session -x"   extra flags for any test target
#   make test RUN=                          already inside an activated .venv
#   make test UV=/opt/homebrew/bin/uv       a specific uv
#
# GNU Make 3.81 compatible (macOS ships 3.81): no .SHELLFLAGS, no .RECIPEPREFIX.

.DEFAULT_GOAL := help

UV ?= uv
UVX ?= uvx
# Every command goes through `uv run`, so the pinned dependency set is used
# whatever the caller's shell state. `RUN=` (empty) falls back to bare
# executables, for callers who have already activated .venv.
RUN ?= $(UV) run
PYTEST := $(RUN) pytest
PYTEST_ARGS ?=

# Coverage is on by default (pyproject addopts) and gates at fail_under. A
# marker-narrowed run undercounts and would trip that gate, so every subset
# target opts out; only the full-suite targets carry the gate.
NO_COV := --no-cov

# Highest live tier `make test-live` runs (tests/e2e/live_matrix.py): 0 spends
# nothing, 2 is pytest's own default, 3 is opt-in. `make test-live-free` pins 0.
LIVE_TIER ?= 2

# In CI, annotate the offending lines in the PR diff instead of plain text.
RUFF_OUTPUT := $(if $(GITHUB_ACTIONS),--output-format github,)

AUDIT_REQUIREMENTS := requirements-audit.txt

##@ Setup

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nAgent Bridge - make targets\n"} /^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } END { print "" }' $(MAKEFILE_LIST)

install: ## Sync the locked dependency set (fails on a stale uv.lock)
	$(UV) sync --locked

lock: ## Re-resolve uv.lock after changing pyproject dependencies
	$(UV) lock

hooks: ## Install the git pre-commit + commit-msg hooks
	$(RUN) pre-commit install

##@ Checks (non-mutating — this is what CI runs)

check: lint typecheck test test-e2e ## The merge gate: everything CI blocks a PR on
	@echo "all checks passed"

check-all: check audit secrets ## check + the security scans (audit.yml)

lint: ## Ruff lint + format check
	$(RUN) ruff check $(RUFF_OUTPUT)
	$(RUN) ruff format --check

typecheck: ## Pyright, strict, on src/ + tests/fakes + tests/contracts
	$(RUN) pyright

audit: ## Scan the locked dependency set for known vulnerabilities
	$(UV) export --format requirements-txt --no-emit-project -o $(AUDIT_REQUIREMENTS)
	$(UVX) pip-audit -r $(AUDIT_REQUIREMENTS) --disable-pip

secrets: ## Scan git history for hardcoded secrets (CI uses gitleaks-action)
	@command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks not on PATH: brew install gitleaks (or rely on the pre-commit hook)"; exit 1; }
	gitleaks git --redact -v .

##@ Tests

test: ## Unit + integration, with the coverage gate (CI version matrix)
	$(PYTEST) -m "not e2e" $(PYTEST_ARGS)

test-unit: ## Fast single-layer tests only
	$(PYTEST) -m unit $(NO_COV) $(PYTEST_ARGS)

test-integration: ## Tests that cross a process boundary (scripted CLIs)
	$(PYTEST) -m integration $(NO_COV) $(PYTEST_ARGS)

test-e2e: ## Full-stack + live-platform scenarios, no tokens (CI e2e job)
	$(PYTEST) -m "e2e and not live" $(NO_COV) $(PYTEST_ARGS)

test-live: ## Opt-in: spawns the REAL agent CLIs, spends tokens, never in CI
	$(PYTEST) -m live --live --live-tier=$(LIVE_TIER) $(NO_COV) -v $(PYTEST_ARGS)

test-live-free: ## Tier 0: real CLIs, zero tokens — run this after a CLI upgrade
	$(PYTEST) -m live --live --live-tier=0 $(NO_COV) -v $(PYTEST_ARGS)

test-all: ## The whole suite (live scenarios skip themselves without --live)
	$(PYTEST) $(PYTEST_ARGS)

coverage-html: ## Same run as `test`, writing an htmlcov/ report
	$(PYTEST) -m "not e2e" --cov-report=html $(PYTEST_ARGS)
	@echo "open htmlcov/index.html"

##@ Mutating (local only)

format: ## Ruff autofix + format in place
	$(RUN) ruff check --fix
	$(RUN) ruff format

##@ Run

run: ## Start the bridge
	$(RUN) agent-bridge

run-debug: ## Start the bridge with debug logging
	AGENT_BRIDGE_LOG_LEVEL=DEBUG $(RUN) agent-bridge

##@ Housekeeping

clean: ## Remove caches, coverage output and the audit export
	rm -rf .pytest_cache .ruff_cache .coverage .coverage.* htmlcov $(AUDIT_REQUIREMENTS)
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +

.PHONY: help install lock hooks check check-all lint typecheck audit secrets test test-unit test-integration test-e2e test-live test-live-free test-all coverage-html format run run-debug clean

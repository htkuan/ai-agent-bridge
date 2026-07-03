# Configuration

Agent Bridge supports two configuration modes that can be freely combined:

1. **Pure env mode** — everything via environment variables (optionally from a `.env` file). No YAML file needed; 100% backward compatible.
2. **YAML mode** — a nested YAML file describes all components, with environment variables still able to override any single key at deploy time.

**Precedence (low → high): built-in defaults < YAML file < environment variables.**

Env vars always win — this follows the 12-factor pattern, so a deployment can override a config file checked into a repo without editing it. An env var set to an empty string (e.g. an unfilled `KEY=` line in `.env`) is treated as *unset* and does not shadow YAML values.

Source: `src/agent_bridge/config_loader.py` (`ConfigSource`, `load_config_source`); each component config exposes `from_source(source)` plus a `from_env()` shortcut (equivalent to `from_source` with an empty YAML source).

`.env` loading happens exactly once at process startup (`app.main`), before the YAML file is read — so `.env` values are available both as overrides and as `$(VAR)` substitution sources.

## Config file discovery

Checked in order at startup:

1. CLI flag: `agent-bridge -c path/to/config.yaml` (or `--config`)
2. Env var: `AGENT_BRIDGE_CONFIG=path/to/config.yaml`
3. `./agent-bridge.yaml` in the current working directory, if it exists
4. Otherwise: pure env mode (empty YAML)

An **explicitly** specified path (CLI or env var) that does not exist raises `ValueError` at startup — fail fast, no silent fallback. A malformed file (invalid YAML, or a top level that isn't a mapping) also fails startup with a clear error.

See [`agent-bridge.example.yaml`](../agent-bridge.example.yaml) for a fully commented example.

## `$(VAR)` secret substitution

Secrets never live in the YAML file. Write `$(VAR)` inside any string value and it is replaced with `os.environ["VAR"]` when the file loads:

```yaml
platforms:
  slack:
    bot_token: $(SLACK_BOT_TOKEN)
    app_token: $(SLACK_APP_TOKEN)
```

Rules:

- Replacement applies to **every string value** in the document, at any nesting depth (including inside lists), and anywhere within the string (`https://$(HOST)/api` works).
- A referenced variable that is **not defined** fails startup with a `ValueError` listing **all** missing variables at once.
- Escape: `$$(` produces a literal `$(` — e.g. `cost is $$(unknown)` renders as `cost is $(unknown)`.
- `$something` without the `(NAME)` form is left untouched.
- No default-value syntax (`$(VAR:default)` is not supported) — put defaults in the YAML value itself or rely on the config's built-in default.

## YAML key ⇔ environment variable mapping

Every YAML key maps 1:1 to an env var (the env var always wins). Types follow env-var semantics: booleans accept `true/1/yes/on`, lists may be written as YAML lists **or** comma-separated strings.

### Global

| YAML key | Env var | Default | Description |
|----------|---------|---------|-------------|
| `log_level` | `AGENT_BRIDGE_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `agent` | `AGENT_BRIDGE_AGENT` | `claude` | Which registered agent handles messages (single active agent) |
| — | `AGENT_BRIDGE_CONFIG` | — | Path to the YAML config file (env/CLI only, not a YAML key) |

### Bridge

| YAML key | Env var | Default |
|----------|---------|---------|
| `bridge.session_store_path` | `AGENT_BRIDGE_SESSION_STORE_PATH` | `./sessions.json` |
| `bridge.session_ttl_hours` | `AGENT_BRIDGE_SESSION_TTL_HOURS` | `72` |
| `bridge.max_concurrent_sessions` | `AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS` | `5` |
| `bridge.dedupe.ttl_seconds` | `AGENT_BRIDGE_DEDUPE_TTL_SECONDS` | `0` (disabled) |
| `bridge.dedupe.max_entries` | `AGENT_BRIDGE_DEDUPE_MAX_ENTRIES` | `512` |
| `bridge.dedupe.simhash_threshold` | `AGENT_BRIDGE_DEDUPE_SIMHASH_THRESHOLD` | `0` (exact match only) |

### Slack platform

Slack activates when both tokens resolve; otherwise the adapter is disabled with a log line.

| YAML key | Env var | Default |
|----------|---------|---------|
| `platforms.slack.bot_token` | `AGENT_BRIDGE_SLACK_BOT_TOKEN` | — (required) |
| `platforms.slack.app_token` | `AGENT_BRIDGE_SLACK_APP_TOKEN` | — (required) |
| `platforms.slack.startup_notify_channel` | `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL` | — |
| `platforms.slack.startup_notify_message` | `AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE` | — |
| `platforms.slack.allow_channels` | `AGENT_BRIDGE_SLACK_ALLOW_CHANNELS` | — (allow all) |
| `platforms.slack.channel_not_allowed_message` | `AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE` | built-in English notice |
| `platforms.slack.usage_report.enabled` | `AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED` | `false` |
| `platforms.slack.usage_report.template` | `AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE` | built-in layout |

### Telegram platform

Telegram activates when the bot token resolves; otherwise the adapter is disabled with a log line. Requires the `telegram` extra (`pip install ai-agent-bridge[telegram]`).

| YAML key | Env var | Default |
|----------|---------|---------|
| `platforms.telegram.bot_token` | `AGENT_BRIDGE_TELEGRAM_BOT_TOKEN` | — (required) |
| `platforms.telegram.allow_chats` | `AGENT_BRIDGE_TELEGRAM_ALLOW_CHATS` | — (allow all) |
| `platforms.telegram.poll_timeout_seconds` | `AGENT_BRIDGE_TELEGRAM_POLL_TIMEOUT_SECONDS` | `30` |
| `platforms.telegram.state_path` | `AGENT_BRIDGE_TELEGRAM_STATE_PATH` | `./telegram.json` |
| `platforms.telegram.api_base_url` | `AGENT_BRIDGE_TELEGRAM_API_BASE_URL` | `https://api.telegram.org` |

### LINE platform

LINE activates when both the channel secret and the channel access token resolve; otherwise the adapter is disabled with a log line. Requires the `line` extra (`pip install ai-agent-bridge[line]`) and a public HTTPS URL in front of the webhook server.

| YAML key | Env var | Default |
|----------|---------|---------|
| `platforms.line.channel_secret` | `AGENT_BRIDGE_LINE_CHANNEL_SECRET` | — (required) |
| `platforms.line.channel_access_token` | `AGENT_BRIDGE_LINE_CHANNEL_ACCESS_TOKEN` | — (required) |
| `platforms.line.webhook.host` | `AGENT_BRIDGE_LINE_WEBHOOK_HOST` | `0.0.0.0` |
| `platforms.line.webhook.port` | `AGENT_BRIDGE_LINE_WEBHOOK_PORT` | `8080` |
| `platforms.line.webhook.path` | `AGENT_BRIDGE_LINE_WEBHOOK_PATH` | `/line/webhook` |
| `platforms.line.api_base_url` | `AGENT_BRIDGE_LINE_API_BASE_URL` | `https://api.line.me` |

### POST API platform

The POST API adapter requires an explicit `enabled: true` (auth is optional, so there is no secret to infer activation from). Requires the `api` extra (`pip install ai-agent-bridge[api]`). Binds loopback by default — see [docs/platforms/api.md](platforms/api.md) for the security notes.

| YAML key | Env var | Default |
|----------|---------|---------|
| `platforms.api.enabled` | `AGENT_BRIDGE_API_ENABLED` | `false` |
| `platforms.api.host` | `AGENT_BRIDGE_API_HOST` | `127.0.0.1` |
| `platforms.api.port` | `AGENT_BRIDGE_API_PORT` | `8081` |
| `platforms.api.auth_token` | `AGENT_BRIDGE_API_AUTH_TOKEN` | — (no auth) |

### Heartbeat platform

Heartbeat requires an explicit `enabled: true` (there is no secret to infer activation from).

| YAML key | Env var | Default |
|----------|---------|---------|
| `platforms.heartbeat.enabled` | `AGENT_BRIDGE_HEARTBEAT_ENABLED` | `false` |
| `platforms.heartbeat.interval_minutes` | `AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES` | — (required when enabled) |
| `platforms.heartbeat.prompt` | `AGENT_BRIDGE_HEARTBEAT_PROMPT` | — (required when enabled) |
| `platforms.heartbeat.state_path` | `AGENT_BRIDGE_HEARTBEAT_STATE_PATH` | `./heartbeat.json` |

### Claude agent

| YAML key | Env var | Default |
|----------|---------|---------|
| `agents.claude.work_dir` | `AGENT_BRIDGE_CLAUDE_WORK_DIR` | `.` |
| `agents.claude.permission_mode` | `AGENT_BRIDGE_CLAUDE_PERMISSION_MODE` | `acceptEdits` |
| `agents.claude.timeout_seconds` | `AGENT_BRIDGE_CLAUDE_TIMEOUT_SECONDS` | `600` |
| `agents.claude.worktree_enabled` | `AGENT_BRIDGE_CLAUDE_WORKTREE_ENABLED` | `false` |
| `agents.claude.effort` | `AGENT_BRIDGE_CLAUDE_EFFORT` | `xhigh` (`low`\|`medium`\|`high`\|`xhigh`\|`max`) |

`ANTHROPIC_API_KEY` is consumed directly by the Claude Code CLI (not by Agent Bridge) and has no YAML key.

### Codex agent

Active only when selected via `agent: codex` / `AGENT_BRIDGE_AGENT=codex`. Requires the [Codex CLI](agents/codex.md#prerequisites) installed and authenticated on the host.

| YAML key | Env var | Default |
|----------|---------|---------|
| `agents.codex.work_dir` | `AGENT_BRIDGE_CODEX_WORK_DIR` | `.` |
| `agents.codex.model` | `AGENT_BRIDGE_CODEX_MODEL` | — (CLI default) |
| `agents.codex.sandbox` | `AGENT_BRIDGE_CODEX_SANDBOX` | `workspace-write` (`read-only`\|`workspace-write`\|`danger-full-access`) |
| `agents.codex.timeout_seconds` | `AGENT_BRIDGE_CODEX_TIMEOUT_SECONDS` | `600` |
| `agents.codex.session_map_path` | `AGENT_BRIDGE_CODEX_SESSION_MAP_PATH` | `./codex-sessions.json` |

## Adding config for a new component

1. Give the component a frozen dataclass config with `from_source(source)` and a delegating `from_env()`.
2. Read every field through `source.get(env_key, yaml_path, default)` — one env var and one dotted YAML path per field:
   - platforms: `platforms.{name}.{field}` ⇔ `AGENT_BRIDGE_{NAME}_{FIELD}`
   - agents: `agents.{name}.{field}` ⇔ `AGENT_BRIDGE_{NAME}_{FIELD}`
3. Validate in `_validate()` and raise `ValueError` with the env var name in the message — fail fast at startup.
4. Add the new keys to this table, `.env.example`, `agent-bridge.example.yaml`, and the README/CLAUDE.md env tables.

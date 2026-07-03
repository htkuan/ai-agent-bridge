# Getting Started

Get a chat platform talking to an AI agent in about five minutes.

## Prerequisites

- **Python 3.12+**
- The CLI for the agent you want to drive, installed and authenticated:
    - [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (default agent), or
    - [OpenAI Codex CLI](https://developers.openai.com/codex) (`AGENT_BRIDGE_AGENT=codex`), or
    - [OpenCode CLI](https://opencode.ai) (`AGENT_BRIDGE_AGENT=opencode`)
- Credentials for at least one platform (e.g. Slack bot + app tokens) — each
  platform page documents its setup

## Install

=== "From PyPI"

    ```bash
    pip install "ai-agent-bridge[all]"     # every platform extra
    pip install "ai-agent-bridge[slack]"   # or just the ones you use:
                                           # slack / telegram / line / api
    ```

=== "From source (uv)"

    ```bash
    git clone https://github.com/htkuan/ai-agent-bridge.git
    cd ai-agent-bridge
    uv sync        # installs all extras + dev tools
    ```

The Heartbeat platform and the agent controllers have no extra dependencies.

## Configure

Configuration merges three sources — **env vars > YAML file > built-in
defaults** — so you can start with either mode and mix them freely. The
[configuration reference](configuration.md) documents every key in both forms.

=== "Env / .env mode"

    Set env vars directly, or copy the template
    ([`.env.example`](https://github.com/htkuan/ai-agent-bridge/blob/main/.env.example))
    and fill in what you use:

    ```bash
    cp .env.example .env
    ```

    ```bash title=".env"
    # Slack (see the Slack page for creating the app)
    AGENT_BRIDGE_SLACK_BOT_TOKEN=xoxb-your-bot-token
    AGENT_BRIDGE_SLACK_APP_TOKEN=xapp-your-app-level-token

    # Which agent handles messages (claude / codex / opencode)
    AGENT_BRIDGE_AGENT=claude
    # Where the agent works
    AGENT_BRIDGE_CLAUDE_WORK_DIR=/path/to/your/project
    ```

=== "YAML mode"

    One nested file for all components, with secrets pulled from the
    environment via `$(VAR)` — safe to commit:

    ```bash
    cp agent-bridge.example.yaml agent-bridge.yaml   # auto-discovered in cwd
    ```

    ```yaml title="agent-bridge.yaml"
    agent: claude

    platforms:
      slack:
        bot_token: $(SLACK_BOT_TOKEN)   # substituted from the environment
        app_token: $(SLACK_APP_TOKEN)

    agents:
      claude:
        work_dir: /path/to/your/project
    ```

    Point at a different file with `agent-bridge -c path/to/file.yaml` or
    `AGENT_BRIDGE_CONFIG=...`. Any single key can still be overridden by its
    env var at deploy time.

### Platform setup guides

Each platform needs its own credentials/registration — follow the page for the
one you're connecting:

| Platform | You need | Guide |
|----------|----------|-------|
| Slack | A Slack app with Socket Mode (`xoxb-` + `xapp-` tokens) | [Slack setup](platforms/slack.md#setup) |
| Telegram | A bot token from @BotFather | [Telegram](platforms/telegram.md) |
| LINE | A Messaging API channel + public HTTPS webhook | [LINE](platforms/line.md) |
| POST API | Nothing — enable it and optionally set a bearer token | [POST API](platforms/api.md) |
| Heartbeat | Nothing — enable it with an interval and a prompt | [Heartbeat](platforms/heartbeat.md) |

Platforms are independent: configure one or several, and any platform left
unconfigured simply stays off.

## Run

```bash
agent-bridge              # pip install
uv run agent-bridge       # from a source checkout
```

The log shows which platforms started and which agent is active. Then talk to
it — for example on Slack:

| Action | How |
|--------|-----|
| Channel | `@AgentBridge help me refactor this function` |
| DM | Send a direct message to the bot |
| Continue conversation | Reply in the same thread |
| Attach files | Upload files in the message — the agent receives download URLs |

Each thread (or Telegram topic / LINE chat / API `session` id) is one agent
session: the agent keeps context within it, and sessions expire after a
configurable TTL (default 72 h).

## Next steps

- Tune concurrency, session TTL, dedupe, and timeouts — [Configuration](configuration.md)
- Understand the event flow end to end — [Architecture](architecture.md)
- Run the agent inside isolated git worktrees — [Claude Code § Worktree Mode](agents/claude.md#worktree-mode)
- Fire scheduled prompts (cron-style automation) — [Heartbeat](platforms/heartbeat.md)

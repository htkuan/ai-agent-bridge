# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Report them privately via **GitHub Security Advisories**:
[github.com/htkuan/ai-agent-bridge/security/advisories/new](https://github.com/htkuan/ai-agent-bridge/security/advisories/new)
("Report a vulnerability"). You'll get a response as soon as possible, normally
within a few days. Please include:

- A description of the issue and its impact
- Steps to reproduce (a minimal config helps — redact your own tokens)
- Affected version(s) / commit

You'll be credited in the advisory unless you prefer otherwise.

## Supported versions

Agent Bridge is pre-1.0. Only the **latest 0.x release** receives security fixes:

| Version | Supported |
|---------|-----------|
| latest 0.x | ✅ |
| older releases | ❌ — upgrade to the latest release |

## Scope notes

Agent Bridge spawns AI-agent CLIs (`claude`, `codex`, `opencode`) that can read
and modify the configured working directory and execute tools according to their
own permission settings. Running it means trusting the connected chat platform's
allowed users with that capability — restrict who can reach the bot
(channel/chat allow-lists, API bearer token) and scope `*_WORK_DIR` and the
agent's permission/sandbox mode accordingly. Reports about the consequences of
deliberately permissive configuration (e.g. `danger-full-access` sandbox) are
generally not treated as vulnerabilities, but hardening suggestions are welcome
as regular issues.

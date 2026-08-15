from __future__ import annotations

# The Slack update throttle is a SlackConfig field; tests/e2e/stack.py builds
# every stack with a shrunk one so e2e turns don't idle 1.5s each. Throttle
# behaviour itself is pinned by tests/platforms/slack/test_rendering.py.

from __future__ import annotations

import pytest

import agent_bridge.platforms.slack.adapter as slack_adapter


@pytest.fixture(autouse=True)
def fast_render_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the Slack update throttle so e2e turns don't idle 1.5s each.

    Throttle behaviour itself is pinned by unit tests in
    tests/platforms/slack/test_rendering.py; here it only slows the suite.
    """
    monkeypatch.setattr(slack_adapter, "UPDATE_THROTTLE_SECONDS", 0.05)

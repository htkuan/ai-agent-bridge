"""WebhookConfig: env parsing and value checks."""

from __future__ import annotations

import pytest

from agent_bridge.platforms.webhook.config import (
    DEFAULT_CALLBACK_RETRY_DELAYS,
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    DEFAULT_IDLE_STATE_SECONDS,
    WebhookConfig,
)

# --- from_env / from_env_optional ---


def test_optional_is_none_when_not_enabled():
    assert WebhookConfig.from_env_optional({}) is None
    assert (
        WebhookConfig.from_env_optional({"AGENT_BRIDGE_WEBHOOK_ENABLED": "false"})
        is None
    )


def test_optional_enabled_returns_config():
    config = WebhookConfig.from_env_optional(
        {
            "AGENT_BRIDGE_WEBHOOK_ENABLED": "true",
            "AGENT_BRIDGE_WEBHOOK_TOKEN": "s3cret",
        }
    )
    assert config == WebhookConfig(token="s3cret")


def test_enabled_without_token_raises():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_WEBHOOK_TOKEN"):
        WebhookConfig.from_env_optional({"AGENT_BRIDGE_WEBHOOK_ENABLED": "true"})


def test_env_defaults_match_dataclass_defaults():
    env = {"AGENT_BRIDGE_WEBHOOK_TOKEN": "t"}
    assert WebhookConfig.from_env(env) == WebhookConfig(token="t")
    config = WebhookConfig(token="t")
    assert config.callback_timeout_seconds == DEFAULT_CALLBACK_TIMEOUT_SECONDS
    assert config.callback_retry_delays == DEFAULT_CALLBACK_RETRY_DELAYS
    assert config.idle_state_seconds == DEFAULT_IDLE_STATE_SECONDS


# --- validation ---


def test_nonpositive_callback_timeout_raises():
    with pytest.raises(ValueError, match="callback_timeout_seconds"):
        WebhookConfig(token="t", callback_timeout_seconds=0)


def test_negative_retry_delay_raises():
    with pytest.raises(ValueError, match="callback_retry_delays"):
        WebhookConfig(token="t", callback_retry_delays=(1.0, -0.5))


def test_empty_retry_delays_are_allowed():
    config = WebhookConfig(token="t", callback_retry_delays=())
    assert config.callback_retry_delays == ()


def test_nonpositive_idle_state_raises():
    with pytest.raises(ValueError, match="idle_state_seconds"):
        WebhookConfig(token="t", idle_state_seconds=0)

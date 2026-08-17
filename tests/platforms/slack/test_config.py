"""SlackConfig: env parsing plus the on/off decision behind ``from_env_optional``."""

from __future__ import annotations

import pytest

from agent_bridge.platforms.slack.config import (
    DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE,
    DEFAULT_MSG_MAX_BYTES,
    DEFAULT_UPDATE_THROTTLE_SECONDS,
    SlackConfig,
)

_TOKENS = {
    "AGENT_BRIDGE_SLACK_BOT_TOKEN": "xoxb-x",
    "AGENT_BRIDGE_SLACK_APP_TOKEN": "xapp-x",
}


# --- from_env_optional: is Slack configured at all? ---


def test_absent_tokens_mean_not_configured():
    assert SlackConfig.from_env_optional({}) is None


def test_blank_tokens_mean_not_configured():
    assert (
        SlackConfig.from_env_optional(
            {"AGENT_BRIDGE_SLACK_BOT_TOKEN": "", "AGENT_BRIDGE_SLACK_APP_TOKEN": ""}
        )
        is None
    )


@pytest.mark.parametrize("present", list(_TOKENS))
def test_half_configured_slack_is_an_error_not_an_opt_out(present: str):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_SLACK_BOT_TOKEN"):
        SlackConfig.from_env_optional({present: _TOKENS[present]})


def test_both_tokens_build_a_config():
    config = SlackConfig.from_env_optional(_TOKENS)
    assert config is not None
    assert config.bot_token == "xoxb-x"


# --- from_env: field parsing ---


def test_defaults():
    config = SlackConfig.from_env(_TOKENS)
    assert config.startup_notify_channel is None
    assert config.startup_notify_message is None
    assert config.allow_channels == frozenset()
    assert config.channel_not_allowed_message == DEFAULT_CHANNEL_NOT_ALLOWED_MESSAGE
    assert config.usage_report_enabled is False
    assert config.usage_report_template is None
    assert config.update_throttle_seconds == DEFAULT_UPDATE_THROTTLE_SECONDS
    assert config.msg_max_bytes == DEFAULT_MSG_MAX_BYTES


def test_reads_all_variables():
    config = SlackConfig.from_env(
        {
            **_TOKENS,
            "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_CHANNEL": "C123",
            "AGENT_BRIDGE_SLACK_STARTUP_NOTIFY_MESSAGE": "up",
            "AGENT_BRIDGE_SLACK_ALLOW_CHANNELS": " #Ops-Alerts , team-eng ,, ",
            "AGENT_BRIDGE_SLACK_CHANNEL_NOT_ALLOWED_MESSAGE": "nope",
            "AGENT_BRIDGE_SLACK_USAGE_REPORT_ENABLED": "yes",
            "AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE": "{cost_usd}",
        }
    )
    assert config.startup_notify_channel == "C123"
    assert config.startup_notify_message == "up"
    assert config.allow_channels == frozenset({"ops-alerts", "team-eng"})
    assert config.channel_not_allowed_message == "nope"
    assert config.usage_report_enabled is True
    assert config.usage_report_template == "{cost_usd}"


def test_blank_allow_channels_means_allow_all():
    config = SlackConfig.from_env({**_TOKENS, "AGENT_BRIDGE_SLACK_ALLOW_CHANNELS": ""})
    assert config.allow_channels == frozenset()


def test_blank_usage_template_is_none():
    config = SlackConfig.from_env(
        {**_TOKENS, "AGENT_BRIDGE_SLACK_USAGE_REPORT_TEMPLATE": ""}
    )
    assert config.usage_report_template is None


# --- validation ---


def test_missing_token_rejected_on_construction():
    with pytest.raises(ValueError, match="AGENT_BRIDGE_SLACK_BOT_TOKEN"):
        SlackConfig(bot_token="", app_token="xapp-x")


@pytest.mark.parametrize(
    ("field", "value"),
    [("update_throttle_seconds", 0), ("msg_max_bytes", 0)],
)
def test_rendering_knobs_must_be_positive(field: str, value: float):
    with pytest.raises(ValueError, match=field):
        SlackConfig(bot_token="xoxb-x", app_token="xapp-x", **{field: value})


# --- channel_profiles: the [slack.channel_profiles] section + lookups ---


def test_channel_profiles_default_empty():
    assert SlackConfig.from_env(_TOKENS).channel_profiles == {}


def test_channel_profiles_from_data_normalizes_keys_and_trims_values():
    mapping = SlackConfig.channel_profiles_from_data(
        {" #Ops-Alerts ": " ops ", "team-eng": "eng"}
    )
    assert mapping == {"ops-alerts": "ops", "team-eng": "eng"}


def test_channel_profiles_from_data_empty_is_empty():
    assert SlackConfig.channel_profiles_from_data({}) == {}


@pytest.mark.parametrize("value", [5, "", "   ", None])
def test_channel_profiles_from_data_rejects_bad_values(value: object):
    with pytest.raises(ValueError, match="non-empty profile name"):
        SlackConfig.channel_profiles_from_data({"general": value})


def test_channel_profiles_from_data_rejects_blank_channel_name():
    with pytest.raises(ValueError, match="blank channel name"):
        SlackConfig.channel_profiles_from_data({" # ": "ops"})


def test_channel_profiles_from_data_rejects_normalized_duplicates():
    with pytest.raises(ValueError, match="more than once"):
        SlackConfig.channel_profiles_from_data({"Ops": "a", "#ops": "b"})


def test_profile_for_channel_normalizes_the_lookup_side():
    config = SlackConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        channel_profiles={"ops-alerts": "ops"},
    )
    assert config.profile_for_channel("#Ops-Alerts") == "ops"
    assert config.profile_for_channel("ops-alerts") == "ops"
    assert config.profile_for_channel("general") is None


def test_mapped_channel_outside_allow_list_is_dead_config():
    with pytest.raises(ValueError, match="not in AGENT_BRIDGE_SLACK_ALLOW_CHANNELS"):
        SlackConfig(
            bot_token="xoxb-x",
            app_token="xapp-x",
            allow_channels=frozenset({"general"}),
            channel_profiles={"ops-alerts": "ops"},
        )


def test_mapped_channel_inside_allow_list_is_fine():
    config = SlackConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        allow_channels=frozenset({"general", "ops-alerts"}),
        channel_profiles={"ops-alerts": "ops"},
    )
    assert config.profile_for_channel("ops-alerts") == "ops"


def test_empty_allow_list_permits_any_mapping():
    config = SlackConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        channel_profiles={"anything": "ops"},
    )
    assert config.profile_for_channel("anything") == "ops"

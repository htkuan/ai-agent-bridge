"""AppConfig: the aggregate every component config hangs off."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import config as app_config
from agent_bridge.agents.claude.config import ClaudeConfig
from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.bridge.config import BridgeConfig
from agent_bridge.config import DEFAULT_CLEANUP_INTERVAL_SECONDS, AppConfig
from agent_bridge.platforms.heartbeat.config import HeartbeatConfig
from agent_bridge.platforms.slack.config import SlackConfig
from agent_bridge.platforms.webhook.config import WebhookConfig
from agent_bridge.server.config import HttpConfig


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"AGENT_BRIDGE_CLAUDE_WORK_DIR": str(tmp_path), **extra}


# --- from_env: aggregation ---


def test_from_env_defaults(tmp_path: Path):
    config = AppConfig.from_env(_env(tmp_path))
    assert config.bridge == BridgeConfig()
    assert config.claude == ClaudeConfig(work_dir=tmp_path.resolve())
    assert config.slack is None
    assert config.heartbeat is None
    assert config.webhook is None
    assert config.http is None
    assert config.log_level == "INFO"
    assert config.cleanup_interval_seconds == DEFAULT_CLEANUP_INTERVAL_SECONDS


def test_from_env_wires_every_layer(tmp_path: Path):
    config = AppConfig.from_env(
        _env(
            tmp_path,
            AGENT_BRIDGE_MAX_CONCURRENT_SESSIONS="3",
            AGENT_BRIDGE_CLAUDE_EFFORT="low",
            AGENT_BRIDGE_SLACK_BOT_TOKEN="xoxb-x",
            AGENT_BRIDGE_SLACK_APP_TOKEN="xapp-x",
            AGENT_BRIDGE_HEARTBEAT_ENABLED="true",
            AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES="5",
            AGENT_BRIDGE_HEARTBEAT_PROMPT="tick",
            AGENT_BRIDGE_WEBHOOK_ENABLED="true",
            AGENT_BRIDGE_WEBHOOK_TOKEN="s3cret",
            AGENT_BRIDGE_HTTP_ENABLED="true",
            AGENT_BRIDGE_HTTP_PORT="9000",
            AGENT_BRIDGE_LOG_LEVEL="debug",
        )
    )
    assert config.bridge.router.max_concurrent_sessions == 3
    assert config.claude.effort == "low"
    assert config.slack == SlackConfig(bot_token="xoxb-x", app_token="xapp-x")
    assert config.heartbeat == HeartbeatConfig(interval_minutes=5, prompt="tick")
    assert config.webhook == WebhookConfig(token="s3cret")
    assert config.http == HttpConfig(port=9000)
    assert config.log_level == "DEBUG"


def test_env_defaults_match_dataclass_defaults(tmp_path: Path):
    assert AppConfig.from_env(_env(tmp_path)) == AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path.resolve())
    )


def test_a_layer_error_surfaces_from_from_env(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_SESSION_TTL_HOURS"):
        AppConfig.from_env(_env(tmp_path, AGENT_BRIDGE_SESSION_TTL_HOURS="0"))


def test_process_env_is_the_only_source_that_loads_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    loads: list[bool] = []
    monkeypatch.setattr(app_config, "load_env_file", lambda: loads.append(True))
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_WORK_DIR", str(tmp_path))

    AppConfig.from_env()  # no mapping → reads os.environ, overlaid by .env
    AppConfig.from_env(_env(tmp_path))  # explicit mapping → no .env overlay

    assert loads == [True]


# --- validation ---


def test_rejects_unknown_log_level(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_LOG_LEVEL"):
        AppConfig(claude=ClaudeConfig(work_dir=tmp_path), log_level="CHATTY")


def test_rejects_non_positive_cleanup_interval(tmp_path: Path):
    with pytest.raises(ValueError, match="cleanup_interval_seconds"):
        AppConfig(claude=ClaudeConfig(work_dir=tmp_path), cleanup_interval_seconds=0)


def test_claude_config_is_required():
    with pytest.raises(TypeError, match="claude"):
        AppConfig()  # pyright: ignore[reportCallIssue]


def test_webhook_without_http_server_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_ENABLED"):
        AppConfig(
            claude=ClaudeConfig(work_dir=tmp_path), webhook=WebhookConfig(token="t")
        )


def test_webhook_without_http_server_raises_from_env(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_HTTP_ENABLED"):
        AppConfig.from_env(
            _env(
                tmp_path,
                AGENT_BRIDGE_WEBHOOK_ENABLED="true",
                AGENT_BRIDGE_WEBHOOK_TOKEN="t",
            )
        )


def test_http_server_alone_is_fine(tmp_path: Path):
    config = AppConfig(claude=ClaudeConfig(work_dir=tmp_path), http=HttpConfig())
    assert config.webhook is None


# --- the profiles file (AGENT_BRIDGE_PROFILES_PATH) ---

_SLACK_TOKENS = {
    "AGENT_BRIDGE_SLACK_BOT_TOKEN": "xoxb-x",
    "AGENT_BRIDGE_SLACK_APP_TOKEN": "xapp-x",
}


def _profiles_env(tmp_path: Path, content: str, **extra: str) -> dict[str, str]:
    path = tmp_path / "profiles.toml"
    path.write_text(content)
    return _env(tmp_path, AGENT_BRIDGE_PROFILES_PATH=str(path), **extra)


def test_profiles_path_unset_means_no_profiles(tmp_path: Path):
    config = AppConfig.from_env(_env(tmp_path))
    assert config.claude_profiles == {}


def test_profiles_file_builds_named_profiles_and_slack_mapping(tmp_path: Path):
    other = tmp_path / "other"
    env = _profiles_env(
        tmp_path,
        f"""
        [claude.profiles.backend]
        work_dir = {str(other)!r}
        permission_mode = "plan"
        model = "claude-opus-5"

        [claude.profiles.docs]

        [slack.channel_profiles]
        backend-team = "backend"
        docs-help = "docs"
        """,
        **_SLACK_TOKENS,
    )
    config = AppConfig.from_env(env)

    backend = config.claude_profiles["backend"]
    assert backend.work_dir == other.resolve()
    assert backend.permission_mode == "plan"
    assert backend.model == "claude-opus-5"
    # Unset fields inherit the base (env-built) Claude config.
    assert backend.timeout_seconds == config.claude.timeout_seconds
    assert config.claude_profiles["docs"] == config.claude
    assert config.slack is not None
    assert config.slack.channel_profiles == {
        "backend-team": "backend",
        "docs-help": "docs",
    }


def test_profile_inherits_base_built_from_env(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        "[claude.profiles.backend]",
        AGENT_BRIDGE_CLAUDE_EFFORT="low",
    )
    config = AppConfig.from_env(env)
    assert config.claude_profiles["backend"].effort == "low"


def test_empty_profiles_file_is_fine(tmp_path: Path):
    config = AppConfig.from_env(_profiles_env(tmp_path, ""))
    assert config.claude_profiles == {}


def test_profiles_file_missing_raises(tmp_path: Path):
    env = _env(tmp_path, AGENT_BRIDGE_PROFILES_PATH=str(tmp_path / "nope.toml"))
    with pytest.raises(ValueError, match="AGENT_BRIDGE_PROFILES_PATH"):
        AppConfig.from_env(env)


def test_profiles_file_invalid_toml_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid TOML"):
        AppConfig.from_env(_profiles_env(tmp_path, "not = [valid"))


def test_profiles_file_unknown_section_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown section.*discord"):
        AppConfig.from_env(_profiles_env(tmp_path, "[discord]"))


def test_profiles_file_unknown_key_under_claude_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown key.*\[claude\]"):
        AppConfig.from_env(_profiles_env(tmp_path, "[claude]\nprofile = 1"))


def test_profiles_file_unknown_key_under_slack_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown key.*\[slack\]"):
        AppConfig.from_env(
            _profiles_env(tmp_path, "[slack]\nchannels = 1", **_SLACK_TOKENS)
        )


def test_profiles_file_section_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match=r"\[claude\].*must be a table"):
        AppConfig.from_env(_profiles_env(tmp_path, 'claude = "x"'))


def test_profiles_file_channel_profiles_must_be_a_table(tmp_path: Path):
    with pytest.raises(ValueError, match=r"\[slack\.channel_profiles\].*table"):
        AppConfig.from_env(
            _profiles_env(tmp_path, '[slack]\nchannel_profiles = "x"', **_SLACK_TOKENS)
        )


def test_channel_mapping_without_slack_configured_raises(tmp_path: Path):
    content = """
    [claude.profiles.backend]

    [slack.channel_profiles]
    backend-team = "backend"
    """
    with pytest.raises(ValueError, match="Slack is not configured"):
        AppConfig.from_env(_profiles_env(tmp_path, content))


def test_channel_mapping_to_unknown_profile_raises(tmp_path: Path):
    content = """
    [slack.channel_profiles]
    backend-team = "nope"
    """
    with pytest.raises(ValueError, match=r"unknown profile.*nope"):
        AppConfig.from_env(_profiles_env(tmp_path, content, **_SLACK_TOKENS))


def test_channel_mapping_to_unknown_profile_raises_on_construction(tmp_path: Path):
    # The cross-check guards programmatically assembled configs too.
    with pytest.raises(ValueError, match="unknown profile"):
        AppConfig(
            claude=ClaudeConfig(work_dir=tmp_path),
            slack=SlackConfig(
                bot_token="xoxb-x",
                app_token="xapp-x",
                channel_profiles={"general": "nope"},
            ),
        )


def test_channel_mapping_to_defined_profile_constructs(tmp_path: Path):
    config = AppConfig(
        claude=ClaudeConfig(work_dir=tmp_path),
        claude_profiles={"ops": ClaudeConfig(work_dir=tmp_path)},
        slack=SlackConfig(
            bot_token="xoxb-x",
            app_token="xapp-x",
            channel_profiles={"general": "ops"},
        ),
    )
    assert config.slack is not None
    assert config.slack.profile_for_channel("general") == "ops"


def test_profiles_file_empty_claude_section_is_fine(tmp_path: Path):
    config = AppConfig.from_env(_profiles_env(tmp_path, "[claude]"))
    assert config.claude_profiles == {}


# --- [pi.profiles]: the second agent type in the routing namespace ---


def test_profiles_path_unset_means_no_pi_profiles(tmp_path: Path):
    config = AppConfig.from_env(_env(tmp_path))
    assert config.pi_profiles == {}


def test_pi_profiles_inherit_base_built_from_env(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        """
        [pi.profiles.fast]
        model = "gpt-5.6-luna"
        """,
        AGENT_BRIDGE_PI_PROVIDER="openai-codex",
        AGENT_BRIDGE_PI_WORK_DIR=str(tmp_path),
    )
    config = AppConfig.from_env(env)
    fast = config.pi_profiles["fast"]
    assert fast.model == "gpt-5.6-luna"
    assert fast.provider == "openai-codex"
    assert fast.work_dir == tmp_path.resolve()


def test_pi_and_claude_profiles_coexist(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        """
        [claude.profiles.backend]

        [pi.profiles.fast]
        """,
        AGENT_BRIDGE_PI_WORK_DIR=str(tmp_path),
    )
    config = AppConfig.from_env(env)
    assert set(config.claude_profiles) == {"backend"}
    assert set(config.pi_profiles) == {"fast"}


def test_profile_name_collision_across_agents_raises(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        """
        [claude.profiles.bot]

        [pi.profiles.bot]
        """,
        AGENT_BRIDGE_PI_WORK_DIR=str(tmp_path),
    )
    with pytest.raises(ValueError, match=r"more than one agent.*bot"):
        AppConfig.from_env(env)


def test_profile_name_collision_raises_on_construction(tmp_path: Path):
    with pytest.raises(ValueError, match="more than one agent"):
        AppConfig(
            claude=ClaudeConfig(work_dir=tmp_path),
            claude_profiles={"bot": ClaudeConfig(work_dir=tmp_path)},
            pi_profiles={"bot": PiConfig(work_dir=tmp_path)},
        )


def test_channel_mapping_to_pi_profile_is_valid(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        """
        [pi.profiles.fast]

        [slack.channel_profiles]
        quick-qa = "fast"
        """,
        AGENT_BRIDGE_PI_WORK_DIR=str(tmp_path),
        **_SLACK_TOKENS,
    )
    config = AppConfig.from_env(env)
    assert config.slack is not None
    assert config.slack.profile_for_channel("quick-qa") == "fast"


def test_profiles_file_unknown_key_under_pi_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"Unknown key.*\[pi\]"):
        AppConfig.from_env(_profiles_env(tmp_path, "[pi]\nprofile = 1"))


# --- default_agent: where agent=None routes ---


def test_default_agent_unset_by_default(tmp_path: Path):
    assert AppConfig.from_env(_env(tmp_path)).default_agent is None


def test_default_agent_from_env(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        "[claude.profiles.backend]",
        AGENT_BRIDGE_DEFAULT_AGENT="backend",
    )
    assert AppConfig.from_env(env).default_agent == "backend"


def test_default_agent_may_reference_a_pi_profile(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        "[pi.profiles.fast]",
        AGENT_BRIDGE_DEFAULT_AGENT="fast",
        AGENT_BRIDGE_PI_WORK_DIR=str(tmp_path),
    )
    assert AppConfig.from_env(env).default_agent == "fast"


def test_default_agent_unknown_raises(tmp_path: Path):
    with pytest.raises(ValueError, match=r"AGENT_BRIDGE_DEFAULT_AGENT.*ghost"):
        AppConfig.from_env(_env(tmp_path, AGENT_BRIDGE_DEFAULT_AGENT="ghost"))


def test_default_agent_unknown_raises_on_construction(tmp_path: Path):
    with pytest.raises(ValueError, match="AGENT_BRIDGE_DEFAULT_AGENT"):
        AppConfig(claude=ClaudeConfig(work_dir=tmp_path), default_agent="ghost")


# --- heartbeat.agent: validated against the registry at boot ---


def test_heartbeat_agent_must_reference_defined_profile(tmp_path: Path):
    env = _env(
        tmp_path,
        AGENT_BRIDGE_HEARTBEAT_ENABLED="true",
        AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES="5",
        AGENT_BRIDGE_HEARTBEAT_PROMPT="tick",
        AGENT_BRIDGE_HEARTBEAT_AGENT="ghost",
    )
    with pytest.raises(ValueError, match=r"AGENT_BRIDGE_HEARTBEAT_AGENT.*ghost"):
        AppConfig.from_env(env)


def test_heartbeat_agent_referencing_defined_profile_passes(tmp_path: Path):
    env = _profiles_env(
        tmp_path,
        "[claude.profiles.night]",
        AGENT_BRIDGE_HEARTBEAT_ENABLED="true",
        AGENT_BRIDGE_HEARTBEAT_INTERVAL_MINUTES="5",
        AGENT_BRIDGE_HEARTBEAT_PROMPT="tick",
        AGENT_BRIDGE_HEARTBEAT_AGENT="night",
    )
    config = AppConfig.from_env(env)
    assert config.heartbeat is not None
    assert config.heartbeat.agent == "night"

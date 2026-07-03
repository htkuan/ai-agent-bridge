from __future__ import annotations

import pytest

from agent_bridge.config_loader import (
    ConfigSource,
    load_config_source,
    substitute_secrets,
)

# --- ConfigSource precedence ---


def test_get_returns_default_when_no_yaml_no_env():
    source = ConfigSource({}, env={})
    assert source.get("SOME_KEY", "a.b.c", "fallback") == "fallback"
    assert source.get("SOME_KEY", "a.b.c") is None


def test_get_yaml_overrides_default():
    source = ConfigSource({"a": {"b": {"c": "from-yaml"}}}, env={})
    assert source.get("SOME_KEY", "a.b.c", "fallback") == "from-yaml"


def test_get_env_overrides_yaml():
    source = ConfigSource({"a": {"b": {"c": "from-yaml"}}}, env={"SOME_KEY": "from-env"})
    assert source.get("SOME_KEY", "a.b.c", "fallback") == "from-env"


def test_get_empty_env_value_treated_as_unset():
    # .env templates ship `KEY=` placeholders; those must not shadow YAML.
    source = ConfigSource({"a": {"b": "from-yaml"}}, env={"SOME_KEY": ""})
    assert source.get("SOME_KEY", "a.b") == "from-yaml"


def test_get_stringifies_yaml_scalars():
    source = ConfigSource(
        {
            "num": 72,
            "flt": 0.5,
            "on": True,
            "off": False,
            "nothing": None,
            "names": ["ops-alerts", "team-eng"],
        },
        env={},
    )
    assert source.get("K", "num") == "72"
    assert source.get("K", "flt") == "0.5"
    assert source.get("K", "on") == "true"
    assert source.get("K", "off") == "false"
    assert source.get("K", "nothing", "dflt") == "dflt"
    assert source.get("K", "names") == "ops-alerts,team-eng"


def test_get_rejects_mapping_at_leaf():
    source = ConfigSource({"a": {"b": {"c": 1}}}, env={})
    with pytest.raises(ValueError, match=r"a\.b"):
        source.get("K", "a.b")


def test_get_missing_intermediate_path_returns_default():
    source = ConfigSource({"a": "scalar"}, env={})
    assert source.get("K", "a.b.c", "dflt") == "dflt"


# --- $(VAR) substitution ---


def test_substitute_replaces_vars_in_nested_structures():
    data = {
        "token": "$(MY_TOKEN)",
        "nested": {"url": "https://$(HOST)/api"},
        "items": ["$(A)", "plain"],
        "count": 3,
    }
    result = substitute_secrets(data, env={"MY_TOKEN": "s3cret", "HOST": "example.com", "A": "x"})
    assert result == {
        "token": "s3cret",
        "nested": {"url": "https://example.com/api"},
        "items": ["x", "plain"],
        "count": 3,
    }


def test_substitute_missing_vars_lists_all_in_error():
    data = {"a": "$(MISSING_ONE)", "b": {"c": "$(MISSING_TWO) and $(MISSING_ONE)"}}
    with pytest.raises(ValueError) as exc_info:
        substitute_secrets(data, env={})
    message = str(exc_info.value)
    assert "MISSING_ONE" in message
    assert "MISSING_TWO" in message


def test_substitute_escape_produces_literal():
    env: dict[str, str] = {"VAR": "value"}
    assert substitute_secrets("$$(VAR)", env=env) == "$(VAR)"
    assert substitute_secrets("cost: $$(a) $(VAR)", env=env) == "cost: $(a) value"


def test_substitute_ignores_non_var_dollar_usage():
    assert substitute_secrets("price is $5 (approx)", env={}) == "price is $5 (approx)"


# --- File discovery ---


def test_explicit_path_missing_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_config_source(tmp_path / "nope.yaml")


def test_env_var_path_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ValueError, match="not found"):
        load_config_source()


def test_cli_path_overrides_env_var(tmp_path, monkeypatch):
    cli_file = tmp_path / "cli.yaml"
    cli_file.write_text("log_level: DEBUG\n")
    monkeypatch.setenv("AGENT_BRIDGE_CONFIG", str(tmp_path / "nope.yaml"))
    source = load_config_source(cli_file)
    assert source.get("X", "log_level") == "DEBUG"


def test_env_var_path_used_when_no_cli(tmp_path, monkeypatch):
    config_file = tmp_path / "conf.yaml"
    config_file.write_text("agent: claude\n")
    monkeypatch.setenv("AGENT_BRIDGE_CONFIG", str(config_file))
    source = load_config_source()
    assert source.get("X", "agent") == "claude"
    assert source.path == config_file


def test_default_file_discovered_in_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agent-bridge.yaml").write_text("log_level: WARNING\n")
    source = load_config_source()
    assert source.get("X", "log_level") == "WARNING"


def test_no_file_yields_pure_env_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOME_ENV", "env-value")
    source = load_config_source()
    assert source.path is None
    assert source.get("SOME_ENV", "some.path") == "env-value"
    assert source.get("OTHER", "some.path", "dflt") == "dflt"


def test_bad_yaml_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: [unclosed\n")
    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config_source(bad)


def test_non_mapping_top_level_raises(tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- one\n- two\n")
    with pytest.raises(ValueError, match="top-level mapping"):
        load_config_source(bad)


def test_empty_file_is_valid_empty_config(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    source = load_config_source(empty)
    assert source.get("X", "anything", "dflt") == "dflt"


def test_loaded_file_substitutes_secrets(tmp_path, monkeypatch):
    config_file = tmp_path / "conf.yaml"
    config_file.write_text("platforms:\n  slack:\n    bot_token: $(TEST_BOT_TOKEN)\n")
    monkeypatch.setenv("TEST_BOT_TOKEN", "xoxb-real")
    source = load_config_source(config_file)
    assert source.get("X", "platforms.slack.bot_token") == "xoxb-real"


def test_loaded_file_missing_secret_raises(tmp_path, monkeypatch):
    config_file = tmp_path / "conf.yaml"
    config_file.write_text("platforms:\n  slack:\n    bot_token: $(NOT_DEFINED_ANYWHERE)\n")
    monkeypatch.delenv("NOT_DEFINED_ANYWHERE", raising=False)
    with pytest.raises(ValueError, match="NOT_DEFINED_ANYWHERE"):
        load_config_source(config_file)

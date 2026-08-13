from types import SimpleNamespace

from agent_bridge.bridge.events import Usage
from agent_bridge.platforms.slack.adapter import (
    _USAGE_DIVIDER,
    SlackAdapter,
    _default_usage_footer,
    _render_usage_template,
)
from agent_bridge.platforms.slack.config import SlackConfig


def _turn() -> Usage:
    return Usage(
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=300,
        cache_creation_tokens=50,
        num_turns=2,
        duration_api_ms=2500,
        duration_ms=3000,
        cost_usd=0.0123,
    )


def _session() -> Usage:
    return Usage(
        input_tokens=1000,
        output_tokens=2000,
        cache_read_tokens=3000,
        cache_creation_tokens=500,
        num_turns=5,
        duration_api_ms=20000,
        duration_ms=30000,
        cost_usd=0.0456,
    )


# --- Default layout ---


def test_default_footer_shows_turn_and_session():
    out = _default_usage_footer(_turn(), _session())
    assert "$0.0123" in out
    assert "100 in / 200 out" in out
    assert "300 cached" in out
    assert "📈 session" in out
    assert "$0.0456" in out
    assert "6500 tokens" in out  # 1000+2000+3000+500


def test_default_footer_hides_session_when_none():
    out = _default_usage_footer(_turn(), None)
    assert "$0.0123" in out
    assert "session" not in out


# --- Template substitution ---


def test_template_substitutes_turn_and_session_placeholders():
    tpl = "{cost_usd} | {total_tokens} | {session_cost_usd} | {duration_s}s"
    out = _render_usage_template(tpl, _turn(), _session())
    assert out == "0.0123 | 650 | 0.0456 | 3.0s"  # turn total 100+200+300+50


def test_template_session_placeholders_zero_when_no_session():
    out = _render_usage_template("{session_total_tokens}", _turn(), None)
    assert out == "0"


def test_template_unknown_placeholder_left_blank():
    out = _render_usage_template("a{nope}b", _turn(), None)
    assert out == "ab"


def test_template_malformed_degrades_to_raw():
    tpl = "cost {cost_usd} {oops"  # unmatched brace
    out = _render_usage_template(tpl, _turn(), None)
    assert out == tpl


# --- _build_usage_footer gating (instance method, called unbound) ---


def _stub(enabled: bool, template: str | None = None) -> SimpleNamespace:
    config = SlackConfig(
        bot_token="x",
        app_token="y",
        usage_report_enabled=enabled,
        usage_report_template=template,
    )
    return SimpleNamespace(_config=config)


def test_build_footer_empty_when_disabled():
    out = SlackAdapter._build_usage_footer(_stub(enabled=False), _turn(), _session())
    assert out == ""


def test_build_footer_empty_when_usage_none():
    out = SlackAdapter._build_usage_footer(_stub(enabled=True), None, None)
    assert out == ""


def test_build_footer_default_is_labelled_divider_plus_italic():
    out = SlackAdapter._build_usage_footer(_stub(enabled=True), _turn(), _session())
    assert out.startswith("\n\n")
    assert "$0.0123" in out
    body_lines = out.strip().split("\n")
    # Labelled divider first, then each usage line in italics.
    assert body_lines[0] == _USAGE_DIVIDER
    assert "cost" in _USAGE_DIVIDER
    for line in body_lines[1:]:
        assert line.startswith("_")
        assert line.endswith("_")


def test_build_footer_wraps_template_in_footnote():
    out = SlackAdapter._build_usage_footer(
        _stub(enabled=True, template="X{cost_usd}X"), _turn(), None
    )
    assert out == f"\n\n{_USAGE_DIVIDER}\n_X0.0123X_"

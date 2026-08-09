from agent_bridge.platforms.slack.adapter import (
    SLACK_MSG_MAX_BYTES,
    _fit_with_suffix,
    _truncate_to_bytes,
    _utf8_len,
)


def test_utf8_len_counts_bytes_not_chars():
    # Each CJK codepoint is 3 bytes in UTF-8
    assert _utf8_len("測") == 3
    assert _utf8_len("測試") == 6
    assert _utf8_len("hello") == 5


def test_truncate_preserves_short_text():
    assert _truncate_to_bytes("hello", 100) == "hello"
    assert _truncate_to_bytes("測試", 100) == "測試"


def test_truncate_drops_partial_multibyte_sequence():
    # 測 is 3 bytes; budget 5 fits one 測 (3 bytes) and must drop the next
    # partial sequence rather than emit replacement chars.
    assert _truncate_to_bytes("測測", 5) == "測"
    assert _truncate_to_bytes("測測", 3) == "測"
    assert _truncate_to_bytes("測測", 2) == ""


def test_truncate_ascii_exact_budget():
    assert _truncate_to_bytes("abcdef", 3) == "abc"


def test_truncate_zero_or_negative_budget_returns_empty():
    assert _truncate_to_bytes("hello", 0) == ""
    assert _truncate_to_bytes("hello", -5) == ""


def test_fit_with_suffix_short_enough_passes_through():
    assert _fit_with_suffix("hi", 100, " (…)") == "hi"


def test_fit_with_suffix_reserves_room_for_suffix():
    suffix = " (…)"  # 6 bytes
    # 20 bytes of text with a 10-byte budget → must truncate and still
    # leave room for the 6-byte suffix.
    result = _fit_with_suffix("a" * 20, 10, suffix)
    assert result.endswith(suffix)
    assert _utf8_len(result) <= 10


def test_fit_with_suffix_cjk_regression():
    # Regression for the 528-char stuck bug: 1334 x '測' = 4002 bytes,
    # which was passing the old char-length check (< 3900) and hitting
    # Slack's msg_too_long. The byte-based fit must trim it.
    text = "測" * 1334
    suffix = "\n\n_… (generating response…)_"
    result = _fit_with_suffix(text, SLACK_MSG_MAX_BYTES, suffix)
    assert _utf8_len(result) <= SLACK_MSG_MAX_BYTES
    assert result.endswith(suffix)


def test_fit_with_suffix_cjk_just_under_limit_untouched():
    # 1300 x '測' = 3900 bytes — exactly at the ceiling, no truncation needed.
    text = "測" * 1300
    assert _utf8_len(text) == 3900
    suffix = "\n\n_… (generating response…)_"
    assert _fit_with_suffix(text, SLACK_MSG_MAX_BYTES, suffix) == text

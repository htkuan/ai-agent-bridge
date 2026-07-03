from __future__ import annotations

import pytest

from agent_bridge.platforms.api.adapter import (
    ApiRequestError,
    merge_context,
    parse_request,
    resolve_session,
)

# --- parse_request ---


def test_minimal_body_defaults():
    request = parse_request({"text": "hello"})
    assert request.text == "hello"
    assert request.session is None
    assert request.system_prompt is None
    assert request.context is None
    assert request.stream is False


def test_full_body():
    request = parse_request(
        {
            "text": "hello",
            "session": "job-42",
            "system_prompt": "be terse",
            "context": {"caller": "cron"},
            "stream": True,
        }
    )
    assert request.session == "job-42"
    assert request.system_prompt == "be terse"
    assert request.context == {"caller": "cron"}
    assert request.stream is True


@pytest.mark.parametrize("payload", [[], "text", 42, None])
def test_non_object_body_rejected(payload):
    with pytest.raises(ApiRequestError, match="JSON object"):
        parse_request(payload)


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": ""}, {"text": "   "}, {"text": 123}, {"text": None}],
)
def test_missing_or_empty_text_rejected(payload):
    with pytest.raises(ApiRequestError, match="'text'"):
        parse_request(payload)


@pytest.mark.parametrize("session", ["", "   ", 42, ["s"]])
def test_invalid_session_rejected(session):
    with pytest.raises(ApiRequestError, match="'session'"):
        parse_request({"text": "hi", "session": session})


def test_invalid_system_prompt_rejected():
    with pytest.raises(ApiRequestError, match="'system_prompt'"):
        parse_request({"text": "hi", "system_prompt": 1})


@pytest.mark.parametrize(
    "context",
    ["not-a-map", ["a"], {"key": 1}, {"key": None}, {"key": {"nested": "x"}}],
)
def test_invalid_context_rejected(context):
    with pytest.raises(ApiRequestError, match="'context'"):
        parse_request({"text": "hi", "context": context})


@pytest.mark.parametrize("stream", ["true", 1, None])
def test_non_bool_stream_rejected(stream):
    with pytest.raises(ApiRequestError, match="'stream'"):
        parse_request({"text": "hi", "stream": stream})


# --- resolve_session ---


def test_client_session_is_resumable():
    key, resumable = resolve_session("job-42")
    assert key == "api:client:job-42"
    assert resumable is True


def test_no_session_is_oneshot():
    key, resumable = resolve_session(None)
    assert key.startswith("api:oneshot:")
    assert resumable is False


def test_oneshot_keys_are_unique_per_call():
    assert resolve_session(None)[0] != resolve_session(None)[0]


# --- merge_context ---


def test_merge_context_adds_platform_tag():
    assert merge_context(None) == {"platform": "api"}
    assert merge_context({"caller": "cron"}) == {
        "caller": "cron",
        "platform": "api",
    }


def test_merge_context_platform_tag_wins_over_client():
    # A client cannot masquerade as another platform.
    assert merge_context({"platform": "slack"}) == {"platform": "api"}

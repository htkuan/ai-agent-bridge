"""Real HTTP requests against a real ApiAdapter server (ephemeral port) →
real Bridge + SessionManager → FakeAgentController: buffered JSON and SSE
responses, auth, validation, session continuation, and capacity rejection."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from agent_bridge.bridge import Bridge
from agent_bridge.events import Completion, StatusUpdate, TextDelta
from agent_bridge.platforms.api.adapter import ApiAdapter
from agent_bridge.platforms.api.config import ApiConfig
from tests.helpers import FakeAgentController

pytestmark = pytest.mark.integration


@pytest.fixture
async def api_stack(session_manager):
    """Factory: real API server + real bridge + scripted fake agent."""
    started: list[ApiAdapter] = []

    async def _start(
        *,
        agent_events=None,
        auth_token: str = "",
        max_concurrent: int = 2,
        delay: float = 0.0,
    ):
        controller = FakeAgentController(events=agent_events, delay=delay)
        bridge = Bridge(session_manager, controller, max_concurrent=max_concurrent)
        config = ApiConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,  # ephemeral — read back via adapter.bound_port
            auth_token=auth_token,
        )
        adapter = ApiAdapter(config, bridge, session_manager=session_manager)
        await adapter.start()
        started.append(adapter)
        return adapter, controller

    yield _start

    for adapter in started:
        await adapter.stop()


def _url(adapter: ApiAdapter, path: str = "/v1/messages") -> str:
    return f"http://127.0.0.1:{adapter.bound_port}{path}"


async def _post(
    adapter: ApiAdapter, body: dict, *, headers: dict | None = None
) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as client:
        async with client.post(_url(adapter), json=body, headers=headers) as resp:
            return resp.status, await resp.json()


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        lines = block.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


# --- Buffered mode ---


async def test_buffered_full_cycle(api_stack, session_manager):
    adapter, controller = await api_stack(
        agent_events=[
            StatusUpdate(status="Running Bash", detail="ls"),
            TextDelta(text="partial"),
            Completion(
                text="the full answer",
                cost_usd=0.01,
                duration_ms=1200,
                metadata={"usage": {"input_tokens": 10, "output_tokens": 5, "num_turns": 1}},
            ),
        ],
    )

    status, body = await _post(
        adapter,
        {
            "text": "run the report",
            "session": "job-1",
            "context": {"caller": "cron"},
        },
    )

    assert status == 200
    assert body["session"] == "job-1"
    assert body["text"] == "the full answer"  # Completion.text, not the delta
    assert body["is_error"] is False
    assert body["status_updates"] == ["Running Bash: ls"]
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["total_tokens"] == 15
    assert body["usage"]["cost_usd"] == 0.01
    assert body["usage"]["duration_ms"] == 1200
    # New resumable session tracked from its first turn → running total too.
    assert body["session_usage"]["total_tokens"] == 15

    # The agent got the text verbatim (no sender tagging), the merged opaque
    # context, and the default API framing as system prompt.
    assert controller.calls == ["run the report"]
    assert controller.last_context == {"caller": "cron", "platform": "api"}
    assert "HTTP API" in controller.last_system_prompt

    # Client session persisted under the documented key.
    assert session_manager.get("api:client:job-1") is not None


async def test_client_system_prompt_passes_through(api_stack):
    adapter, controller = await api_stack()
    await _post(adapter, {"text": "hi", "system_prompt": "answer in haiku"})
    assert controller.last_system_prompt == "answer in haiku"


async def test_oneshot_without_session(api_stack):
    adapter, controller = await api_stack()

    status, body = await _post(adapter, {"text": "ping"})
    assert status == 200
    assert body["session"] is None
    assert body["text"] == "echo:ping"

    # Every call is a fresh, untracked session — no resume across POSTs.
    await _post(adapter, {"text": "ping"})
    assert [run.is_new for run in controller.runs] == [True, True]
    assert controller.runs[0].session_id != controller.runs[1].session_id


# --- SSE mode ---


async def test_sse_event_sequence(api_stack):
    adapter, _controller = await api_stack(
        agent_events=[
            StatusUpdate(status="Thinking"),
            TextDelta(text="hello "),
            TextDelta(text="world"),
            Completion(
                text="hello world",
                metadata={"usage": {"input_tokens": 3, "output_tokens": 2}},
            ),
        ],
    )

    async with aiohttp.ClientSession() as client:
        async with client.post(
            _url(adapter), json={"text": "greet", "stream": True}
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            assert resp.headers["Cache-Control"] == "no-cache"
            raw = await resp.text()

    events = _parse_sse(raw)
    assert [name for name, _ in events] == [
        "processing",
        "status",
        "text_delta",
        "text_delta",
        "completion",
    ]
    assert events[1][1] == {"status": "Thinking", "detail": ""}
    assert events[2][1] == {"text": "hello "}
    completion = events[-1][1]
    assert completion["text"] == "hello world"
    assert completion["is_error"] is False
    assert completion["usage"]["total_tokens"] == 5


# --- Auth / validation / health ---


async def test_missing_bearer_token_rejected(api_stack):
    adapter, controller = await api_stack(auth_token="s3cret")
    status, body = await _post(adapter, {"text": "hi"})
    assert status == 401
    assert "error" in body
    assert controller.runs == []


async def test_wrong_bearer_token_rejected(api_stack):
    adapter, controller = await api_stack(auth_token="s3cret")
    status, _body = await _post(
        adapter, {"text": "hi"}, headers={"Authorization": "Bearer wrong"}
    )
    assert status == 401
    assert controller.runs == []


async def test_correct_bearer_token_accepted(api_stack):
    adapter, _controller = await api_stack(auth_token="s3cret")
    status, body = await _post(
        adapter, {"text": "hi"}, headers={"Authorization": "Bearer s3cret"}
    )
    assert status == 200
    assert body["text"] == "echo:hi"


async def test_missing_text_rejected(api_stack):
    adapter, controller = await api_stack()
    status, body = await _post(adapter, {"session": "x"})
    assert status == 400
    assert "'text'" in body["error"]
    assert controller.runs == []


async def test_malformed_json_rejected(api_stack):
    adapter, _controller = await api_stack()
    async with aiohttp.ClientSession() as client:
        async with client.post(
            _url(adapter),
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        ) as resp:
            assert resp.status == 400


async def test_healthz_needs_no_auth(api_stack):
    adapter, _controller = await api_stack(auth_token="s3cret")
    async with aiohttp.ClientSession() as client:
        async with client.get(_url(adapter, "/healthz")) as resp:
            assert resp.status == 200
            assert await resp.json() == {"status": "ok"}


# --- Session continuation ---


async def test_same_session_resumes(api_stack, session_manager):
    adapter, controller = await api_stack()

    await _post(adapter, {"text": "first", "session": "chat-1"})
    await _post(adapter, {"text": "second", "session": "chat-1"})

    assert controller.calls == ["first", "second"]
    first, second = controller.runs
    assert first.is_new is True
    assert second.is_new is False  # real SessionManager resumed the mapping
    assert first.session_id == second.session_id
    assert session_manager.get("api:client:chat-1") == first.session_id


# --- Capacity ---


async def test_capacity_full_returns_503(api_stack):
    adapter, controller = await api_stack(max_concurrent=1, delay=0.5)

    first = asyncio.create_task(_post(adapter, {"text": "slow one"}))
    while not controller.runs:  # wait until the slot is actually held
        await asyncio.sleep(0.01)

    status, body = await _post(adapter, {"text": "rejected"})
    assert status == 503
    assert body["is_error"] is True
    assert "Too many requests" in body["text"]
    assert body["usage"] is None

    # SSE requests are rejected the same way, before any stream starts.
    stream_status, stream_body = await _post(
        adapter, {"text": "rejected too", "stream": True}
    )
    assert stream_status == 503
    assert stream_body["is_error"] is True

    first_status, first_body = await first
    assert first_status == 200
    assert first_body["is_error"] is False
    assert controller.calls == ["slow one"]

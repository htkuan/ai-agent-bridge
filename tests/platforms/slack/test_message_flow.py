"""_process_message gates: serialisation, parking, draining, and cleanup."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_bridge.bridge.events import BridgeEvent, Completion, Processing, UserQuestion
from agent_bridge.platforms.slack.adapter import _PendingMessage
from tests.fakes import mention_event
from tests.platforms.slack.harness import build_harness

WAITING_PLACEHOLDER = ":hourglass: Waiting for previous task to finish..."


async def test_idle_session_streams_reply():
    harness = build_harness()
    adapter = harness.adapter

    await adapter._process_message(
        mention_event(text="<@UBOT> hello", ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    call = harness.bridge.calls[0]
    assert call.session_key == "slack:C123:1.0"
    assert call.text == "[alice (U123)]: hello"
    assert call.system_prompt is not None
    assert "Slack" in call.system_prompt
    # The placeholder message ends up rewritten with the final reply.
    assert list(harness.client.messages.values()) == ["ok"]
    state = adapter._get_state("slack:C123:1.0")
    assert state.processing is False


async def test_mention_only_message_is_dropped():
    harness = build_harness()

    await harness.adapter._process_message(
        mention_event(text="<@UBOT>", ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert harness.bridge.calls == []
    assert harness.adapter._sessions == {}


async def test_busy_session_parks_message():
    harness = build_harness()
    adapter = harness.adapter
    state = adapter._get_state("slack:C123:1.0")
    state.processing = True

    await adapter._process_message(
        mention_event(text="<@UBOT> queued one", ts="2.0", thread_ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert harness.bridge.calls == []
    assert state.pending is not None
    assert state.pending.text == "queued one"
    placeholder_key = ("C123", state.pending.message_ts)
    assert harness.client.messages[placeholder_key] == WAITING_PLACEHOLDER


async def test_park_keeps_only_latest_and_deletes_replaced_placeholder():
    harness = build_harness()
    adapter = harness.adapter
    state = adapter._get_state("slack:C123:1.0")
    state.processing = True

    await adapter._process_message(
        mention_event(text="<@UBOT> queued one", ts="2.0", thread_ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )
    assert state.pending is not None
    first_ts = state.pending.message_ts

    await adapter._process_message(
        mention_event(text="<@UBOT> queued two", ts="3.0", thread_ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert state.pending is not None
    assert state.pending.text == "queued two"
    assert ("C123", first_ts) not in harness.client.messages
    assert len(harness.client.calls_to("chat_delete")) == 1


async def test_drains_parked_message_after_run():
    harness = build_harness()
    adapter = harness.adapter
    state = adapter._get_state("slack:C123:1.0")
    placeholder = await harness.client.chat_postMessage(
        channel="C123", text=WAITING_PLACEHOLDER, thread_ts="1.0"
    )
    state.pending = _PendingMessage(
        text="queued",
        context={"user_name": "bob", "user_id": "U9"},
        message_ts=placeholder["ts"],
        channel="C123",
        thread_ts="1.0",
    )

    await adapter._process_message(
        mention_event(text="<@UBOT> hello", ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert [c.text for c in harness.bridge.calls] == [
        "[alice (U123)]: hello",
        "[bob (U9)]: queued",
    ]
    assert state.pending is None
    assert state.processing is False
    # The drained run rendered into the parked placeholder message.
    assert harness.client.messages[("C123", placeholder["ts"])] == "ok"


async def test_answer_resumes_waiting_session():
    harness = build_harness()
    adapter = harness.adapter
    state = adapter._get_state("slack:C123:1.0")
    state.waiting_for_answer = True

    await adapter._process_message(
        mention_event(text="<@UBOT> my answer", ts="2.0", thread_ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert state.waiting_for_answer is False
    assert state.processing is False
    assert harness.bridge.calls[0].text == "[alice (U123)]: my answer"


async def test_user_question_puts_session_in_waiting_state():
    questions = [{"question": "Which env?", "options": ["dev", "prod"]}]
    harness = build_harness(
        events=[Processing(), UserQuestion(questions=questions), Completion(text="")]
    )
    adapter = harness.adapter

    await adapter._process_message(
        mention_event(text="<@UBOT> deploy", ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    state = adapter._get_state("slack:C123:1.0")
    assert state.waiting_for_answer is True
    assert state.processing is False
    posted = "\n".join(harness.client.messages.values())
    assert "Claude needs your input" in posted
    assert "Which env?" in posted


class _ExplodingBridge:
    def handle_message(self, **_kwargs: object) -> AsyncIterator[BridgeEvent]:
        async def boom() -> AsyncIterator[BridgeEvent]:
            raise RuntimeError("boom")
            yield Processing()  # makes this an async generator

        return boom()


async def test_error_resets_state_and_deletes_parked_placeholder():
    harness = build_harness()
    adapter = harness.adapter
    adapter._bridge = _ExplodingBridge()
    state = adapter._get_state("slack:C123:1.0")
    placeholder = await harness.client.chat_postMessage(
        channel="C123", text=WAITING_PLACEHOLDER, thread_ts="1.0"
    )
    state.pending = _PendingMessage(
        text="queued",
        context={},
        message_ts=placeholder["ts"],
        channel="C123",
        thread_ts="1.0",
    )

    await adapter._process_message(
        mention_event(text="<@UBOT> hello", ts="1.0"),
        harness.client.say_for("C123"),
        harness.client,
    )

    assert state.processing is False
    assert state.pending is None
    assert ("C123", placeholder["ts"]) not in harness.client.messages

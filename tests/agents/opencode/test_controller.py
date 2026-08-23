"""OpencodeController: command construction, the session handle store, and
the streamed run driven by the scripted fake opencode CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.agents.opencode.config import OpencodeConfig
from agent_bridge.agents.opencode.controller import OpencodeController
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta
from tests.conftest import FakeOpencodeFactory
from tests.fakes import opencode_cli

# run() drives a real subprocess (the scripted fake CLI).
pytestmark = pytest.mark.integration


def _seeded_config(tmp_path: Path, mapping: dict[str, str]) -> OpencodeConfig:
    map_path = tmp_path / "opencode-sessions.json"
    map_path.write_text(json.dumps(mapping))
    return OpencodeConfig(work_dir=tmp_path, session_map_path=map_path)


# --- build_command ---


def test_build_command_new_session_titles_the_session(tmp_path: Path):
    controller = OpencodeController(OpencodeConfig(work_dir=tmp_path))
    cmd = controller.build_command("abc-123", "hello", is_new=True)
    assert cmd == [
        "opencode",
        "run",
        "--format",
        "json",
        "--title",
        "bridge-abc-123",
    ]


def test_build_command_resume_uses_stored_session_id(tmp_path: Path):
    controller = OpencodeController(_seeded_config(tmp_path, {"s1": "ses_abc"}))
    cmd = controller.build_command("s1", "hi", is_new=False)
    assert cmd == ["opencode", "run", "--format", "json", "-s", "ses_abc"]
    assert "--title" not in cmd


def test_build_command_resume_map_miss_falls_back_to_new_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    controller = OpencodeController(OpencodeConfig(work_dir=tmp_path))
    with caplog.at_level("WARNING"):
        cmd = controller.build_command("s1", "hi", is_new=False)
    assert "-s" not in cmd
    assert cmd[cmd.index("--title") + 1] == "bridge-s1"
    assert "No opencode session recorded" in caplog.text


def test_build_command_includes_optional_flags(tmp_path: Path):
    controller = OpencodeController(
        OpencodeConfig(
            work_dir=tmp_path, model="anthropic/claude-opus-5", variant="max"
        )
    )
    cmd = controller.build_command("s1", "hi", is_new=True)
    assert cmd[cmd.index("-m") + 1] == "anthropic/claude-opus-5"
    assert cmd[cmd.index("--variant") + 1] == "max"


def test_build_command_omits_unset_flags(tmp_path: Path):
    cmd = OpencodeController(OpencodeConfig(work_dir=tmp_path)).build_command(
        "s1", "hi", is_new=True
    )
    for flag in ("-m", "--variant", "-s"):
        assert flag not in cmd


def test_build_command_prompt_never_in_argv(tmp_path: Path):
    controller = OpencodeController(OpencodeConfig(work_dir=tmp_path))
    cmd = controller.build_command("s1", "--dangerous-looking prompt", is_new=True)
    assert "--dangerous-looking prompt" not in cmd


# --- stdin_payload ---


def test_stdin_payload_carries_the_prompt(tmp_path: Path):
    controller = OpencodeController(OpencodeConfig(work_dir=tmp_path))
    assert controller.stdin_payload("[alice]: hi") == b"[alice]: hi"


def test_stdin_payload_prefixes_system_prompt_as_tagged_block(tmp_path: Path):
    controller = OpencodeController(OpencodeConfig(work_dir=tmp_path))
    payload = controller.stdin_payload("hi", "You are in a Slack thread.")
    assert payload == (
        b"<system_directives>\nYou are in a Slack thread.\n</system_directives>\n\nhi"
    )


# --- run() with the scripted CLI ---


async def test_run_happy_path_streams_and_completes(fake_opencode: FakeOpencodeFactory):
    cli = fake_opencode(
        [
            opencode_cli.step_start(session_id="ses_1"),
            opencode_cli.tool_use("read", session_id="ses_1"),
            opencode_cli.step_finish(
                session_id="ses_1",
                reason="tool-calls",
                input_tokens=3,
                output_tokens=30,
                cache_write=5944,
                cost=0.0015334,
            ),
            opencode_cli.text_event("working on it", session_id="ses_1"),
            opencode_cli.text_event("the answer", session_id="ses_1"),
            opencode_cli.step_finish(
                session_id="ses_1",
                input_tokens=2,
                output_tokens=40,
                cache_read=5944,
                cost=0.0001736,
            ),
        ]
    )
    controller = OpencodeController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["working on it", "the answer"]
    assert StatusUpdate(status="Using read...") in events
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert events[-1] == completions[0]
    completion = completions[0]
    assert completion.text == "the answer"
    assert completion.is_error is False
    assert completion.cost_usd == pytest.approx(0.0015334 + 0.0001736)
    assert completion.metadata["usage"]["input_tokens"] == 5
    assert completion.metadata["usage"]["output_tokens"] == 70
    assert completion.metadata["usage"]["cache_read_tokens"] == 5944
    assert completion.metadata["usage"]["cache_creation_tokens"] == 5944
    assert completion.metadata["usage"]["num_turns"] == 2


async def test_run_passes_prompt_via_stdin_not_argv(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(opencode_cli.reply_steps("ok"))
    controller = OpencodeController(cli.config)

    [e async for e in controller.run("s1", "--flag-looking prompt", is_new=True)]

    (argv,) = cli.invocations()
    assert "--flag-looking prompt" not in argv
    assert cli.stdin_payloads() == ["--flag-looking prompt"]


async def test_run_folds_system_prompt_into_stdin(fake_opencode: FakeOpencodeFactory):
    cli = fake_opencode(opencode_cli.reply_steps("ok"))
    controller = OpencodeController(cli.config)

    [
        e
        async for e in controller.run(
            "s1", "hi", is_new=True, system_prompt="Be terse."
        )
    ]

    (argv,) = cli.invocations()
    assert "Be terse." not in argv
    assert cli.stdin_payloads() == [
        "<system_directives>\nBe terse.\n</system_directives>\n\nhi"
    ]


async def test_run_persists_session_mapping_from_first_event(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(opencode_cli.reply_steps("ok", session_id="ses_42"))
    controller = OpencodeController(cli.config)

    [e async for e in controller.run("s1", "hi", is_new=True)]

    store_file = cli.config.resolved_session_map_path
    assert json.loads(store_file.read_text()) == {"s1": "ses_42"}


async def test_run_resume_puts_stored_session_id_in_argv(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(opencode_cli.reply_steps("ok", session_id="ses_42"))
    controller = OpencodeController(cli.config)

    [e async for e in controller.run("s1", "first", is_new=True)]
    [e async for e in controller.run("s1", "again", is_new=False)]

    first, second = cli.invocations()
    assert "-s" not in first
    assert second[second.index("-s") + 1] == "ses_42"
    assert "--title" not in second


async def test_run_resume_map_miss_degrades_to_new_session(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(opencode_cli.reply_steps("ok"))
    controller = OpencodeController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=False)]

    (argv,) = cli.invocations()
    assert "-s" not in argv
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False


async def test_cleanup_session_removes_the_mapping(fake_opencode: FakeOpencodeFactory):
    cli = fake_opencode(opencode_cli.reply_steps("ok", session_id="ses_42"))
    controller = OpencodeController(cli.config)
    [e async for e in controller.run("s1", "hi", is_new=True)]

    await controller.cleanup_session("s1")

    store_file = cli.config.resolved_session_map_path
    assert json.loads(store_file.read_text()) == {}


async def test_run_error_event_with_nonzero_exit_is_an_error_completion(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(
        [
            opencode_cli.step_start(),
            opencode_cli.stream_error("Unexpected server error."),
            opencode_cli.exit_code(1),
        ]
    )
    controller = OpencodeController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "Unexpected server error."


async def test_run_nonzero_exit_without_json_is_the_engine_generic_error(
    fake_opencode: FakeOpencodeFactory,
):
    cli = fake_opencode(
        [
            opencode_cli.stderr_line("Error: Session not found"),
            opencode_cli.exit_code(1),
        ]
    )
    controller = OpencodeController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Opencode process exited with code 1" in completion.text


async def test_run_skips_malformed_json_lines(fake_opencode: FakeOpencodeFactory):
    cli = fake_opencode(
        [opencode_cli.raw_line("not json"), *opencode_cli.reply_steps("fine")]
    )
    controller = OpencodeController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert deltas == "fine"
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False

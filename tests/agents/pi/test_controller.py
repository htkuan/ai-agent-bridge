"""PiController: command construction and the streamed run driven by the
scripted fake pi CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.agents.pi.config import PiConfig
from agent_bridge.agents.pi.controller import PiController
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta
from tests.conftest import FakePiFactory
from tests.fakes import pi_cli

# run() drives a real subprocess (the scripted fake CLI).
pytestmark = pytest.mark.integration


# --- build_command ---


def test_build_command_minimal(tmp_path: Path):
    controller = PiController(PiConfig(work_dir=tmp_path))
    cmd = controller.build_command("abc-123", "hello", is_new=True)
    assert cmd == ["pi", "-p", "--mode", "json", "--session-id", "abc-123"]


def test_build_command_same_for_new_and_resume(tmp_path: Path):
    # --session-id creates-if-missing, so both branches share one shape.
    controller = PiController(PiConfig(work_dir=tmp_path))
    new = controller.build_command("s1", "hi", is_new=True)
    resumed = controller.build_command("s1", "hi", is_new=False)
    assert new == resumed


def test_build_command_prompt_never_in_argv(tmp_path: Path):
    controller = PiController(PiConfig(work_dir=tmp_path))
    cmd = controller.build_command("s1", "--dangerous-looking prompt", is_new=True)
    assert "--dangerous-looking prompt" not in cmd


def test_stdin_payload_carries_the_prompt(tmp_path: Path):
    controller = PiController(PiConfig(work_dir=tmp_path))
    assert controller.stdin_payload("[alice]: hi", None) == b"[alice]: hi"


def test_build_command_includes_optional_flags(tmp_path: Path):
    controller = PiController(
        PiConfig(
            work_dir=tmp_path,
            provider="openai-codex",
            model="gpt-5.6-luna",
            thinking="high",
            tools=("read", "grep"),
            exclude_tools=("bash",),
        )
    )
    cmd = controller.build_command("s1", "hi", is_new=True)
    assert cmd[cmd.index("--provider") + 1] == "openai-codex"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
    assert cmd[cmd.index("--thinking") + 1] == "high"
    assert cmd[cmd.index("--tools") + 1] == "read,grep"
    assert cmd[cmd.index("--exclude-tools") + 1] == "bash"


def test_build_command_omits_unset_flags(tmp_path: Path):
    cmd = PiController(PiConfig(work_dir=tmp_path)).build_command(
        "s1", "hi", is_new=True
    )
    for flag in ("--provider", "--model", "--thinking", "--tools", "--exclude-tools"):
        assert flag not in cmd


def test_build_command_appends_system_prompt_verbatim(tmp_path: Path):
    sp = "You are replying inside a Slack thread."
    cmd = PiController(PiConfig(work_dir=tmp_path)).build_command(
        "s1", "hi", is_new=True, system_prompt=sp
    )
    assert cmd[cmd.index("--append-system-prompt") + 1] == sp


def test_build_command_omits_system_prompt_when_empty(tmp_path: Path):
    cmd = PiController(PiConfig(work_dir=tmp_path)).build_command(
        "s1", "hi", is_new=True, system_prompt=""
    )
    assert "--append-system-prompt" not in cmd


# --- run() with the scripted CLI ---


async def test_run_happy_path_streams_and_completes(fake_pi: FakePiFactory):
    cli = fake_pi(
        [
            pi_cli.session_header(),
            pi_cli.assistant_message(
                "reading first", stop_reason="toolUse", tool_call="read"
            ),
            pi_cli.tool_start("read", {"path": "x.py"}),
            pi_cli.tool_result_message(),
            pi_cli.turn_end(),
            pi_cli.assistant_message(
                "the answer", input_tokens=10, output_tokens=5, cost=0.02
            ),
            pi_cli.turn_end(),
            pi_cli.agent_end(),
        ]
    )
    controller = PiController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["reading first", "the answer"]
    assert StatusUpdate(status="Using read...") in events
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert events[-1] == completions[0]
    completion = completions[0]
    assert completion.text == "the answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.02
    assert completion.metadata["usage"]["input_tokens"] == 10
    assert completion.metadata["usage"]["num_turns"] == 2


async def test_run_passes_prompt_via_stdin_not_argv(fake_pi: FakePiFactory):
    cli = fake_pi(pi_cli.reply_steps("ok"))
    controller = PiController(cli.config)

    [e async for e in controller.run("s1", "--flag-looking prompt", is_new=True)]

    (argv,) = cli.invocations()
    assert "--flag-looking prompt" not in argv
    assert argv[argv.index("--session-id") + 1] == "s1"
    assert cli.stdin_payloads() == ["--flag-looking prompt"]


async def test_run_exit_without_agent_end_is_an_error(fake_pi: FakePiFactory):
    cli = fake_pi(
        [
            pi_cli.session_header(),
            pi_cli.stderr_line("provider exploded"),
            pi_cli.exit_code(1),
        ]
    )
    controller = PiController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Pi process exited with code 1" in completion.text


async def test_run_continues_past_agent_end_with_retry(fake_pi: FakePiFactory):
    cli = fake_pi(
        [
            pi_cli.session_header(),
            pi_cli.assistant_message("first try"),
            pi_cli.turn_end(),
            pi_cli.agent_end(will_retry=True),
            pi_cli.assistant_message("second try"),
            pi_cli.turn_end(),
            pi_cli.agent_end(),
        ]
    )
    controller = PiController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert completions[0].text == "second try"


async def test_run_skips_malformed_json_lines(fake_pi: FakePiFactory):
    cli = fake_pi([pi_cli.raw_line("not json"), *pi_cli.reply_steps("fine")])
    controller = PiController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert deltas == "fine"
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False

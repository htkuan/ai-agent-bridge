"""CodexController: command construction, the session handle store, and the
streamed run driven by the scripted fake codex CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta
from tests.conftest import FakeCodexFactory
from tests.fakes import codex_cli

# run() drives a real subprocess (the scripted fake CLI).
pytestmark = pytest.mark.integration


def _seeded_config(tmp_path: Path, mapping: dict[str, str]) -> CodexConfig:
    map_path = tmp_path / "codex-sessions.json"
    map_path.write_text(json.dumps(mapping))
    return CodexConfig(work_dir=tmp_path, session_map_path=map_path)


# --- build_command ---


def test_build_command_new_session(tmp_path: Path):
    controller = CodexController(CodexConfig(work_dir=tmp_path))
    cmd = controller.build_command("abc-123", "hello", is_new=True)
    assert cmd == ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"]


def test_build_command_resume_uses_config_override_not_sandbox_flag(tmp_path: Path):
    controller = CodexController(_seeded_config(tmp_path, {"s1": "thread-abc"}))
    cmd = controller.build_command("s1", "hi", is_new=False)
    assert cmd == [
        "codex",
        "exec",
        "resume",
        "thread-abc",
        "--json",
        "-c",
        'sandbox_mode="workspace-write"',
        "-",
    ]
    assert "--sandbox" not in cmd


def test_build_command_resume_map_miss_falls_back_to_new_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    controller = CodexController(CodexConfig(work_dir=tmp_path))
    with caplog.at_level("WARNING"):
        cmd = controller.build_command("s1", "hi", is_new=False)
    assert "resume" not in cmd
    assert cmd == ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"]
    assert "No codex thread recorded" in caplog.text


def test_build_command_includes_optional_flags(tmp_path: Path):
    controller = CodexController(
        CodexConfig(
            work_dir=tmp_path,
            sandbox_mode="read-only",
            model="gpt-5.3-codex",
            effort="high",
            skip_git_repo_check=True,
        )
    )
    cmd = controller.build_command("s1", "hi", is_new=True)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.3-codex"
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert "--skip-git-repo-check" in cmd
    assert cmd[-1] == "-"


def test_build_command_omits_unset_flags(tmp_path: Path):
    cmd = CodexController(CodexConfig(work_dir=tmp_path)).build_command(
        "s1", "hi", is_new=True
    )
    for flag in ("-m", "-c", "--skip-git-repo-check"):
        assert flag not in cmd


def test_build_command_prompt_never_in_argv(tmp_path: Path):
    controller = CodexController(CodexConfig(work_dir=tmp_path))
    cmd = controller.build_command("s1", "--dangerous-looking prompt", is_new=True)
    assert "--dangerous-looking prompt" not in cmd


# --- stdin_payload ---


def test_stdin_payload_carries_the_prompt(tmp_path: Path):
    controller = CodexController(CodexConfig(work_dir=tmp_path))
    assert controller.stdin_payload("[alice]: hi") == b"[alice]: hi"


def test_stdin_payload_prefixes_system_prompt_as_tagged_block(tmp_path: Path):
    controller = CodexController(CodexConfig(work_dir=tmp_path))
    payload = controller.stdin_payload("hi", "You are in a Slack thread.")
    assert payload == (
        b"<system_directives>\nYou are in a Slack thread.\n</system_directives>\n\nhi"
    )


# --- run() with the scripted CLI ---


async def test_run_happy_path_streams_and_completes(fake_codex: FakeCodexFactory):
    cli = fake_codex(
        [
            codex_cli.thread_started("t-1"),
            codex_cli.turn_started(),
            codex_cli.command_execution_started("git status"),
            codex_cli.command_execution_completed("git status"),
            codex_cli.file_change(["calc.py"]),
            codex_cli.agent_message("working on it"),
            codex_cli.agent_message("the answer"),
            codex_cli.turn_completed(
                input_tokens=100, cached_input_tokens=60, output_tokens=5
            ),
        ]
    )
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["working on it", "the answer"]
    assert StatusUpdate(status="Running a command...", detail="git status") in events
    assert StatusUpdate(status="Editing files...", detail="calc.py") in events
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert events[-1] == completions[0]
    completion = completions[0]
    assert completion.text == "the answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.0
    assert completion.metadata["usage"]["input_tokens"] == 40
    assert completion.metadata["usage"]["cache_read_tokens"] == 60
    assert completion.metadata["usage"]["output_tokens"] == 5
    assert completion.metadata["usage"]["num_turns"] == 1


async def test_run_passes_prompt_via_stdin_not_argv(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("s1", "--flag-looking prompt", is_new=True)]

    (argv,) = cli.invocations()
    assert "--flag-looking prompt" not in argv
    assert argv[-1] == "-"
    assert cli.stdin_payloads() == ["--flag-looking prompt"]


async def test_run_folds_system_prompt_into_stdin(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok"))
    controller = CodexController(cli.config)

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


async def test_run_persists_thread_mapping_from_thread_started(
    fake_codex: FakeCodexFactory,
):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="t-42"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("s1", "hi", is_new=True)]

    store_file = cli.config.resolved_session_map_path
    assert json.loads(store_file.read_text()) == {"s1": "t-42"}


async def test_run_resume_puts_stored_thread_id_in_argv(
    fake_codex: FakeCodexFactory,
):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="t-42"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("s1", "first", is_new=True)]
    [e async for e in controller.run("s1", "again", is_new=False)]

    first, second = cli.invocations()
    assert "resume" not in first
    assert second[second.index("resume") + 1] == "t-42"
    assert "--sandbox" not in second
    assert 'sandbox_mode="workspace-write"' in second


async def test_run_resume_map_miss_degrades_to_new_session(
    fake_codex: FakeCodexFactory,
):
    cli = fake_codex(codex_cli.reply_steps("ok"))
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=False)]

    (argv,) = cli.invocations()
    assert "resume" not in argv
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False


async def test_cleanup_session_removes_the_mapping(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="t-42"))
    controller = CodexController(cli.config)
    [e async for e in controller.run("s1", "hi", is_new=True)]

    await controller.cleanup_session("s1")

    store_file = cli.config.resolved_session_map_path
    assert json.loads(store_file.read_text()) == {}


async def test_run_turn_failed_is_an_error_completion(fake_codex: FakeCodexFactory):
    cli = fake_codex(
        [
            codex_cli.thread_started(),
            codex_cli.error_item("Model metadata for `bogus` not found."),
            codex_cli.stream_error("status 400"),
            codex_cli.turn_failed("status 400"),
        ]
    )
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert completion.text == "status 400"


async def test_run_exit_without_terminal_is_an_error(fake_codex: FakeCodexFactory):
    cli = fake_codex(
        [
            codex_cli.thread_started(),
            codex_cli.stderr_line("no rollout found for thread id t-x"),
            codex_cli.exit_code(1),
        ]
    )
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Codex process exited with code 1" in completion.text


async def test_run_skips_malformed_json_lines(fake_codex: FakeCodexFactory):
    cli = fake_codex([codex_cli.raw_line("not json"), *codex_cli.reply_steps("fine")])
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert deltas == "fine"
    last = events[-1]
    assert isinstance(last, Completion)
    assert last.is_error is False

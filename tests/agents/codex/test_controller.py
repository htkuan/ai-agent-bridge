from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.agents.codex.config import CodexConfig
from agent_bridge.agents.codex.controller import CodexController
from agent_bridge.agents.handles import SessionHandleStore
from agent_bridge.bridge.events import Completion, StatusUpdate, TextDelta
from tests.conftest import FakeCodexFactory
from tests.fakes import codex_cli

pytestmark = pytest.mark.integration


def _config(tmp_path: Path, **kwargs: object) -> CodexConfig:
    return CodexConfig(
        work_dir=tmp_path,
        session_map_path=tmp_path / "codex-sessions.json",
        **kwargs,
    )


def test_build_command_minimal_new_session(tmp_path: Path):
    controller = CodexController(_config(tmp_path))
    cmd = controller.build_command("s1", "hello", is_new=True)
    assert cmd == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "-",
    ]


def test_build_command_includes_optional_new_session_flags(tmp_path: Path):
    controller = CodexController(
        _config(
            tmp_path,
            sandbox_mode="read-only",
            model="gpt-5.1-codex",
            effort="high",
            skip_git_repo_check=True,
        )
    )

    cmd = controller.build_command("s1", "hi", is_new=True)

    assert cmd == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.1-codex",
        "-c",
        'model_reasoning_effort="high"',
        "--skip-git-repo-check",
        "-",
    ]


def test_build_command_resume_uses_stored_thread_id_and_config_sandbox(
    tmp_path: Path,
):
    config = _config(tmp_path, sandbox_mode="danger-full-access")
    SessionHandleStore(config.resolved_session_map_path).put("s1", "thread-1")
    controller = CodexController(config)

    cmd = controller.build_command("s1", "hi", is_new=False)

    assert cmd == [
        "codex",
        "exec",
        "resume",
        "thread-1",
        "--json",
        "-c",
        'sandbox_mode="danger-full-access"',
        "-",
    ]
    assert "--sandbox" not in cmd


def test_build_command_map_miss_on_resume_falls_back_to_new_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    controller = CodexController(_config(tmp_path))

    cmd = controller.build_command("missing", "hi", is_new=False)

    assert cmd[:5] == ["codex", "exec", "--json", "--sandbox", "workspace-write"]
    assert "resume" not in cmd
    assert "No Codex thread id" in caplog.text


def test_build_command_prompt_never_in_argv(tmp_path: Path):
    controller = CodexController(_config(tmp_path))
    cmd = controller.build_command("s1", "--dangerous-looking prompt", is_new=True)
    assert "--dangerous-looking prompt" not in cmd


def test_stdin_payload_carries_prompt(tmp_path: Path):
    controller = CodexController(_config(tmp_path))
    assert controller.stdin_payload("[alice]: hi", None) == b"[alice]: hi"


def test_stdin_payload_prefixes_system_prompt(tmp_path: Path):
    controller = CodexController(_config(tmp_path))
    assert controller.stdin_payload("hi", "Stay concise.") == (
        b"<system_directives>\nStay concise.\n</system_directives>\n\nhi"
    )


async def test_run_happy_path_streams_and_completes(fake_codex: FakeCodexFactory):
    cli = fake_codex(
        [
            codex_cli.thread_started("thread-1"),
            codex_cli.agent_message("working"),
            codex_cli.command_execution_started("git status"),
            codex_cli.command_execution_completed(),
            codex_cli.file_change(["/repo/calc.py"]),
            codex_cli.agent_message("the answer"),
            codex_cli.turn_completed(
                input_tokens=100,
                cached_input_tokens=40,
                cache_write_input_tokens=5,
                output_tokens=12,
            ),
        ]
    )
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=True)]

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["working", "the answer"]
    assert StatusUpdate(status="Running a command...", detail="git status") in events
    assert StatusUpdate(status="Editing files...", detail="/repo/calc.py") in events
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert events[-1] == completions[0]
    completion = completions[0]
    assert completion.text == "the answer"
    assert completion.is_error is False
    assert completion.cost_usd == 0.0
    assert completion.metadata["usage"]["input_tokens"] == 60
    assert completion.metadata["usage"]["cache_read_tokens"] == 40
    assert completion.metadata["usage"]["cache_creation_tokens"] == 5
    assert completion.metadata["usage"]["output_tokens"] == 12
    assert completion.metadata["usage"]["num_turns"] == 1


async def test_run_passes_prompt_via_stdin_not_argv(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("s1", "--flag-looking prompt", is_new=True)]

    (argv,) = cli.invocations()
    assert "--flag-looking prompt" not in argv
    assert argv[-1] == "-"
    assert cli.stdin_payloads() == ["--flag-looking prompt"]


async def test_run_prefixes_system_prompt_in_stdin(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok"))
    controller = CodexController(cli.config)

    [
        e
        async for e in controller.run(
            "s1", "body", is_new=True, system_prompt="Slack thread context"
        )
    ]

    assert cli.stdin_payloads() == [
        "<system_directives>\nSlack thread context\n</system_directives>\n\nbody"
    ]


async def test_thread_started_persists_mapping(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="thread-1"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("bridge-1", "hi", is_new=True)]

    assert json.loads(cli.config.resolved_session_map_path.read_text()) == {
        "bridge-1": "thread-1"
    }


async def test_resume_run_uses_stored_thread_id(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="thread-1"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("bridge-1", "first", is_new=True)]
    [e async for e in controller.run("bridge-1", "second", is_new=False)]

    _, resume_argv = cli.invocations()
    assert resume_argv[:4] == ["exec", "resume", "thread-1", "--json"]
    assert resume_argv[resume_argv.index("-c") + 1] == (
        'sandbox_mode="workspace-write"'
    )
    assert "--sandbox" not in resume_argv


async def test_resume_map_miss_falls_back_to_new_session(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="thread-new"))
    controller = CodexController(cli.config)

    [e async for e in controller.run("bridge-1", "hi", is_new=False)]

    (argv,) = cli.invocations()
    assert argv[:4] == ["exec", "--json", "--sandbox", "workspace-write"]
    assert "resume" not in argv
    assert json.loads(cli.config.resolved_session_map_path.read_text()) == {
        "bridge-1": "thread-new"
    }


async def test_cleanup_session_removes_mapping(fake_codex: FakeCodexFactory):
    cli = fake_codex(codex_cli.reply_steps("ok", thread_id="thread-1"))
    controller = CodexController(cli.config)
    SessionHandleStore(cli.config.resolved_session_map_path).put("bridge-1", "thread-1")

    await controller.cleanup_session("bridge-1")

    assert json.loads(cli.config.resolved_session_map_path.read_text()) == {}


async def test_run_exit_without_terminal_is_an_error(fake_codex: FakeCodexFactory):
    cli = fake_codex(
        [codex_cli.stderr_line("no rollout found"), codex_cli.exit_code(1)]
    )
    controller = CodexController(cli.config)

    events = [e async for e in controller.run("s1", "hi", is_new=False)]

    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Codex process exited with code 1" in completion.text

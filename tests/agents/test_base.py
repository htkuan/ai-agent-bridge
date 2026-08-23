"""The ``CliAgentController`` engine's base-only behaviour.

The engine's happy, timeout, and kill-tree paths are exercised end-to-end
through ``ClaudeController`` (tests/agents/claude/test_controller.py). Here a
toy subclass pins what only the base defines: required-hook errors, the
default ``cleanup_session`` no-op, and ``on_stream_end`` synthesis for CLIs
whose stream has no terminal event (the opencode shape).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_bridge.agents.base import CliAgentController, RunState
from agent_bridge.bridge.events import BridgeEvent, Completion, TextDelta

# The toy controller drives a real python subprocess.
pytestmark = pytest.mark.integration


@dataclass
class _EchoState(RunState):
    texts: list[str] = field(default_factory=list[str])


class _EchoController(CliAgentController[_EchoState]):
    """Toy agent: prints plain-text lines and exits — no terminal event."""

    agent_name = "Echo"

    def __init__(self, work_dir: Path, lines: list[str], *, exit_code: int = 0) -> None:
        super().__init__(work_dir=work_dir, timeout_seconds=5.0)
        self._lines = lines
        self._exit_code = exit_code

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        body = "".join(f"print({line!r});" for line in self._lines)
        return [sys.executable, "-c", f"{body}raise SystemExit({self._exit_code})"]

    def new_run_state(self) -> _EchoState:
        return _EchoState()

    def parse_line(self, line: str, state: _EchoState) -> list[BridgeEvent]:
        text = line.strip()
        if not text:
            return []
        state.texts.append(text)
        return [TextDelta(text=text)]

    def on_stream_end(
        self, state: _EchoState, return_code: int | None, stderr: str
    ) -> Completion | None:
        if return_code != 0:
            return None  # fall through to the engine's generic error
        return Completion(text=" ".join(state.texts), is_error=False)


class _StdinFeedingController(CliAgentController[RunState]):
    """Toy agent whose CLI never reads the (large) stdin payload it is fed —
    pins that a dead pipe is tolerated, not raised."""

    agent_name = "Mute"

    def __init__(self, work_dir: Path) -> None:
        super().__init__(work_dir=work_dir, timeout_seconds=5.0)

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(0.3)"]

    def new_run_state(self) -> RunState:
        return RunState()

    def parse_line(self, line: str, state: RunState) -> list[BridgeEvent]:
        return []

    def stdin_payload(
        self, prompt: str, system_prompt: str | None = None
    ) -> bytes | None:
        # Far beyond the pipe buffer, so the write can't finish before the
        # process exits without reading.
        return b"x" * (2 * 1024 * 1024)


class _SessionIdEchoController(CliAgentController[RunState]):
    """Echoes ``state.session_id`` — pins that the engine stamps the bridge
    session id onto the run state before any line is parsed."""

    agent_name = "SessionEcho"

    def __init__(self, work_dir: Path) -> None:
        super().__init__(work_dir=work_dir, timeout_seconds=5.0)

    def build_command(
        self,
        session_id: str,
        prompt: str,
        is_new: bool,
        system_prompt: str | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "print('line')"]

    def new_run_state(self) -> RunState:
        return RunState()

    def parse_line(self, line: str, state: RunState) -> list[BridgeEvent]:
        if not line.strip():
            return []
        return [TextDelta(text=state.session_id)]

    def on_stream_end(
        self, state: RunState, return_code: int | None, stderr: str
    ) -> Completion | None:
        return Completion(text="done", is_error=False)


class _BareController(CliAgentController[RunState]):
    """No hooks overridden — pins the required-hook contract."""


async def test_on_stream_end_synthesizes_completion_at_eof(tmp_path: Path):
    controller = _EchoController(tmp_path, ["hello", "world"])
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["hello", "world"]
    completions = [e for e in events if isinstance(e, Completion)]
    assert len(completions) == 1
    assert isinstance(events[-1], Completion)
    assert completions[0].is_error is False
    assert completions[0].text == "hello world"


async def test_on_stream_end_none_falls_back_to_generic_error(tmp_path: Path):
    controller = _EchoController(tmp_path, ["partial"], exit_code=2)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Echo process exited with code 2" in completion.text


async def test_unread_stdin_payload_does_not_break_the_run(tmp_path: Path):
    controller = _StdinFeedingController(tmp_path)
    events = [e async for e in controller.run("s1", "hi", is_new=True)]
    # The run still terminates with the engine's generic completion (no
    # output was ever parsed) instead of surfacing the broken pipe.
    completion = events[-1]
    assert isinstance(completion, Completion)
    assert completion.is_error is True
    assert "Mute process exited" in completion.text


async def test_engine_stamps_session_id_on_run_state(tmp_path: Path):
    controller = _SessionIdEchoController(tmp_path)
    events = [e async for e in controller.run("s-42", "hi", is_new=True)]
    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["s-42"]


async def test_default_cleanup_session_is_a_noop(tmp_path: Path):
    controller = _EchoController(tmp_path, [])
    await controller.cleanup_session("never-seen")


def test_required_hooks_raise_not_implemented(tmp_path: Path):
    bare = _BareController(work_dir=tmp_path, timeout_seconds=1.0)
    with pytest.raises(NotImplementedError):
        bare.build_command("s1", "hi", is_new=True)
    with pytest.raises(NotImplementedError):
        bare.new_run_state()
    with pytest.raises(NotImplementedError):
        bare.parse_line("line", RunState())

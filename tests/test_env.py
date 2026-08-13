"""The typed env readers every ``from_env`` parses through."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import env as env_module
from agent_bridge.env import (
    env_bool,
    env_csv,
    env_float,
    env_int,
    env_path,
    env_str,
    env_str_or_none,
    load_env_file,
)

# --- load_env_file ---


def test_load_env_file_delegates_to_dotenv(monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(env_module, "load_dotenv", lambda: calls.append(True))
    load_env_file()
    assert calls == [True]


# --- env_str / env_str_or_none ---


@pytest.mark.parametrize("env", [{}, {"X": ""}, {"X": "   "}])
def test_str_falls_back_to_default_when_blank(env: dict[str, str]):
    assert env_str(env, "X", "fallback") == "fallback"


def test_str_trims():
    assert env_str({"X": "  value  "}, "X", "fallback") == "value"


@pytest.mark.parametrize("env", [{}, {"X": ""}, {"X": " \n "}])
def test_str_or_none_is_none_when_blank(env: dict[str, str]):
    assert env_str_or_none(env, "X") is None


def test_str_or_none_keeps_the_value_verbatim():
    # Templates and messages must survive intact — no trimming here.
    assert env_str_or_none({"X": " {cost_usd} \n"}, "X") == " {cost_usd} \n"


# --- env_bool ---


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", "TRUE", " On "])
def test_bool_truthy_spellings(raw: str):
    assert env_bool({"X": raw}, "X", False) is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "FALSE"])
def test_bool_falsy_spellings(raw: str):
    assert env_bool({"X": raw}, "X", True) is False


def test_bool_blank_uses_default():
    assert env_bool({"X": ""}, "X", True) is True
    assert env_bool({}, "X", False) is False


def test_bool_rejects_garbage_instead_of_silently_disabling():
    with pytest.raises(ValueError, match="X must be one of"):
        env_bool({"X": "maybe"}, "X", False)


# --- env_int / env_float ---


def test_int_parses_and_defaults():
    assert env_int({"X": " 7 "}, "X", 1) == 7
    assert env_int({}, "X", 1) == 1


def test_int_rejects_non_integer():
    with pytest.raises(ValueError, match="X must be an integer, got 'abc'"):
        env_int({"X": "abc"}, "X", 1)


def test_float_parses_and_defaults():
    assert env_float({"X": "1.5"}, "X", 0.0) == 1.5
    assert env_float({}, "X", 0.5) == 0.5


def test_float_rejects_non_number():
    with pytest.raises(ValueError, match="X must be a number, got 'abc'"):
        env_float({"X": "abc"}, "X", 1.0)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e400"])
def test_float_rejects_non_finite(raw: str):
    # nan/inf parse fine but slip through every `<= 0` range check downstream
    # (nan compares False against everything), so they must die at the boundary.
    with pytest.raises(ValueError, match="must be a finite number"):
        env_float({"X": raw}, "X", 1.0)


# --- env_path / env_csv ---


def test_path_parses_and_defaults():
    assert env_path({"X": "./a/b.json"}, "X", "./d.json") == Path("./a/b.json")
    assert env_path({}, "X", "./d.json") == Path("./d.json")


def test_csv_splits_trims_and_drops_blanks():
    assert env_csv({"X": " a , b ,, c "}, "X") == ("a", "b", "c")


@pytest.mark.parametrize("env", [{}, {"X": ""}, {"X": " , "}])
def test_csv_blank_is_empty(env: dict[str, str]):
    assert env_csv(env, "X") == ()

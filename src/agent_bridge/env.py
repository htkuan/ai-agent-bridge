"""The single place that reads environment values.

Every ``{Component}Config.from_env`` parses through these readers, so truthy
rules, blank handling and error messages stay identical across layers. Readers
take an explicit mapping, so tests can pass a plain dict instead of mutating
the process environment (and stay immune to a developer's local ``.env``).

``.env`` loading happens once, in ``AppConfig.from_env`` — nowhere else.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

type Env = Mapping[str, str]

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def load_env_file() -> None:
    load_dotenv()


def env_str(env: Env, name: str, default: str) -> str:
    """Trimmed value; unset or blank falls back to ``default``."""
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def env_str_or_none(env: Env, name: str) -> str | None:
    """Verbatim value (no trimming — templates and messages keep their shape);
    unset or blank yields None."""
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value


def env_bool(env: Env, name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(
        f"{name} must be one of {', '.join(sorted(_TRUE | _FALSE))}, got {raw!r}"
    )


def env_int(env: Env, name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def env_float(env: Env, name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    # nan/inf parse fine but slip through every `<= 0` range check downstream
    # (nan compares False against everything), surfacing much later as an
    # opaque failure deep inside a component. Reject them at the boundary.
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


def env_path(env: Env, name: str, default: str) -> Path:
    return Path(env_str(env, name, default))


def env_csv(env: Env, name: str) -> tuple[str, ...]:
    """Comma-separated list; blank items dropped, each item trimmed."""
    return tuple(item.strip() for item in env.get(name, "").split(",") if item.strip())


# Live process environment — the default source for every ``from_env``.
# Bound as a default argument, so it still reflects later mutations.
PROCESS_ENV: Env = os.environ

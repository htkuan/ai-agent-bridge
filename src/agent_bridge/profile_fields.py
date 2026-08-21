"""Typed field readers for tables in the profiles file.

The TOML counterpart of ``env.py``: one type-checking rule and one
error-message shape per field kind, shared by every config that parses
``[<agent>.profiles.<name>]`` tables. ``where`` names the table a value came
from (e.g. ``claude.profiles.backend``) so messages point at the exact line
to fix.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import cast


def field_str(where: str, table: Mapping[str, object], key: str, default: str) -> str:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}.{key} must be a non-empty string, got {value!r}")
    return value


def field_opt_str(
    where: str, table: Mapping[str, object], key: str, default: str | None
) -> str | None:
    # No TOML spelling for "reset to unset" — absent means inherit.
    if key not in table:
        return default
    return field_str(where, table, key, "")


def field_path(
    where: str, table: Mapping[str, object], key: str, default: Path
) -> Path:
    if key not in table:
        return default
    # An empty path would resolve to the process cwd — the exact dangerous
    # fallback work_dir exists to avoid — so field_str's non-empty check
    # is load-bearing here.
    return Path(field_str(where, table, key, "")).resolve()


def field_number(
    where: str, table: Mapping[str, object], key: str, default: float
) -> float:
    if key not in table:
        return default
    value = table[key]
    # bool is an int subclass — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where}.{key} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{where}.{key} must be a finite number, got {value!r}")
    return float(value)


def field_bool(
    where: str, table: Mapping[str, object], key: str, default: bool
) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ValueError(f"{where}.{key} must be a boolean, got {value!r}")
    return value


def field_str_tuple(
    where: str, table: Mapping[str, object], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, list):
        raise ValueError(f"{where}.{key} must be an array of strings, got {value!r}")
    items = cast("list[object]", value)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(
            f"{where}.{key} must be an array of non-empty strings, got {value!r}"
        )
    return tuple(cast("list[str]", items))

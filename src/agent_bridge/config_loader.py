from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILENAME = "agent-bridge.yaml"

# `$$(` escapes to a literal `$(`; `$(VAR)` expands to os.environ["VAR"].
# Alternation order matters: the escape must win at the same position.
_SECRET_PATTERN = re.compile(r"\$\$\(|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


class ConfigSource:
    """Merged configuration view: env var > YAML value > default.

    ``get(env_key, yaml_path, default)`` returns a string (env-var semantics —
    callers parse types exactly as they would for an env var) or None.
    ``yaml_path`` is a dotted path into the nested YAML mapping
    (e.g. ``platforms.slack.bot_token``).
    """

    def __init__(
        self,
        data: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        path: Path | None = None,
    ) -> None:
        self._data: dict[str, Any] = dict(data or {})
        # None ⇒ read os.environ live (so load_dotenv() calls are honoured).
        self._env = env
        self.path = path

    @classmethod
    def empty(cls) -> ConfigSource:
        return cls({})

    def get(self, env_key: str, yaml_path: str, default: str | None = None) -> str | None:
        env = os.environ if self._env is None else self._env
        # Empty-string env values are treated as unset: .env templates commonly
        # ship `KEY=` placeholders which must not shadow YAML values.
        value = env.get(env_key)
        if value:
            return value
        yaml_value = self._lookup(yaml_path)
        if yaml_value is not None:
            return yaml_value
        return default

    def _lookup(self, yaml_path: str) -> str | None:
        node: Any = self._data
        for part in yaml_path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return _stringify(yaml_path, node)


def _stringify(yaml_path: str, value: Any) -> str | None:
    match value:
        case None:
            return None
        case bool():
            return "true" if value else "false"
        case str():
            return value
        case int() | float():
            return str(value)
        case list():
            items = [_stringify(yaml_path, item) for item in value]
            if any(item is None for item in items):
                raise ValueError(f"Config key {yaml_path!r} contains a null list item")
            return ",".join(items)  # type: ignore[arg-type]
        case _:
            raise ValueError(
                f"Config key {yaml_path!r} must be a scalar or list of scalars, "
                f"got {type(value).__name__}"
            )


def substitute_secrets(data: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace ``$(VAR)`` in every string value with the env var's value.

    ``$$(`` escapes to a literal ``$(``. All missing variables across the whole
    document are collected and reported in a single ValueError.
    """
    env = os.environ if env is None else env
    missing: list[str] = []

    def _substitute_str(text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            if match.group(0) == "$$(":
                return "$("
            var = match.group(1)
            if var not in env:
                missing.append(var)
                return match.group(0)
            return env[var]

        return _SECRET_PATTERN.sub(_replace, text)

    def _walk(node: Any) -> Any:
        match node:
            case str():
                return _substitute_str(node)
            case Mapping():
                return {key: _walk(value) for key, value in node.items()}
            case list():
                return [_walk(item) for item in node]
            case _:
                return node

    result = _walk(data)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ValueError(f"Config file references undefined environment variables: {names}")
    return result


def load_config_source(config_path: Path | None = None) -> ConfigSource:
    """Discover and load the YAML config file into a ConfigSource.

    Discovery order: explicit ``config_path`` (CLI) > ``AGENT_BRIDGE_CONFIG``
    env var > ``./agent-bridge.yaml`` if present > pure-env mode (empty YAML).
    An explicitly specified path that does not exist is a startup error.
    """
    path = config_path
    if path is None:
        env_path = os.environ.get("AGENT_BRIDGE_CONFIG")
        if env_path:
            path = Path(env_path)
    if path is not None:
        if not path.is_file():
            raise ValueError(f"Config file not found: {path}")
    else:
        candidate = Path(DEFAULT_CONFIG_FILENAME)
        if candidate.is_file():
            path = candidate
        else:
            return ConfigSource.empty()

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file {path}: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"Config file {path} must contain a top-level mapping, got {type(raw).__name__}"
        )
    return ConfigSource(substitute_secrets(raw), path=path)

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from slack_sdk.errors import SlackApiError


@dataclass(frozen=True)
class SlackCall:
    method: str
    kwargs: dict[str, Any]


class FakeSlackClient:
    """In-memory double for the Slack Web API surface the adapter uses.

    Records every call in ``calls``, mints monotonically increasing ``ts``
    values, and tracks the visible message state (post/update/delete) in
    ``messages`` so tests can assert what a Slack user ends up seeing.

    Queue an error with ``fail_next["chat_update"] = "ratelimited"`` — the
    next call of that method raises ``SlackApiError`` (once) with that error
    code in its response.
    """

    def __init__(
        self,
        *,
        workspace: str = "acme",
        channel_names: dict[str, str] | None = None,
        user_names: dict[str, str] | None = None,
        bot_user_id: str = "UBOT",
    ) -> None:
        self.workspace = workspace
        self.channel_names: dict[str, str] = channel_names or {}
        self.user_names: dict[str, str] = user_names or {}
        self.bot_user_id = bot_user_id
        self.calls: list[SlackCall] = []
        self.messages: dict[tuple[str, str], str] = {}  # (channel, ts) -> text
        self.uploads: list[dict[str, Any]] = []
        self.fail_next: dict[str, str] = {}  # method -> slack error code
        self._seq = 0

    # -- helpers ---------------------------------------------------------

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append(SlackCall(method, kwargs))
        code = self.fail_next.pop(method, None)
        if code is not None:
            raise SlackApiError(
                f"{method} failed", response={"ok": False, "error": code}
            )

    def _next_ts(self) -> str:
        self._seq += 1
        return f"{1700000000 + self._seq}.{self._seq:06d}"

    def calls_to(self, method: str) -> list[SlackCall]:
        return [c for c in self.calls if c.method == method]

    def say_for(self, channel: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        """A bolt-style ``say`` callback bound to a channel."""

        async def say(**kwargs: Any) -> dict[str, Any]:
            return await self.chat_postMessage(channel=channel, **kwargs)

        return say

    # -- Web API surface used by the adapter ------------------------------

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postMessage", kwargs)
        ts = self._next_ts()
        channel = str(kwargs.get("channel", ""))
        self.messages[(channel, ts)] = str(kwargs.get("text", ""))
        return {"ok": True, "channel": channel, "ts": ts}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_update", kwargs)
        channel = str(kwargs.get("channel", ""))
        ts = str(kwargs.get("ts", ""))
        self.messages[(channel, ts)] = str(kwargs.get("text", ""))
        return {"ok": True, "channel": channel, "ts": ts}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_delete", kwargs)
        channel = str(kwargs.get("channel", ""))
        ts = str(kwargs.get("ts", ""))
        self.messages.pop((channel, ts), None)
        return {"ok": True}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._record("files_upload_v2", kwargs)
        self.uploads.append(kwargs)
        return {"ok": True}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs.get("channel", ""))
        return {"ok": True, "channel": {"name": self.channel_names.get(channel)}}

    async def team_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("team_info", kwargs)
        return {"ok": True, "team": {"name": self.workspace}}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        user = str(kwargs.get("user", ""))
        name = self.user_names.get(user)
        return {
            "ok": True,
            "user": {"profile": {"display_name": name, "real_name": name}},
        }

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {"ok": True, "user_id": self.bot_user_id, "user": "bridge-bot"}


class FakeBoltApp:
    """Captures handlers registered via ``@app.event(...)`` for direct calls."""

    def __init__(self, client: FakeSlackClient | None = None) -> None:
        self.client: FakeSlackClient = client or FakeSlackClient()
        self.handlers: dict[str, Callable[..., Awaitable[None]]] = {}

    def event(
        self, name: str
    ) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
        def register(
            fn: Callable[..., Awaitable[None]],
        ) -> Callable[..., Awaitable[None]]:
            self.handlers[name] = fn
            return fn

        return register


# -- Slack event payload builders -----------------------------------------


def _base_event(
    text: str,
    channel: str,
    user: str,
    ts: str,
    thread_ts: str | None,
    files: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "text": text,
        "channel": channel,
        "user": user,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if files is not None:
        event["files"] = files
    return event


def mention_event(
    text: str = "<@UBOT> hello",
    *,
    channel: str = "C123",
    user: str = "U123",
    ts: str = "1700000000.000100",
    thread_ts: str | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An ``app_mention`` event payload as bolt delivers it."""
    return _base_event(text, channel, user, ts, thread_ts, files)


def dm_event(
    text: str = "hello",
    *,
    channel: str = "D123",
    user: str = "U123",
    ts: str = "1700000000.000100",
    thread_ts: str | None = None,
    files: list[dict[str, Any]] | None = None,
    channel_type: str = "im",
    bot_id: str | None = None,
    subtype: str | None = None,
) -> dict[str, Any]:
    """A ``message`` event payload (DM) as bolt delivers it.

    ``bot_id`` / ``subtype`` are included when set so tests can exercise the
    adapter's skip paths.
    """
    event = _base_event(text, channel, user, ts, thread_ts, files)
    event["channel_type"] = channel_type
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    return event

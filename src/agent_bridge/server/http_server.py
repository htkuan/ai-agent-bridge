"""HttpServer — the shared HTTP host every HTTP-facing component mounts onto.

Pure infrastructure: owns a FastAPI app and the embedded uvicorn lifecycle,
and knows nothing about the bridge or any platform. ``app.py`` builds it,
lets adapters register their routers, and starts it last — the server
accepting traffic is the final step of startup, and the first to stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Generator

try:
    import uvicorn
    from fastapi import APIRouter, FastAPI
except ImportError:
    raise ImportError(
        "HTTP dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[http]"
    ) from None

from agent_bridge.server.config import HttpConfig
from agent_bridge.server.console import build_console_router

logger = logging.getLogger(__name__)

# How long start() waits for uvicorn to come up before declaring failure.
STARTUP_TIMEOUT_SECONDS = 10.0


class _NoSignalServer(uvicorn.Server):
    """``app.py`` owns SIGINT/SIGTERM; keep uvicorn from capturing them."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class HttpServer:
    def __init__(self, config: HttpConfig) -> None:
        self._config = config
        # Docs endpoints off: this server fronts an agent, not a public API.
        self._app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._app.include_router(build_console_router())
        self._server: _NoSignalServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None

    @property
    def app(self) -> FastAPI:
        """The underlying ASGI app — tests drive it in-process."""
        return self._app

    @property
    def port(self) -> int:
        """The bound port — differs from config when it asked for port 0."""
        if self._sock is None:
            raise RuntimeError("HTTP server is not started")
        return int(self._sock.getsockname()[1])

    def include_router(self, router: APIRouter) -> None:
        """Mount a component's routes. Must happen before ``start()``."""
        self._app.include_router(router)

    async def start(self) -> None:
        if self._serve_task is not None:
            raise RuntimeError("HttpServer.start() called twice")
        # Bind here, not inside uvicorn: a bad host/port then fails as a
        # plain exception on the startup path instead of uvicorn's
        # sys.exit(1) from inside the serve task.
        self._sock = self._bind()
        uv_config = uvicorn.Config(
            self._app,
            host=self._config.host,
            port=self._config.port,
            # Leave logging to app.py's basicConfig — uvicorn's own
            # dictConfig would replace the root handlers.
            log_config=None,
        )
        server = _NoSignalServer(uv_config)
        self._server = server
        self._serve_task = asyncio.create_task(server.serve(sockets=[self._sock]))

        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
        while not server.started:
            if self._serve_task.done() or (
                asyncio.get_running_loop().time() > deadline
            ):
                exc = self._serve_task.exception() if self._serve_task.done() else None
                raise RuntimeError(
                    "HTTP server failed to start on "
                    f"{self._config.host}:{self._config.port}"
                ) from exc
            await asyncio.sleep(0.02)
        logger.info(
            "HTTP server listening on http://%s:%d", self._config.host, self.port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await self._serve_task
            except Exception:
                # Shutdown path: log rather than mask whatever triggered it.
                logger.exception("HTTP server exited with an error")
        self._serve_task = None
        self._server = None
        self._sock = None

    def _bind(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self._config.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        try:
            sock.bind((self._config.host, self._config.port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                "HTTP server could not bind "
                f"{self._config.host}:{self._config.port}: {exc}"
            ) from exc
        return sock

"""Console routes: the operator-facing page and the health endpoint.

Deliberately free of bridge/platform imports. When the console grows real
status views, ``app.py`` will inject read-only provider callables — this
module must never reach into other layers itself.
"""

from __future__ import annotations

from importlib import metadata

try:
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
except ImportError:
    raise ImportError(
        "HTTP dependencies are not installed. "
        "Install them with: pip install ai-agent-bridge[http]"
    ) from None

_CONSOLE_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Bridge Console</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 40rem;
           margin: 4rem auto; padding: 0 1rem; color: #222; }
    code { background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>Agent Bridge</h1>
  <p>The bridge is running. This console is a placeholder — status and
     session views will land here.</p>
  <p>Health check: <a href="/api/health"><code>GET /api/health</code></a></p>
</body>
</html>
"""


def _version() -> str:
    try:
        return metadata.version("ai-agent-bridge")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_console_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    # Never called by name: the decorator registers it with the router.
    async def console_page() -> str:  # pyright: ignore[reportUnusedFunction]
        return _CONSOLE_PAGE

    @router.get("/api/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "version": _version()}

    return router

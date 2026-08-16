# The shared HTTP server

`src/agent_bridge/server/` is **infrastructure, not a platform**: a single
FastAPI app + embedded uvicorn that every HTTP-facing component mounts routes
onto. One process, one port, many routers.

```
                 ┌──────────────────────────────────┐
                 │ HttpServer (FastAPI + uvicorn)   │
GET /            │ ├─ console routes (built-in)     │
GET /api/health  │ │                                │
POST /platforms/ │ ├─ WebhookAdapter.router ────────┼──▶ Bridge ──▶ Agent
     webhook/... │ │                                │
                 │ └─ future platform routers …     │
                 └──────────────────────────────────┘
```

## Design rules

- `server/` imports nothing from `bridge/`, `agents/`, or `platforms/`. It
  hosts routers; it does not know what they do.
- Platforms that need HTTP own an `APIRouter` (with their own prefix, e.g.
  `/platforms/webhook`) and `app.py` mounts it via `include_router()` —
  `app.py` stays the only module that knows all the pieces.
- Routers must be registered **before** `start()`. `app.py` guarantees this by
  building every adapter first and starting the server last; on shutdown the
  server stops first (no new requests), then the adapters.
- When the console needs real data (sessions, usage), `app.py` will inject
  read-only provider callables — `server/` must never reach into other layers
  itself.

## Configuration

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `AGENT_BRIDGE_HTTP_ENABLED` | No | `false` | Master switch for the whole server |
| `AGENT_BRIDGE_HTTP_HOST` | No | `127.0.0.1` | Loopback by default — exposing it is an explicit decision |
| `AGENT_BRIDGE_HTTP_PORT` | No | `8080` | `0` asks the OS for a free port (`HttpServer.port` reports it) |

Dependencies come from the `http` extra: `pip install ai-agent-bridge[http]`
(fastapi, uvicorn, httpx).

## Built-in routes

| Route | Purpose |
|-------|---------|
| `GET /` | Console page — a placeholder today; status/session views land here |
| `GET /api/health` | `{"status": "ok", "version": "<package version>"}` — liveness probe |

## Lifecycle notes

- The socket is bound by `HttpServer.start()` itself (not by uvicorn), so a
  taken port fails fast as a clean `RuntimeError` on the startup path instead
  of uvicorn's `sys.exit(1)` inside a background task.
- uvicorn's signal capture is disabled — `app.py` owns SIGINT/SIGTERM.
- uvicorn logging is left to the app's `logging.basicConfig` (`log_config=None`).
- Tests drive the ASGI app in-process via `HttpServer.app` + httpx's
  `ASGITransport`; real-socket lifecycle tests bind port 0.

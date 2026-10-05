"""The TRIPY web application: the only production process (scripts/start.sh, ONE Uvicorn worker).

    uvicorn src.api.app:app --host 0.0.0.0 --port "$PORT"

Startup builds the process's state once (deps.build_context: data paths, the vehicle catalog, the process-wide
RunManager with its startup reconciliation and the shared ConcurrencyController). Shutdown (SIGTERM on a redeploy)
asks active runs to stop at their next safe point (RunManager.shutdown); runs are never resumed after a restart, the
next start reconciles them as INTERRUPTED. One process owns a TRIPY_DATA_DIR: never run a second worker or a second
server against the same data directory.

    /health      public, constant, no I/O
    /api/*       `Authorization: Bearer <TRIPY_ACCESS_TOKEN>` in production (auth.py)
    /mcp/<token> the read-only MCP, only when TRIPY_MCP_TOKEN is set (src/mcp_server/asgi.py)
    everything else: the React production bundle (frontend/dist, frontend.py) with its SPA fallback; required in
                 production, optional elsewhere (Python tests, the Vite dev server in front of the API)
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..app_config import is_production
from ..server_logging import get_logger, redact_server_logs
from .auth import require_access
from .deps import ApiContext, build_context, env_secret
from .errors import ApiError
from .frontend import FRONTEND_DIST, SecurityHeaders, frontend_routes
from .routes import bakeoffs, config, documents, exports, health, runs, series, technical

log = get_logger("api")


def _error(status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status, headers=headers)


AUTO = "auto"


def _frontend_dist(frontend: Path | str | None, secret: Callable[[str], str]) -> Path | None:
    """The bundle to serve: "auto" = TRIPY_FRONTEND_DIST when set (required), else frontend/dist, required in
    production (a missing bundle fails startup instead of serving a broken site) and optional elsewhere; None = no
    frontend; a path = that bundle, required."""
    if frontend is None:
        return None
    if frontend == AUTO:
        override = (secret("TRIPY_FRONTEND_DIST") or "").strip()
        if override:
            return Path(override)
        if not is_production(secret) and not (FRONTEND_DIST / "index.html").is_file():
            log.info("frontend/dist not built: serving the API only (development)")
            return None
        return FRONTEND_DIST
    return Path(frontend)


def create_app(*, context: ApiContext | None = None, secret: Callable[[str], str] = env_secret,
               mount_mcp: bool | None = None, frontend: Path | str | None = AUTO) -> FastAPI:
    """`context`: an already built ApiContext (tests); otherwise one is built at startup. `mount_mcp`: serve the
    read-only MCP (default: when TRIPY_MCP_TOKEN is set). `frontend`: see _frontend_dist."""
    from ..mcp_server import configured_token

    redact_server_logs()
    dist = _frontend_dist(frontend, secret)

    mcp_routes = []
    mcp_lifespan = None
    if mount_mcp if mount_mcp is not None else bool(configured_token(secret)):
        from ..mcp_server.asgi import mcp_lifespan as _mcp_lifespan, mcp_routes as _mcp_routes

        mcp_routes, mcp_lifespan = _mcp_routes(), _mcp_lifespan

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        built = None
        if getattr(app.state, "tripy", None) is None:
            built = build_context(secret)
            app.state.tripy = built
        async with AsyncExitStack() as stack:
            if mcp_lifespan is not None:
                await stack.enter_async_context(mcp_lifespan(app))
            try:
                yield
            finally:
                if built is not None and built.owns_manager:
                    built.manager.shutdown()

    app = FastAPI(title="TRIPY API", version="1", lifespan=lifespan,
                  description="HTTP interface over the TRIPY research engine (one process-wide RunManager over the "
                              "durable run state).")
    app.state.secret = secret
    app.state.tripy = context

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc") or []), "msg": e.get("msg"), "type": e.get("type")}
                   for e in exc.errors()]          # never echoes the submitted input
        return _error(422, "invalid_request", "The request is invalid.", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("api %s %s failed: %s", request.method, request.url.path, type(exc).__name__, exc_info=exc)
        return _error(500, "internal_error", "An unexpected error occurred.")

    app.include_router(health.router)
    protected = [Depends(require_access)]
    for module in (runs, exports, config, series, documents, technical, bakeoffs):
        app.include_router(module.router, dependencies=protected)
    app.router.routes.extend(mcp_routes)
    if dist is not None:          # last: the SPA fallback never shadows /api, /health, /mcp or /assets
        app.router.routes.extend(frontend_routes(dist))
    app.add_middleware(SecurityHeaders)
    return app


app = create_app()

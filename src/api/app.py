"""The TRIPY HTTP API application.

    uvicorn src.api.app:app --host 0.0.0.0 --port 8000

Startup builds the same state the dashboard builds (deps.build_context: data paths, the vehicle catalog, the
process-wide RunManager with its startup reconciliation and the shared ConcurrencyController). Shutdown asks active
runs to stop at their next safe point (RunManager.shutdown), as the dashboard does on SIGTERM.

/health is public; every /api route requires `Authorization: Bearer <TRIPY_ACCESS_TOKEN>` in production (auth.py).
With TRIPY_MCP_TOKEN set, the read-only MCP is mounted at /mcp/<token> with the exact route and lifespan the
Streamlit launcher (tripy_server.py) uses (src/mcp_server/asgi.py).
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..server_logging import get_logger
from .auth import require_access
from .deps import ApiContext, build_context, env_secret
from .errors import ApiError
from .routes import config, exports, health, runs

log = get_logger("api")


def _error(status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status, headers=headers)


def create_app(*, context: ApiContext | None = None, secret: Callable[[str], str] = env_secret,
               mount_mcp: bool | None = None) -> FastAPI:
    """`context`: an already built ApiContext (tests); otherwise one is built at startup. `mount_mcp`: serve the
    read-only MCP (default: when TRIPY_MCP_TOKEN is set, as scripts/start.sh decides)."""
    from ..mcp_server import configured_token

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
                  description="HTTP interface over the TRIPY research engine (the same RunManager and run state "
                              "as the Streamlit dashboard).")
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
    for module in (runs, exports, config):
        app.include_router(module.router, dependencies=protected)
    app.router.routes.extend(mcp_routes)
    return app


app = create_app()

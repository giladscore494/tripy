"""The /mcp route and its lifespan, mounted into the FastAPI application (src/api/app.py) when TRIPY_MCP_TOKEN is set.

The secret path is the credential (claude.ai custom connectors take a URL and no header): exactly
`/mcp/<TRIPY_MCP_TOKEN>` reaches the MCP handler, compared in constant time. Every other path under /mcp (any case:
wrong token, missing token, a prefix of it, a different case, a trailing segment) and every WebSocket gets 404 with an
empty body. The MCP handler sees a scope whose path is "/mcp": the token never reaches the SDK or any log line.
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from starlette.responses import Response
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import ASGIApp, Receive, Scope, Send

from ..server_logging import get_logger
from . import MOUNT_PREFIX, configured_token, token_problem

log = get_logger("mcp")


class McpRoute(BaseRoute):
    """Claims every path equal to /mcp or below /mcp/ (case-insensitive) so nothing there falls through to the
    React SPA fallback; serves only the exact secret path."""

    def __init__(self, token: str, handler: ASGIApp | None):
        self.path = MOUNT_PREFIX
        self._expected = f"{MOUNT_PREFIX}/{token}".encode("utf-8") if token else None
        self._handler = handler

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            path = str(scope.get("path") or "").lower()
            if path == MOUNT_PREFIX or path.startswith(MOUNT_PREFIX + "/"):
                return Match.FULL, {}
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any):
        raise NoMatchFound(name, path_params)

    def authorized(self, path: str) -> bool:
        if self._expected is None or self._handler is None:
            return False
        return hmac.compare_digest(path.encode("utf-8", errors="replace"), self._expected)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.authorized(str(scope.get("path") or "")):
            inner = dict(scope)
            inner["path"], inner["raw_path"] = MOUNT_PREFIX, MOUNT_PREFIX.encode()
            await self._handler(inner, receive, send)
            return
        if scope["type"] == "websocket":
            if "websocket.http.response" in (scope.get("extensions") or {}):      # deny with a real 404
                await send({"type": "websocket.http.response.start", "status": 404, "headers": []})
                await send({"type": "websocket.http.response.body", "body": b""})
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await Response(status_code=404)(scope, receive, send)


class _Mcp:
    """The process's MCP server, built once (only when the launcher runs, i.e. TRIPY_MCP_TOKEN is set)."""

    def __init__(self) -> None:
        self.session_manager = None
        self.built = False

    def handler(self) -> ASGIApp | None:
        if not self.built:
            self.built = True
            try:
                from .server import build

                server = build()
                server.streamable_http_app()            # creates the stateless session manager
                self.session_manager = server.session_manager
            except Exception as exc:  # noqa: BLE001 - the application must start even if the MCP cannot
                log.error("MCP disabled: could not build the server (%s: %s)", type(exc).__name__, exc)
                self.session_manager = None
        if self.session_manager is None:
            return None
        return self.session_manager.handle_request


_MCP = _Mcp()


def mcp_routes(token: str | None = None) -> list[BaseRoute]:
    """[] when TRIPY_MCP_TOKEN is unset or empty; otherwise the one /mcp route (404 for everything when the token
    is unusable, e.g. too short)."""
    token = configured_token() if token is None else token.strip()
    if not token:
        return []
    problem = token_problem(token)
    if problem:
        log.error("MCP disabled: %s; every /mcp path answers 404", problem)
        return [McpRoute("", None)]
    handler = _MCP.handler()
    if handler is not None:
        log.info("MCP enabled at %s/<TRIPY_MCP_TOKEN> (read-only, stateless Streamable HTTP)", MOUNT_PREFIX)
    return [McpRoute(token, handler)]


@asynccontextmanager
async def mcp_lifespan(_app: Any):
    """Runs the MCP session manager's task group for the server's lifetime (no-op when the MCP is not built)."""
    manager = _MCP.session_manager
    if manager is None:
        yield
        return
    async with manager.run():
        yield

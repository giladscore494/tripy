"""The production React bundle (frontend/dist), served by the same FastAPI process as the API, and the HTTP security
headers every response carries.

    GET /assets/<hashed file>    StaticFiles over dist/assets; Cache-Control: public, max-age=31536000, immutable
                                 (Vite content-hashes these names, so a deploy never changes a file in place)
    GET /<file in dist root>     that file (e.g. tripy.svg from frontend/public), short cache
    GET any other path           dist/index.html with Cache-Control: no-cache, so React Router can render the deep
                                 route (/runs/<id>, /series/<id>, /settings, ...) and a new deploy's asset manifest
                                 is picked up on the next load

Never answered with index.html (they keep their own 404 / API / MCP behavior): /api and everything below it, /health,
/mcp and everything below it, /assets and everything below it. Only dist/ is served, never frontend/ sources.
"""

from __future__ import annotations

from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Match, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
ASSET_CACHE = "public, max-age=31536000, immutable"
INDEX_CACHE = "no-cache"
ROOT_FILE_CACHE = "public, max-age=3600"
RESERVED_PREFIXES = ("api", "health", "mcp", "assets")       # first path segment, case-insensitive

# The SPA's own document: same-origin script / style / API only (Vite emits no inline script; React sets a few inline
# style attributes, hence 'unsafe-inline' for styles), never framed. Applied to index.html only, so FastAPI's /docs
# page (CDN script) and the API are unaffected.
INDEX_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
             "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
             "frame-ancestors 'none'")
SECURITY_HEADERS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"))


class FrontendMissing(RuntimeError):
    pass


def reserved(path: str) -> bool:
    first = path.lstrip("/").split("/", 1)[0].lower()
    return first in RESERVED_PREFIXES


def require_bundle(dist: Path) -> Path:
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        raise FrontendMissing(f"The React production bundle is missing: {dist}/index.html and {dist}/assets/ are "
                              "required. Build it with `npm ci && npm run build` in frontend/ (the Dockerfile does).")
    return dist


class ImmutableAssets(StaticFiles):
    """Vite's content-hashed build output: cached for a year, never revalidated."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = ASSET_CACHE
        return response


class SpaFallback(Route):
    """GET / HEAD of every path that is not reserved: a dist-root file when one has that exact name, else index.html.
    A reserved path never matches, so it keeps the router's own 404 / 405 / slash-redirect behavior."""

    def __init__(self, dist: Path):
        self.dist = dist
        self.index = dist / "index.html"
        # names fixed at startup: never resolves a client path against the filesystem
        self.root_files = {p.name: p for p in dist.iterdir() if p.is_file() and p.name != "index.html"}
        super().__init__("/{path:path}", endpoint=self.serve, methods=["GET", "HEAD"], include_in_schema=False)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or reserved(str(scope.get("path") or "")):
            return Match.NONE, {}
        return super().matches(scope)

    async def serve(self, request: Request) -> Response:
        name = request.path_params.get("path") or ""
        if name in self.root_files:
            return FileResponse(self.root_files[name], headers={"Cache-Control": ROOT_FILE_CACHE})
        return FileResponse(self.index, media_type="text/html",
                            headers={"Cache-Control": INDEX_CACHE, "Content-Security-Policy": INDEX_CSP})


def frontend_routes(dist: Path) -> list:
    require_bundle(dist)
    return [Mount("/assets", app=ImmutableAssets(directory=dist / "assets"), name="assets"), SpaFallback(dist)]


class SecurityHeaders:
    """Pure ASGI (streams pass through untouched): nosniff, no referrer, no framing on every HTTP response, and
    `Cache-Control: no-store` on /api responses that set no policy of their own (live, private data)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = str(scope.get("path") or "").lower().startswith("/api")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name.decode() not in headers:
                        headers.append(name.decode(), value.decode())
                if api and "cache-control" not in headers:
                    headers.append("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, wrapped)

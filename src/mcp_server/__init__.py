"""Read-only MCP observation channel for runs, logs and documents (optional).

Enabled by exactly one environment variable, TRIPY_MCP_TOKEN. Unset or empty: the web application (src/api/app.py)
never builds the MCP server, the `mcp` SDK is never imported and /mcp does not exist. Set: the same single process
mounts one extra route, `/mcp/<TRIPY_MCP_TOKEN>` (Streamable HTTP, stateless, JSON responses), with its lifespan.

    asgi.py    the /mcp route (secret-path gate, constant-time) and the session-manager lifespan
    server.py  the FastMCP server: tool registration, worker threads, audit log line per call
    tools.py   the tools themselves: plain functions over the same loaders the HTTP API reads, never writing anything
    safety.py  id / path validation, secret redaction, response size caps and paging

Importing this module itself has no side effects and does not import the `mcp` SDK.
"""

from __future__ import annotations

import os
import re
from typing import Callable

TOKEN_ENV = "TRIPY_MCP_TOKEN"
MOUNT_PREFIX = "/mcp"
TOKEN_MIN_LENGTH = 16
# the token is a URL path segment: unreserved characters only (openssl rand -hex 32, secrets.token_urlsafe(32), ...)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~-]+")


def configured_token(env: Callable[[str], str | None] = os.environ.get) -> str:
    """TRIPY_MCP_TOKEN as configured ("" when unset or blank)."""
    return (env(TOKEN_ENV) or "").strip()


def token_problem(token: str) -> str | None:
    """Why a configured token cannot be served (None when it can). Never includes the value."""
    if not token:
        return f"{TOKEN_ENV} is not set"
    if len(token) < TOKEN_MIN_LENGTH:
        return f"{TOKEN_ENV} is shorter than {TOKEN_MIN_LENGTH} characters"
    if not _TOKEN_CHARS.fullmatch(token):
        return f"{TOKEN_ENV} may contain only letters, digits and . _ ~ -"
    return None

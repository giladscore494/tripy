"""Bearer authentication for /api/*, with the dashboard's access rule (src/access_control.py):

    not production (TRIPY_ENV / Railway)   open, like the dashboard's gate in local development
    production, TRIPY_ACCESS_TOKEN unset   503 for every /api route (fails closed, like the dashboard)
    production, token configured           `Authorization: Bearer <TRIPY_ACCESS_TOKEN>` required, else 401

The comparison is access_control.verify_token (constant time, the token opaque). The token is only ever read from
the Authorization header, never from the URL or a cookie, and is never echoed. /health stays public.
"""

from __future__ import annotations

from fastapi import Request

from ..access_control import MISSING_DETAIL, MISSING_TITLE, access_required, expected_token, verify_token
from .errors import ApiError

UNAUTHORIZED = "Missing or invalid access token."


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def require_access(request: Request) -> None:
    lookup = request.app.state.secret
    if not access_required(lookup):
        return
    expected = expected_token(lookup)
    if not expected:
        raise ApiError(503, "access_control_not_configured", f"{MISSING_TITLE} {MISSING_DETAIL}")
    if not verify_token(bearer_token(request), expected):
        raise ApiError(401, "unauthorized", UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})

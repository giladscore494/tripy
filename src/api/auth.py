"""Bearer authentication for /api/*, with the access rule of src/access_control.py:

    not production (TRIPY_ENV / Railway)   open (local development)
    production, TRIPY_ACCESS_TOKEN unset   503 for every /api route (fails closed)
    production, token configured           `Authorization: Bearer <TRIPY_ACCESS_TOKEN>` required, else 401

The comparison is access_control.verify_token (constant time, the token opaque). The token is only ever read from
the Authorization header, never from the URL or a cookie, and is never echoed. /health stays public.

The vehicle facts API (/api/facts/v1/*, require_facts_access) also accepts TRIPY_FACTS_TOKEN, and only there: every
other /api route compares with TRIPY_ACCESS_TOKEN alone, so the facts token is a 401 anywhere else. Production with
neither token configured fails closed (503). 60 requests per minute per token (429 rate_limited).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from typing import Callable

from fastapi import Request

from ..access_control import (MISSING_DETAIL, MISSING_TITLE, access_required, expected_token, facts_token,
                              verify_token)
from .errors import ApiError

UNAUTHORIZED = "Missing or invalid access token."
FACTS_RATE_LIMIT = 60
FACTS_RATE_WINDOW_S = 60.0


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


class RateLimiter:
    """A sliding window per token (keyed by the token's sha256, never the token): `limit` requests per `window_s`."""

    def __init__(self, limit: int = FACTS_RATE_LIMIT, window_s: float = FACTS_RATE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = int(limit), float(window_s), clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, token: str | None) -> tuple[bool, int]:
        """(allowed, seconds until the oldest request leaves the window)."""
        key = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(self.window_s - (now - hits[0])) + 1)
            hits.append(now)
            return True, 0


def facts_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "facts_rate_limiter", None)
    if limiter is None:
        limiter = request.app.state.facts_rate_limiter = RateLimiter()
    return limiter


def require_facts_access(request: Request) -> str:
    """The facts API's gate. Returns the caller's role: `operator` (TRIPY_ACCESS_TOKEN, or no access control outside
    production) or `facts` (TRIPY_FACTS_TOKEN). Then the per-token rate limit."""
    lookup = request.app.state.secret
    submitted = bearer_token(request)
    if not access_required(lookup):
        role = "operator"
    else:
        operator, facts = expected_token(lookup), facts_token(lookup)
        if not operator and not facts:
            raise ApiError(503, "access_control_not_configured", f"{MISSING_TITLE} {MISSING_DETAIL}")
        as_operator = verify_token(submitted, operator)
        as_facts = verify_token(submitted, facts)            # both compared: no timing difference between them
        if not (as_operator or as_facts):
            raise ApiError(401, "unauthorized", UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
        role = "operator" if as_operator else "facts"
    allowed, retry = facts_rate_limiter(request).allow(submitted)
    if not allowed:
        raise ApiError(429, "rate_limited", f"At most {FACTS_RATE_LIMIT} requests per minute per token.",
                       headers={"Retry-After": str(retry)})
    return role

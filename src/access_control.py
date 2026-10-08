"""Operator access control for production (one shared secret: TRIPY_ACCESS_TOKEN).

Intentionally small: no user accounts, OAuth or roles. The FastAPI application enforces it on every /api route
(src/api/auth.py) and FAILS CLOSED in production:

    TRIPY_ACCESS_TOKEN missing      → 503 access_control_not_configured for every /api route
    no / wrong Bearer token         → 401 "Missing or invalid access token." (no length, partial match or detail)
    correct token                   → the request proceeds

Outside production the gate is open (local development stays convenient); access control is production-only.

The token is opaque: it is compared exactly as configured (no trimming or case folding), in constant time
(hmac.compare_digest over UTF-8 bytes). It is only ever read from the server environment and only ever sent in the
`Authorization: Bearer` header (the React workspace keeps it in sessionStorage), never in the URL (query string, path,
fragment) or a cookie, so it cannot leak through browser history, referrers or request logs. Its value is registered
with the secret redaction in src/app_config.py. There is no per-client lockout (behind Railway's proxy every client
shares one address, so a lockout would let anyone lock the operator out): the protection is a long random token.

Authentication concerns stay here: the RunManager, the research engine, diagnostics and storage know nothing of it.
"""

from __future__ import annotations

import hmac

from .app_config import Lookup, is_production

ACCESS_TOKEN_VAR = "TRIPY_ACCESS_TOKEN"
# The vehicle facts API's own token (yeda-rechev): accepted ONLY on /api/facts/v1/* (src/api/auth.require_facts_access),
# where the operator token works too. Same rules: opaque, constant-time, header only, redacted.
FACTS_TOKEN_VAR = "TRIPY_FACTS_TOKEN"

MISSING_TITLE = "Production access control is not configured."
MISSING_DETAIL = "Set TRIPY_ACCESS_TOKEN in the deployment environment."


def _get(lookup: Lookup, name: str) -> str:
    try:
        return str(lookup(name) or "")
    except Exception:  # noqa: BLE001
        return ""


def access_required(lookup: Lookup) -> bool:
    """Access control applies in production only (TRIPY_ENV=production, or a Railway deployment)."""
    return is_production(lookup)


def expected_token(lookup: Lookup) -> str:
    """The configured token, opaque (whitespace-only counts as not configured)."""
    value = _get(lookup, ACCESS_TOKEN_VAR)
    return value if value.strip() else ""


def facts_token(lookup: Lookup) -> str:
    """The configured facts token, opaque (whitespace-only counts as not configured)."""
    value = _get(lookup, FACTS_TOKEN_VAR)
    return value if value.strip() else ""


def token_configured(lookup: Lookup) -> bool:
    return bool(expected_token(lookup))


def verify_token(submitted: object, expected: str) -> bool:
    """Constant-time comparison; False when nothing is configured or nothing was submitted."""
    if not expected or not isinstance(submitted, str) or not submitted:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))

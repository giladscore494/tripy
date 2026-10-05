"""Centralized configuration: what TRIPY reads from the environment, validation, and secret redaction.

Production secrets come from the environment (Railway service variables). Nothing here ever returns, logs
or renders a secret value: checks report presence only, and `redact` scrubs secret values out of any text
(error messages, technical details) before it reaches the UI or a log line.

This module imports no UI framework; callers pass a lookup (the process environment).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .storage.paths import DataPaths, resolve_paths, storage_status

Lookup = Callable[[str], str | None]

# Values that must never be shown. DATABASE_URL / SUPABASE_DB_URL may embed a password. TRIPY_MCP_TOKEN is the secret
# path of the optional read-only MCP endpoint (src/mcp_server).
SECRET_VARS = ("GLM_API_KEY", "TRIPY_ACCESS_TOKEN", "DATABASE_URL", "SUPABASE_DB_URL", "TRIPY_MCP_TOKEN")

REQUIRED_VARS = ("GLM_API_KEY", "GLM_MODEL")
# Strongly recommended on Railway (the app runs without them, but state is then not persistent).
DEPLOYMENT_VARS = ("TRIPY_DATA_DIR", "TRIPY_ENV", "TRIPY_ACCESS_TOKEN")
ACCESS_TOKEN_MIN_LENGTH = 16
OPTIONAL_VARS = ("GLM_FINALIZER_MODEL", "SEARCH_BACKEND", "DATABASE_URL", "TARGET_MARKET",
                 "TRIPY_MAX_ACTIVE_RUNS", "TRIPY_LOG_LEVEL", "TRIPY_SHUTDOWN_GRACE_S")
SEARCH_BACKENDS = ("glm", "duckduckgo")


def env_lookup(name: str) -> str | None:
    return os.environ.get(name)


def _get(lookup: Lookup, name: str) -> str:
    try:
        return str(lookup(name) or "").strip()
    except Exception:
        return ""


def on_railway(lookup: Lookup = env_lookup) -> bool:
    return any(_get(lookup, n) for n in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))


def is_production(lookup: Lookup = env_lookup) -> bool:
    """TRIPY_ENV=production (set by the Dockerfile), or any Railway deployment."""
    env = _get(lookup, "TRIPY_ENV").lower()
    if env:
        return env in ("prod", "production")
    return on_railway(lookup)


def max_active_runs(lookup: Lookup = env_lookup) -> int:
    try:
        return max(1, int(_get(lookup, "TRIPY_MAX_ACTIVE_RUNS") or 1))
    except ValueError:
        return 1


# --- redaction ---------------------------------------------------------------------------------------

_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/=]{6,}")
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s/]+)(@)")
_KEY_FIELD = re.compile(r"(?i)(['\"]?(?:api[_-]?key|authorization|password|secret|token)['\"]?\s*[:=]\s*['\"]?)"
                        r"([^'\"\s,}]{4,})")


def redact(text: object, lookup: Lookup = env_lookup) -> str:
    """Remove secret values from text before it is shown or logged. Never raises."""
    out = "" if text is None else str(text)
    for name in SECRET_VARS:
        value = _get(lookup, name)
        if len(value) >= 4:
            out = out.replace(value, f"[{name} redacted]")
    out = _BEARER.sub(r"\1[redacted]", out)
    out = _URL_PASSWORD.sub(r"\1[redacted]\3", out)
    out = _KEY_FIELD.sub(r"\1[redacted]", out)
    return out


def redact_obj(value, lookup: Lookup = env_lookup):
    """`redact` applied to every string inside a JSON-like value (for technical views)."""
    if isinstance(value, str):
        return redact(value, lookup)
    if isinstance(value, dict):
        return {k: redact_obj(v, lookup) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, lookup) for v in value]
    return value


# --- validation --------------------------------------------------------------------------------------

@dataclass
class Check:
    name: str          # GLM | Search | Persistent storage | Level 1.5 data | Configuration
    level: str         # ok | warning | error
    status: str        # short, e.g. "Configured", "Missing GLM_API_KEY"
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def validate_config(lookup: Lookup = env_lookup, paths: DataPaths | None = None, *,
                    ui_api_key: bool = False) -> list[Check]:
    """Startup / per-render configuration validation. Presence only: never returns a secret value.

    `ui_api_key`: a key was typed into the UI (local development only)."""
    checks: list[Check] = []
    key = bool(_get(lookup, "GLM_API_KEY")) or ui_api_key
    model = _get(lookup, "GLM_MODEL")
    missing = [n for n, ok in (("GLM_API_KEY", key), ("GLM_MODEL", model)) if not ok]
    if missing:
        checks.append(Check("GLM", "error", "Missing " + ", ".join(missing),
                            "Set " + " and ".join(missing) + " as service variables (Railway → Variables)."))
    else:
        finalizer = _get(lookup, "GLM_FINALIZER_MODEL")
        detail = f"research model {model}" + (f", finalizer {finalizer}" if finalizer else "")
        checks.append(Check("GLM", "ok", "Configured", detail))
    backend = (_get(lookup, "SEARCH_BACKEND") or "glm").lower()
    if backend not in SEARCH_BACKENDS:
        checks.append(Check("Search", "error", f"Unknown SEARCH_BACKEND {backend!r}",
                            "Use glm or duckduckgo."))
    elif backend == "glm" and not key:
        checks.append(Check("Search", "error", "Missing GLM_API_KEY", "GLM web search uses the GLM API key."))
    else:
        engine = _get(lookup, "GLM_SEARCH_ENGINE") or "search-prime"
        checks.append(Check("Search", "ok", "Configured",
                            f"GLM web_search ({engine})" if backend == "glm" else "DuckDuckGo (keyless)"))
    storage = storage_status(paths or resolve_paths(lookup), lookup)
    checks.append(Check("Persistent storage", storage["level"],
                        {"ok": "Connected", "warning": "Not persistent", "error": "Unavailable"}[storage["level"]],
                        storage["message"]))
    dsn = _get(lookup, "DATABASE_URL") or _get(lookup, "SUPABASE_DB_URL")
    checks.append(Check("Level 1.5 data", "ok", "Database" if dsn else "Bundled snapshot",
                        "DATABASE_URL is set" if dsn else "DATABASE_URL not set: the frozen snapshot in data/ is used"))
    token = _get(lookup, "TRIPY_ACCESS_TOKEN")
    if is_production(lookup):
        if not token:
            checks.append(Check("Access control", "error", "Missing TRIPY_ACCESS_TOKEN",
                                "Production access control is not configured. Set TRIPY_ACCESS_TOKEN in the deployment "
                                "environment."))
        elif len(token) < ACCESS_TOKEN_MIN_LENGTH:
            checks.append(Check("Access control", "warning", "Weak TRIPY_ACCESS_TOKEN",
                                f"Use a random token of at least {ACCESS_TOKEN_MIN_LENGTH} characters, e.g. "
                                "python -c \"import secrets; print(secrets.token_urlsafe(32))\"."))
        else:
            checks.append(Check("Access control", "ok", "Enabled", "The API requires TRIPY_ACCESS_TOKEN"))
    problems = []
    raw_extra = _get(lookup, "GLM_EXTRA_BODY")
    if raw_extra:
        try:
            if not isinstance(json.loads(raw_extra), dict):
                problems.append("GLM_EXTRA_BODY must be a JSON object")
        except ValueError as exc:
            problems.append(f"GLM_EXTRA_BODY is not valid JSON ({exc.msg})")
    for name in ("GLM_CHAT_MAX_ATTEMPTS", "GLM_SEARCH_MAX_ATTEMPTS", "PRIMARY_RESEARCH_MAX_TURNS", "BATCH_MAX_WORKERS",
                 "TRIPY_MAX_ACTIVE_RUNS"):
        raw = _get(lookup, name)
        if raw and not raw.lstrip("-").isdigit():
            problems.append(f"{name} must be an integer (got a non-numeric value)")
    raw = _get(lookup, "GLM_CHAT_TIMEOUT_S")
    if raw:
        try:
            float(raw)
        except ValueError:
            problems.append("GLM_CHAT_TIMEOUT_S must be a number")
    if problems:
        checks.append(Check("Configuration", "error", "Invalid value", "; ".join(problems)))
    return checks


def blocking_errors(checks: list[Check]) -> list[Check]:
    """Checks that must pass before a research run can start (storage must be writable, GLM configured)."""
    return [c for c in checks if c.level == "error" and c.name != "Level 1.5 data"]


# --- reachability (no credentials sent) ------------------------------------------------------------------

_REACH: dict[str, tuple[float, bool, str]] = {}
_REACH_LOCK = threading.Lock()


def endpoint_reachable(base_url: str, *, ttl_s: float = 300.0, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Network reachability of the GLM base URL. Sends NO credentials (any HTTP answer, even 401/404, proves the
    host is reachable); cached for `ttl_s`. This is not an authentication check."""
    now = time.monotonic()
    with _REACH_LOCK:
        hit = _REACH.get(base_url)
        if hit and now - hit[0] < ttl_s:
            return hit[1], hit[2]
    try:
        import requests

        resp = requests.head(base_url, timeout=timeout_s, allow_redirects=False)
        ok, note = True, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        ok, note = False, type(exc).__name__
    with _REACH_LOCK:
        _REACH[base_url] = (now, ok, note)
    return ok, note


def diagnostics(lookup: Lookup = env_lookup) -> dict:
    """Startup diagnostics safe to print: presence flags and paths, never values of secrets."""
    paths = resolve_paths(lookup)
    checks = validate_config(lookup, paths)
    return {
        "environment": "production" if is_production(lookup) else "development",
        "on_railway": on_railway(lookup),
        "port": _get(lookup, "PORT") or "8501 (default)",
        "paths": paths.as_dict(),
        "glm_api_key_present": bool(_get(lookup, "GLM_API_KEY")),
        "glm_model": _get(lookup, "GLM_MODEL") or None,
        "glm_finalizer_model": _get(lookup, "GLM_FINALIZER_MODEL") or None,
        "glm_base_url": _get(lookup, "GLM_BASE_URL") or "default",
        "search_backend": _get(lookup, "SEARCH_BACKEND") or "glm",
        "database_url_present": bool(_get(lookup, "DATABASE_URL") or _get(lookup, "SUPABASE_DB_URL")),
        "tripy_access_token_present": bool(_get(lookup, "TRIPY_ACCESS_TOKEN")),
        "max_active_runs": max_active_runs(lookup),
        "checks": [c.as_dict() for c in checks],
    }

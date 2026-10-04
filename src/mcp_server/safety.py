"""Guards shared by every MCP tool: id validation, path containment, secret redaction, response size caps.

* Ids are checked against what exists on disk (run folders, vehicle folders, cached documents) and every path built
  from an input must resolve inside the runs / cache folders: `..`, absolute paths, separators and symlinks that leave
  the tree are rejected.
* Every response is redacted (the configured secrets, any environment value whose name says KEY / TOKEN / SECRET /
  PASSWORD / DSN, URLs with credentials, Authorization / Bearer values, secret-named JSON keys) and capped at
  MAX_RESPONSE_CHARS characters of JSON.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from ..app_config import SECRET_VARS, redact

MAX_RESPONSE_CHARS = 60_000
MAX_STRING_CHARS = 8_000          # one string value inside a structured response (events, tables, payloads)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}")
_DOC_ID = re.compile(r"d_[0-9a-f]{16}")


class ToolInputError(ValueError):
    """A rejected input (unknown id, traversal attempt, bad parameter). The message never echoes secrets."""


# --- ids and paths ----------------------------------------------------------------------------------------------------

def _shown(value: Any) -> str:
    return redact(str(value))[:80]


def _check_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "")
    if not _ID.fullmatch(text) or ".." in text:
        raise ToolInputError(f"invalid {what}: {_shown(text)!r}")
    return text


def inside(path: Path, root: Path) -> bool:
    """`path` (symlinks resolved) is `root` or below it (root resolved too)."""
    try:
        resolved, base = path.resolve(), root.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == base or base in resolved.parents


def child_dir(root: Path, name: Any, what: str) -> Path:
    """An existing direct sub-folder `root/name` that stays inside `root` (no traversal, no symlink out of tree)."""
    name = _check_id(name, what)
    path = root / name
    if not path.is_dir() or not inside(path, root) or path.resolve().parent != root.resolve():
        raise ToolInputError(f"unknown {what}: {_shown(name)!r}")
    return path


def run_dir(runs_dir: Path, run_id: Any) -> Path:
    if str(run_id or "").startswith(("_", ".")):
        raise ToolInputError(f"unknown run_id: {_shown(run_id)!r}")
    return child_dir(runs_dir, run_id, "run_id")


def vehicle_dir(runs_dir: Path, run_id: Any, record_id: Any) -> Path:
    run = run_dir(runs_dir, run_id)
    path = child_dir(run, record_id, "record_id")
    if not any((path / name).is_file() for name in ("events.jsonl", "input.json", "result.json")):
        raise ToolInputError(f"unknown record_id: {_shown(record_id)!r}")
    return path


def is_doc_id(value: Any) -> bool:
    return bool(_DOC_ID.fullmatch(str(value or "")))


def doc_id(value: Any) -> str:
    text = str(value if value is not None else "")
    if not _DOC_ID.fullmatch(text):
        raise ToolInputError(f"invalid doc_id: {_shown(text)!r} (expected d_ followed by 16 hex digits)")
    return text


def contained_document_dir(folder: Path, roots: Iterable[Path]) -> bool:
    return (folder / "meta.json").is_file() and any(inside(folder, root) for root in roots)


def clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


# --- redaction --------------------------------------------------------------------------------------------------------

_SENSITIVE_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|CREDENTIAL", re.I)
_URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.\-]*://([^:/\s@]+):([^@\s/]+)@", re.I)
_SECRET_KEY = re.compile(r"(?i)^(?:.*[-_])?(?:api[-_]?key|apikey|secret|password|passwd|token|access[-_]?token|"
                         r"authorization|proxy[-_]authorization|cookie|set[-_]cookie|dsn|credentials?)$")
_PLAIN_VALUE = re.compile(r"(?i)[0-9.\-]+|true|false|yes|no|none|null|on|off")


def secret_values(env: dict[str, str] | None = None) -> list[str]:
    """Every environment value that must never leave the server, longest first."""
    env = dict(os.environ) if env is None else env
    out: set[str] = set()
    for name, value in env.items():
        value = (value or "").strip()
        if not value:
            continue
        if name in SECRET_VARS or _SENSITIVE_NAME.search(name):
            # a numeric / boolean setting (GLM_MAX_TOKENS=4096) is configuration, not a secret
            if len(value) >= 6 and not _PLAIN_VALUE.fullmatch(value):
                out.add(value)
        for match in _URL_CREDENTIALS.finditer(value):
            out.add(value)
            if len(match.group(2)) >= 3:
                out.add(match.group(2))
    return sorted(out, key=len, reverse=True)


class Redactor:
    """Scrubs secrets out of any JSON-like value (a snapshot of the environment at construction)."""

    def __init__(self, env: dict[str, str] | None = None):
        self.values = secret_values(env)
        snapshot = dict(os.environ) if env is None else dict(env)
        self._lookup: Callable[[str], str | None] = snapshot.get

    def text(self, value: str) -> str:
        for secret in self.values:
            if secret in value:
                value = value.replace(secret, "[redacted]")
        return redact(value, self._lookup)

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): ("[redacted]" if isinstance(v, str) and v and _SECRET_KEY.match(str(k))
                                        else self.obj(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.obj(v) for v in value]
        return value


# --- size -------------------------------------------------------------------------------------------------------------

def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def clip(value: Any, limit: int = MAX_STRING_CHARS) -> Any:
    """Long strings inside a structure are cut (with a marker saying how much was dropped)."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…[clipped {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clip(v, limit) for v in value]
    return value


def page(items: list, offset: Any, limit: Any, build: Callable[[list], dict], *, max_limit: int,
         default_limit: int, budget: int = MAX_RESPONSE_CHARS - 2_000) -> dict:
    """`build(items[offset:offset+limit])` with a `paging` block; the page shrinks until its JSON fits `budget`.

    paging = {offset, limit, returned, total, remaining, next_offset (None at the end)}."""
    total = len(items)
    offset = clamp(offset, 0, max(total, 0), 0)
    limit = clamp(limit, 1, max_limit, default_limit)
    chunk = [clip(i) for i in items[offset:offset + limit]]
    while True:
        payload = build(chunk)
        returned = len(chunk)
        remaining = max(0, total - offset - returned)
        payload["paging"] = {"offset": offset, "limit": limit, "returned": returned, "total": total,
                             "remaining": remaining, "next_offset": offset + returned if remaining else None}
        size = len(dumps(payload))
        if size <= budget or returned <= 1:
            return payload
        chunk = chunk[:max(1, min(returned - 1, returned * budget // size))]


def finish(payload: Any, redactor: Redactor, limit: int = MAX_RESPONSE_CHARS) -> str:
    """The redacted JSON text of a tool response, never longer than `limit` characters."""
    text = dumps(redactor.obj(payload))
    if len(text) <= limit:
        return text
    shown = max(0, limit - 400)
    while True:          # the partial JSON is itself escaped inside the envelope: shrink until the envelope fits
        out = dumps({"truncated": True, "total_chars": len(text), "shown_chars": shown,
                     "hint": "response exceeded the size cap; narrow it with offset / limit / field filters",
                     "partial_json": text[:shown]})
        if len(out) <= limit or shown == 0:
            return out
        shown = max(0, shown - (len(out) - limit) - 16)

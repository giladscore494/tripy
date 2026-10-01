"""Research tool registry and dispatcher.

Dispatch performs technical validation only (parseable JSON arguments, required
keys present, integer coercion). It never filters sources or values.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from ..schemas import TOOL_SPECS
from ..storage.cache import DocumentCache
from .evidence import EvidenceStore


@dataclass
class ToolConfig:
    search_backend: str = "glm"          # "glm" (GLM web_search API) or "duckduckgo" (keyless HTML)
    max_response_bytes: int = 15 * 1024 * 1024
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 40.0
    render_timeout_s: float = 45.0
    # Conservative output caps. They limit what a tool returns to the model; the full
    # document always stays in the document cache and can be paged / searched.
    preview_chars: int = 1200            # fetch_url / fetch_pdf / render_page text preview
    max_text_chars: int = 4000           # upper bound for extract_html / get_cached_document pages
    max_links: int = 40                  # links returned by extract_html / render_page
    max_table_rows: int = 40             # rows per table returned by extract_tables
    max_tables: int = 8                  # tables returned by extract_tables per call


@dataclass
class ToolContext:
    cache: DocumentCache
    evidence: EvidenceStore
    config: ToolConfig = field(default_factory=ToolConfig)
    glm: Any = None                      # GLMClient, used by the "glm" search backend
    vehicle: dict = field(default_factory=dict)
    session: requests.Session = field(default_factory=requests.Session)
    log: Callable[..., Any] | None = None
    documents_opened: list[str] = field(default_factory=list)
    counters: Counter = field(default_factory=Counter)

    def note_document(self, document_id: str, cache_hit: bool | None = None) -> None:
        """Track documents touched by this run; cache_hit counts only download requests."""
        if document_id not in self.documents_opened:
            self.documents_opened.append(document_id)
        if cache_hit is not None:
            self.counters["cache_hits" if cache_hit else "cache_misses"] += 1

    def emit(self, kind: str, **data: Any) -> None:
        if self.log:
            self.log(kind, **data)


def _registry() -> dict[str, Callable[..., dict]]:
    from . import cache, evidence, extract, fetch, render, search

    return {
        "search_web": search.search_web,
        "search_official_domains": search.search_official_domains,
        "fetch_url": fetch.fetch_url,
        "fetch_pdf": fetch.fetch_pdf,
        "render_page": render.render_page,
        "extract_html": extract.extract_html,
        "extract_tables": extract.extract_tables,
        "find_in_document": extract.find_in_document,
        "get_structured_data": extract.get_structured_data,
        "get_cached_document": cache.get_cached_document,
        "store_evidence": evidence.store_evidence,
        "report_field_status": evidence.report_field_status,
    }


_SPEC_BY_NAME = {spec["function"]["name"]: spec["function"]["parameters"] for spec in TOOL_SPECS}


def tool_specs() -> list[dict]:
    return TOOL_SPECS


def _coerce_args(name: str, raw: Any) -> dict:
    if raw is None or raw == "":
        args: Any = {}
    elif isinstance(raw, dict):
        args = raw
    else:
        args = json.loads(raw)
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    schema = _SPEC_BY_NAME[name]
    missing = [key for key in schema.get("required", []) if args.get(key) in (None, "")]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")
    props = schema.get("properties", {})
    clean = {}
    for key, value in args.items():
        if key not in props:
            continue  # unknown keys are ignored, not fatal
        expected = props[key].get("type")
        if expected == "integer" and value is not None:
            value = int(value)
        elif expected == "array" and isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        clean[key] = value
    return clean


def dispatch(ctx: ToolContext, name: str, raw_args: Any) -> dict:
    """Run one tool call. Always returns a JSON-serializable dict."""
    registry = _registry()
    if name not in registry:
        return {"error": "unknown_tool", "message": f"No tool named {name!r}", "available": list(registry)}
    try:
        args = _coerce_args(name, raw_args)
    except (ValueError, TypeError) as exc:
        return {"error": "invalid_arguments", "message": str(exc)}
    started = time.monotonic()
    try:
        result = registry[name](ctx, **args)
    except Exception as exc:  # tools must not crash the agent loop
        result = {"error": type(exc).__name__, "message": str(exc)[:500]}
    ctx.counters[f"tool:{name}"] += 1
    if isinstance(result, dict):
        result.setdefault("_elapsed_ms", int((time.monotonic() - started) * 1000))
    return result

"""Shared shapes: research targets, GLM tool definitions and lenient output parsing.

Nothing in this module judges truth. Target fields are suggestions handed to the
model and a yardstick for observational coverage; parsing exists only so the UI
can render whatever the model returned.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Level 2 targets come from the enrichment field schema (data/enrichment_fields.json, see
# src/fields.py). Keys are suggested names; the model may return other keys and those are
# kept as additional fields. LEVEL2_TARGET_FIELDS is the grouped view of the default schema.
FIELD_STATUSES = ("found", "conflict_resolved", "not_applicable", "unresolved", "conflicting",
                  "foreign_market_only", "variant_not_exact", "weak_provenance")


def _default_groups() -> dict[str, dict[str, str]]:
    from .fields import load_schema

    groups: dict[str, dict[str, str]] = {}
    for spec in load_schema():
        groups.setdefault(spec["group"], {})[spec["name"]] = spec["description"]
    return groups


LEVEL2_TARGET_FIELDS: dict[str, dict[str, str]] = _default_groups()

LEVEL3_TOPICS: dict[str, str] = {
    "known_issues_reliability": "Known issues and reliability by model/year",
    "ownership_costs": "Total ownership costs",
    "maintenance": "Maintenance schedule and costs",
    "insurance": "Insurance cost indications",
    "depreciation": "Depreciation / resale value",
    "recalls": "Recalls",
}


def target_field_names(include_electric: bool = True, propulsion: str | None = None) -> list[str]:
    """Default-schema field names that apply to a vehicle (by `applies_to`, never by field name)."""
    from .fields import applicable_names, resolve_requested_fields

    if propulsion is None and not include_electric:
        propulsion = "conventional"
    return applicable_names(resolve_requested_fields(None, propulsion=propulsion))


# --- Tool definitions handed to GLM (OpenAI-compatible function format) -------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: list[dict] = [
    _fn(
        "search_web",
        "General web search. Returns titles, URLs and snippets with no reliability ranking.",
        {
            "query": {"type": "string", "description": "Search query, any language."},
            "max_results": {"type": "integer", "description": "1-20, default 8."},
            "domain": {"type": "string", "description": "Optional: restrict to one domain."},
        },
        ["query"],
    ),
    _fn(
        "search_official_domains",
        "Same as search_web but biased to manufacturer/importer domains. Convenience only: "
        "it does not make other sources less acceptable. Omit domains to use defaults for "
        "this vehicle's manufacturer.",
        {
            "query": {"type": "string"},
            "domains": {"type": "array", "items": {"type": "string"}, "description": "Optional list of domains."},
        },
        ["query"],
    ),
    _fn(
        "fetch_url",
        "Download a URL (HTML, JSON, text or PDF). Stores status, headers, content-type, final URL "
        "and body in the document cache; returns a document_id plus a short preview. Query the stored "
        "document with find_in_document / extract_tables / get_structured_data / extract_html.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn(
        "fetch_pdf",
        "Download a PDF; stores bytes, extracted text and metadata in the document cache. Returns a "
        "document_id and a short preview; query it with find_in_document / extract_tables.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn(
        "render_page",
        "Render a JS/SPA page in a headless browser; returns a document_id, visible text preview "
        "and links after rendering.",
        {
            "url": {"type": "string"},
            "wait_ms": {"type": "integer", "description": "Extra wait after load, default 2500."},
        },
        ["url"],
    ),
    _fn(
        "extract_html",
        "Extract headings, lists, links and readable text from a stored document. Page with offset.",
        {
            "document_id": {"type": "string"},
            "offset": {"type": "integer", "description": "Text offset to continue from, default 0."},
            "max_chars": {"type": "integer", "description": "Page size; capped by the run configuration."},
        },
        ["document_id"],
    ),
    _fn(
        "extract_tables",
        "Extract tables (and definition-list spec grids) from a stored HTML or PDF document as rows/columns.",
        {
            "document_id": {"type": "string"},
            "max_tables": {"type": "integer", "description": "Tables per call; capped by the run configuration."},
            "start_table": {"type": "integer", "description": "Index of the first table to return (paging)."},
        },
        ["document_id"],
    ),
    _fn(
        "find_in_document",
        "Focused search inside a stored document. scope: 'text' (visible text, default), "
        "'source' (raw HTML/JSON source) or 'structured' (embedded JSON data).",
        {
            "document_id": {"type": "string"},
            "query": {"type": "string"},
            "scope": {"type": "string", "description": "text | source | structured"},
            "context_chars": {"type": "integer", "description": "Default 300."},
        },
        ["document_id", "query"],
    ),
    _fn(
        "get_structured_data",
        "Extract JSON-LD, Next.js/Nuxt data, embedded application/json, window state objects and "
        "meta tags from a stored HTML document.",
        {"document_id": {"type": "string"}},
        ["document_id"],
    ),
    _fn(
        "get_cached_document",
        "Return an already-downloaded document by document_id or URL without fetching again.",
        {
            "key": {"type": "string", "description": "document_id or URL."},
            "offset": {"type": "integer"},
            "max_chars": {"type": "integer"},
        },
        ["key"],
    ),
    _fn(
        "store_evidence",
        "Record what you consider evidence for a value: field, exact value, source URL / document_id, a short "
        "verbatim quote, the market and trim the source describes, and your note. This is a log, not a verifier. "
        "Returns an evidence_id (e1, e2, ...) to cite in evidence_ids; document_ids are not evidence ids.",
        {
            "field": {"type": "string"},
            "value": {"type": "string", "description": "Value as found (numbers may be given as numbers)."},
            "unit": {"type": "string"},
            "source_url": {"type": "string"},
            "document_id": {"type": "string"},
            "quote": {"type": "string", "description": "Verbatim fragment from the source."},
            "market": {"type": "string",
                       "description": "Market the source describes, e.g. IL, MY, UK, EU, DK, CN, global, unknown."},
            "variant": {"type": "string", "description": "Trim/variant the source describes, as written there."},
            "variant_match": {"type": "string",
                              "description": "Your judgement: exact | different | unclear (does the source "
                                             "describe this exact variant?)"},
            "note": {"type": "string", "description": "E.g. why a different market or trim is still relevant."},
        },
        ["field", "value"],
    ),
]

TOOL_SPECS.append(_fn(
    "report_field_status",
    "Declare the status of one requested field when it is not simply found: not_applicable (does not exist for "
    "this vehicle), unresolved, conflicting, foreign_market_only, variant_not_exact or weak_provenance; "
    "found; or conflict_resolved (you established which of several different stored values applies to the "
    "exact target variant). Used to decide which fields get a focused follow-up; it never changes stored "
    "evidence.",
    {
        "field": {"type": "string"},
        "status": {"type": "string", "description": " | ".join(FIELD_STATUSES)},
        "note": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"},
                         "description": "Evidence records (e1, e2, ...) behind this status; required for "
                                        "conflict_resolved."},
    },
    ["field", "status"],
))

TOOL_NAMES = [spec["function"]["name"] for spec in TOOL_SPECS]


# --- Lenient output parsing ---------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_model_output(text: str | None) -> tuple[Any | None, str]:
    """Best-effort JSON extraction. Returns (parsed, note). Never raises, never rejects."""
    if not text or not text.strip():
        return None, "empty"
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate), "ok"
        except (ValueError, TypeError):
            continue
    return None, "not_json"


def iter_fields(output: Any) -> list[tuple[str, dict]]:
    """Normalize the model's `fields` into (name, entry) pairs for display.

    Accepts {name: {value, unit, ...}}, {name: bare_value} or a list of entries
    carrying a `field`/`name` key. Anything else yields nothing (it is still
    shown in the raw JSON view).
    """
    if not isinstance(output, dict):
        return []
    fields = output.get("fields")
    pairs: list[tuple[str, dict]] = []
    if isinstance(fields, dict):
        for name, entry in fields.items():
            pairs.append((str(name), entry if isinstance(entry, dict) else {"value": entry}))
    elif isinstance(fields, list):
        for i, entry in enumerate(fields):
            if isinstance(entry, dict):
                name = entry.get("field") or entry.get("name") or f"item_{i}"
                pairs.append((str(name), entry))
    return pairs


def has_value(entry: dict) -> bool:
    if "value" not in entry and "values" in entry:
        value = entry.get("values")
    else:
        value = entry.get("value")
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True

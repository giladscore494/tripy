"""Deterministic batch inspection of ONE cached document for MANY fields (no model call, no network).

The first real Corolla 2024 BUSINESS EDI run spent research turns as a model-driven Ctrl+F over documents that were
already local: find_in_document("Maximum torque"), then "Torque", then "Fuel tank", "Kerb Weight", "Tyres", ...
This module does that whole sequence in one local operation:

    cached document (DocumentCache)
            ↓
    the field dictionary's aliases (data/enrichment_fields.json, compiled once by src/candidate_harvest.py)
            ↓
    one pass per requested field over the document text: label locations -> compact snippets, overlapping
    windows merged, original offsets kept
            +
    the document's harvested candidates for those fields (the same cached harvest every phase reads), with
    their quote, table/row/page and, where the quote occurs verbatim in the text, its offset
            ↓
    {"document_id", "matches": {field: [{snippet, offset, end, matched_alias}]}, "candidates": {...}}

What it never does: create Evidence, decide which variant or market a value belongs to, change a field state, call
current_evaluation(), call a model or fetch anything. The caller decides which fields to ask for (normally the open
fields from current_evaluation()). A snippet or a candidate is a location to read, never a fact.
"""

from __future__ import annotations

from typing import Any, Iterable

from .candidate_harvest import _alias_hits, dictionary_for, harvest_document, normalize_text
from .fields import normalize_field_name

INSPECTION_VERSION = "inspect-v1"
DEFAULT_CONTEXT_CHARS = 120
DEFAULT_MATCHES_PER_FIELD = 2
DEFAULT_CANDIDATES_PER_FIELD = 3
NOTE = ("Deterministic local inspection: snippets and candidates are locations in the cached document, not evidence. "
        "Store a value only with store_evidence (document_id + a short verbatim quote) after judging variant and "
        "market.")


def _window(text: str, start: int, end: int, context: int) -> tuple[int, int]:
    """The label's line and the line after it (a value often sits on the next line of an extracted table), never more
    than `context` characters before the label or after it."""
    line_start = text.rfind("\n", 0, start) + 1
    nxt = text.find("\n", end)
    line_end = len(text) if nxt < 0 else (text.find("\n", nxt + 1) if text.find("\n", nxt + 1) >= 0 else len(text))
    return max(line_start, start - context), min(line_end, end + context)


def _merge(text: str, hits: list[tuple[int, int, str]], context: int) -> list[dict]:
    """Overlapping windows of one field become ONE snippet; the most specific (longest) alias names it."""
    merged: list[dict] = []
    for start, end, alias in sorted(hits):
        a, b = _window(text, start, end, context)
        if merged and a < merged[-1]["end"]:
            last = merged[-1]
            last["end"] = max(last["end"], b)
            last["aliases"].append(alias)
            if len(alias) > len(last["matched_alias"]):
                last["matched_alias"], last["label_offset"] = alias, start
            continue
        merged.append({"offset": a, "end": b, "label_offset": start, "matched_alias": alias, "aliases": [alias]})
    return merged


def _locate(text: str, norm: str, quote: str) -> int | None:
    """Offset of a candidate quote in the document text, when it occurs verbatim (table-row quotes such as
    'label | cell' do not; their table/row/page provenance is kept instead)."""
    quote = (quote or "").strip().strip("…").strip()
    if len(quote) < 4:
        return None
    pos = text.find(quote)
    if pos < 0:
        pos = norm.find(normalize_text(quote))
    return pos if pos >= 0 else None


def _compact_candidate(cand: dict, offset: int | None) -> dict:
    keep = ("value", "unit", "raw_value", "quote", "matched_alias", "extraction_method", "parser_confidence",
            "table_index", "row_index", "page", "variant_hint", "variant_hints", "year_hint", "market_hint",
            "occurrences")
    out = {k: cand.get(k) for k in keep if cand.get(k) not in (None, "", [], {})}
    if offset is not None:
        out["offset"] = offset
    return out


def inspect_document(cache, document_id: str, specs: list[dict], fields: Iterable[str] | None = None, *,
                     context_chars: int = DEFAULT_CONTEXT_CHARS, max_matches_per_field: int = DEFAULT_MATCHES_PER_FIELD,
                     candidates_per_field: int = DEFAULT_CANDIDATES_PER_FIELD, max_chars: int | None = None) -> dict:
    """Inspect one cached document for several fields at once. Pure over the cache: no model, no network, no
    evidence, no field state. `specs` are the run's requested field specs (the dictionary); `fields` the field
    names to look for (default: every applicable spec)."""
    meta = cache.get(str(document_id))
    if not meta:
        return {"document_id": document_id, "error": "document_not_cached",
                "message": f"Unknown document_id {document_id!r}; it must be fetched first."}
    text = cache.read_text(str(document_id)) or ""
    norm = normalize_text(text)
    if len(norm) != len(text):          # normalization is length preserving by construction; never trust it blindly
        norm = text.lower() if len(text.lower()) == len(text) else text
    applicable = [s["name"] for s in specs if s.get("applicable", True)]
    wanted = list(dict.fromkeys(normalize_field_name(f) for f in (fields if fields is not None else applicable)))
    known = {s["name"] for s in specs}
    unknown = [f for f in wanted if f not in known]
    wanted = [f for f in wanted if f in known]
    rules = {r.name: r for r in dictionary_for(specs).rules}
    no_dictionary = [f for f in wanted if f not in rules]
    context = max(30, min(int(context_chars or DEFAULT_CONTEXT_CHARS), 600))
    per_field = max(1, int(max_matches_per_field or DEFAULT_MATCHES_PER_FIELD))
    try:
        harvested, _ = harvest_document(cache, str(document_id), specs)
    except Exception:   # a broken parse never prevents the text inspection
        harvested = []
    by_field: dict[str, list[dict]] = {}
    for cand in harvested:
        by_field.setdefault(normalize_field_name(cand.get("field")), []).append(cand)

    matches: dict[str, list[dict]] = {}
    candidates: dict[str, list[dict]] = {}
    for name in wanted:
        cands = sorted(by_field.get(name) or [], key=lambda c: -(c.get("parser_confidence") or 0))
        located = [(c, _locate(text, norm, str(c.get("quote") or ""))) for c in cands[:max(0, candidates_per_field)]]
        if located:
            candidates[name] = [_compact_candidate(c, off) for c, off in located]
        rule = rules.get(name)
        if rule is None:
            continue
        windows = _merge(text, [(s, e, alias) for s, e, alias, _ in _alias_hits(rule, norm)], context)
        if not windows:
            continue
        anchors = [off for _, off in located if off is not None]
        # windows that hold a parsed candidate first, then the more specific label, then document order
        windows.sort(key=lambda w: (not any(w["offset"] <= a < w["end"] for a in anchors),
                                    -len(w["matched_alias"]), w["offset"]))
        matches[name] = [{"snippet": text[w["offset"]:w["end"]], "offset": w["offset"], "end": w["end"],
                          "label_offset": w["label_offset"], "matched_alias": w["matched_alias"]}
                         for w in windows[:per_field]]
    result: dict[str, Any] = {
        "document_id": str(document_id), "url": meta.get("final_url") or meta.get("url"),
        "title": meta.get("title") or "", "status": meta.get("status"), "doc_type": meta.get("doc_type"),
        "text_chars": len(text), "fields_requested": len(wanted),
        "fields_with_matches": sorted(set(matches) | set(candidates)),
        "fields_without_matches": [f for f in wanted if f not in matches and f not in candidates],
        "matches": matches, "candidates": candidates, "inspection_version": INSPECTION_VERSION,
        "model_calls": 0, "note": NOTE,
    }
    if unknown:
        result["unknown_fields"] = unknown
    if no_dictionary:
        result["fields_without_dictionary"] = no_dictionary
    if max_chars:
        _fit(result, int(max_chars))
    return result


def _fit(result: dict, max_chars: int) -> None:
    """Keep a model-facing result under max_chars: drop the lowest-ranked snippet (then candidate) of the field with
    the most, never a field's last location. Records which fields were trimmed."""
    import json

    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, default=str))

    trimmed: set[str] = set()
    for key in ("matches", "candidates"):
        bucket = result[key]
        while size() > max_chars and any(len(v) > 1 for v in bucket.values()):
            name = max(bucket, key=lambda n: (len(bucket[n]), n))
            bucket[name] = bucket[name][:-1]
            trimmed.add(name)
    while size() > max_chars and any(len(m.get("snippet") or "") > 120 for v in result["matches"].values() for m in v):
        for items in result["matches"].values():
            for m in items:
                if len(m.get("snippet") or "") > 120:
                    m["snippet"] = m["snippet"][:120] + "…"
        result["snippets_shortened"] = True
    if size() > max_chars and len(result["fields_without_matches"]) > 3:
        result["fields_without_matches_count"] = len(result.pop("fields_without_matches"))
    omitted: list[str] = []
    while size() > max_chars and (result["matches"] or result["candidates"]):
        # still too large (many fields requested): leave whole fields out, last ones first, and say which (the
        # list and hint are part of the size being checked)
        name = sorted(set(result["matches"]) | set(result["candidates"]))[-1]
        result["matches"].pop(name, None)
        result["candidates"].pop(name, None)
        omitted.append(name)
        result["fields_omitted_for_size"] = sorted(omitted)
        result["hint"] = "Result capped: inspect the omitted fields with another call that names them in `fields`."
    if trimmed:
        result["trimmed_fields"] = sorted(trimmed)

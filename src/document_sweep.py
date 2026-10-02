"""Model Document Sweep: "use what we already downloaded" before any targeted web recovery.

    deterministic candidates (src/candidate_harvest.py) + current field states + stored evidence
            ↓
    ONE compact packet for ALL applicable requested fields together
            ↓
    the model reviews candidates (market, variant, conflicts), inspects cached documents for what the
    parser missed, and promotes valid facts with store_evidence. It cannot search or fetch: only the
    cached-document tools below are offered, and any other call is refused without being executed.
            ↓
    current_evaluation() decides what is still unresolved (the sweep never sets a state itself)

Pure helpers only (packet, tool filter, after-sweep accounting); the loop lives in src/agent.py.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from .candidate_harvest import candidates_from_events
from .field_recovery import RETRY_STATES, material_key, vehicle_identity
from .fields import normalize_field_name, semantic_notes
from .storage import trace

DOCUMENT_SWEEP_TOOLS = ("find_in_document", "extract_tables", "extract_html", "get_structured_data",
                        "get_cached_document", "store_evidence", "report_field_status")
EXTERNAL_TOOLS = trace.SEARCH_TOOLS + trace.FETCH_TOOLS
MAX_SWEEP_TURNS = 2        # absolute maximum; turn 2 only after a turn-1 cached-document inspection with content
INSPECTION_TOOLS = ("find_in_document", "extract_tables", "extract_html", "get_structured_data",
                    "get_cached_document")
CANDIDATE_KEYS = ("value", "unit", "raw_value", "raw_unit", "document_id", "market_hint", "variant_hint",
                  "variant_hints", "year_hint", "year_hint_differs", "trim_mentioned", "official_domain",
                  "parser_confidence", "extraction_method", "position", "ambiguity", "availability",
                  "price_type_hint", "warranty_type", "converted_from", "range", "page", "occurrences")


def inspection_has_content(name: str, result: dict | None) -> bool:
    """Did a cached-document inspection return something the model still has to read?
    Errors, zero hits, no tables, empty structured data or empty text do not justify a follow-up turn."""
    if name not in INSPECTION_TOOLS or not isinstance(result, dict) or result.get("error"):
        return False
    if name == "find_in_document":
        return bool(result.get("hit_count") or result.get("hits"))
    if name == "extract_tables":
        return bool(result.get("tables_total") or result.get("tables"))
    if name == "get_structured_data":
        return any((result.get("found") or {}).values()) or bool(result.get("data") or result.get("data_preview"))
    if name == "get_cached_document" and not result.get("found", True):
        return False
    return bool((result.get("text") or "").strip())


def sweep_tool_specs(all_specs: list[dict]) -> list[dict]:
    return [s for s in all_specs if s["function"]["name"] in DOCUMENT_SWEEP_TOOLS]


def _domain(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    return (host[4:] if host.startswith("www.") else host) or None


def compact_candidate(cand: dict, quote_chars: int = 200) -> dict:
    out = {k: cand.get(k) for k in CANDIDATE_KEYS if cand.get(k) not in (None, "", [], {})}
    if cand.get("quote"):
        out["quote"] = str(cand["quote"])[:quote_chars]
    if cand.get("source_url"):
        out["source"] = _domain(cand["source_url"])
    return out


def compact_evidence(item: dict) -> dict:
    out = {k: item.get(k) for k in ("evidence_id", "field", "value", "unit", "market", "variant", "variant_match",
                                    "binding_level", "source_authority", "document_id") if item.get(k) not in (None, "")}
    if item.get("source_url"):
        out["source"] = _domain(item["source_url"])
    return out


def sweep_packet(*, payload: dict, specs: list[dict], evaluation: list[dict], matrix: dict, events: list[dict],
                 doc_metas: list[dict], target_market: str, max_turns: int, per_field: int = 6,
                 max_chars: int = 40000) -> dict:
    """The compact context of the sweep: every applicable field at once. No document is sent whole."""
    applicable = [s for s in specs if s.get("applicable", True)]
    states = {e["field"]: e["state"] for e in evaluation}
    review = [s["name"] for s in applicable if states.get(s["name"]) in RETRY_STATES]
    candidates: dict[str, list[dict]] = {}
    for spec in applicable:
        cands = matrix["fields"].get(spec["name"]) or []
        limit = per_field if spec["name"] in review else min(3, per_field)
        if cands:
            candidates[spec["name"]] = [compact_candidate(c) for c in cands[:limit]]
    packet: dict[str, Any] = {
        "vehicle_identity": vehicle_identity(payload, target_market),
        "turn_budget": max_turns,
        "turn_rule": ("Promote valid candidates in turn 1 and finish. A second turn (if turn_budget allows) is given "
                      "only when turn 1 inspected cached documents and got content back."),
        "requested_fields": {s["name"]: s.get("description") or s["name"] for s in applicable},
        "field_semantics": semantic_notes(applicable),
        "current_field_states": {s["name"]: states.get(s["name"]) for s in applicable},
        "fields_to_review": review,
        "deterministic_candidates": candidates,
        "fields_without_candidates": [n for n in review if not candidates.get(n)],
        "stored_evidence": [compact_evidence(e) for e in trace.evidence_items(events)],
        "cached_documents": [{k: m.get(k) for k in ("document_id", "url", "final_url", "title", "doc_type", "pages",
                                                    "text_chars") if m.get(k) not in (None, "")}
                             for m in doc_metas],
        "candidate_summary": {k: matrix.get(k) for k in ("candidate_count", "documents", "applicable_fields",
                                                         "candidate_field_coverage_pct")},
    }
    # Fit the size budget: trim candidates of already-ok fields first, then the longest lists.
    while len(json.dumps(packet, ensure_ascii=False, default=str)) > max_chars:
        lists = sorted(candidates.items(), key=lambda kv: (kv[0] in review, -len(kv[1])))
        trimmed = False
        for name, items in lists:
            if len(items) > 1:
                candidates[name] = items[:-1]
                trimmed = True
                break
        if not trimmed:
            break
    return packet


def promoted_or_missed(sweep_evidence: list[dict], events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split evidence stored by the sweep into (promoted deterministic candidates, facts the deterministic
    harvest missed: no candidate for that field and value from that document)."""
    by_doc: dict[tuple[str, str], set[str]] = {}
    for cand in candidates_from_events(events):
        key = (normalize_field_name(cand.get("field")), str(cand.get("document_id") or cand.get("source_url")))
        by_doc.setdefault(key, set()).add(material_key(cand.get("value")))
    url_to_doc = {}
    for cand in candidates_from_events(events):
        if cand.get("source_url") and cand.get("document_id"):
            url_to_doc[cand["source_url"]] = cand["document_id"]
    promoted, missed = [], []
    for item in sweep_evidence:
        doc = item.get("document_id") or url_to_doc.get(item.get("source_url"))
        key = (normalize_field_name(item.get("field")), str(doc or item.get("source_url")))
        values = by_doc.get(key)
        if values is not None and material_key(item.get("value")) in values:
            promoted.append(item)
        else:
            missed.append(item)
    return promoted, missed


def sweep_summary(*, before: list[dict], after: list[dict], sweep_evidence: list[dict], promoted: list[dict],
                  missed: list[dict], presented: int, model_calls: int, turns: int, blocked: int,
                  external_calls: int, reply: Any, skipped: str | None = None) -> dict:
    eligible_before = {e["field"] for e in before if e["retry_eligible"]}
    eligible_after = {e["field"] for e in after if e["retry_eligible"]}
    resolved = sorted(eligible_before - eligible_after)
    return {
        "skipped": skipped,
        "model_calls": model_calls,
        "turns": turns,
        "fields_unresolved_before": len(eligible_before),
        "fields_unresolved_after": len(eligible_after),
        "fields_resolved": resolved,
        "unique_fields_resolved": len(resolved),
        "evidence_stored": len(sweep_evidence),
        "fields_promoted_to_evidence": sorted({normalize_field_name(e.get("field")) for e in sweep_evidence}),
        "evidence_promoted_from_candidates": len(promoted),
        "deterministic_misses_found": len(missed),
        "candidates_presented": presented,
        # share of presented candidates the model promoted to evidence; a review rate, never accuracy
        "candidate_precision_reviewed": round(100 * len(promoted) / presented, 1) if presented else None,
        "tool_calls_blocked": blocked,
        "external_calls": external_calls,
        "reply": reply,
    }

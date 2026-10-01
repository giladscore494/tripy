"""Schema-driven targeted field recovery.

    requested enrichment fields  (src/fields.py, from data/enrichment_fields.json or the run's request)
            ↓
    primary research
            ↓
    evaluate every requested field  ->  usable / not_applicable : keep, no retry
                                    ->  unresolved | missing | conflicting | foreign_market_only |
                                        variant_not_exact | weak_provenance : enqueue
                                        (as recorded by the model; see evaluate_field)
            ↓
    field retry #1, optional #2 ... (FIELD_RECOVERY_MAX_ATTEMPTS, or the field's own `recovery_attempts`)
            ↓
    finalizer

Nothing here knows a field name. The evaluation reads only what the model itself
recorded during research: stored evidence (value, market, variant, variant_match,
source, quote), its `report_field_status` declarations and its own primary JSON
answer if it gave one. It is an operational trigger for spending another focused
attempt; it never removes, changes or ranks a value, and the finalizer still sees
everything.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .fields import normalize_field_name
from .schemas import iter_fields, parse_model_output
from .storage import trace

RETRY_STATES = ("unresolved", "missing", "conflicting", "foreign_market_only", "variant_not_exact",
                "weak_provenance")
NO_RETRY_STATES = ("ok", "not_applicable")
UNKNOWN_MARKETS = {"", "unknown", "n/a", "na", "none", "unclear", "?"}
MARKET_ALIASES = {"il": {"il", "isr", "israel", "ישראל", "israeli"}}
DEFAULT_TARGET_MARKET = "IL"


def _market_key(value: Any) -> str:
    return str(value or "").strip().lower()


def is_target_market(market: Any, target: str) -> bool:
    key, target_key = _market_key(market), _market_key(target)
    return key in MARKET_ALIASES.get(target_key, {target_key})


def _value_key(value: Any) -> str:
    """Loose comparison key so '229,990' and 229990 are not counted as different values."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return re.sub(r"[\s,]+", "", text.lower())


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return not (isinstance(value, (list, dict)) and not value)


def declarations(events: Iterable[dict]) -> dict[str, dict]:
    """Latest field-status declaration per field (report_field_status tool or a field-retry reply)."""
    out: dict[str, dict] = {}
    for event in events:
        if event.get("kind") == "field_status" and event.get("field"):
            out[normalize_field_name(event["field"])] = {k: event.get(k) for k in ("status", "note", "source")}
    return out


def primary_output(events: Iterable[dict]) -> Any:
    """The research model's own final JSON (a research turn with no tool calls), if it gave one."""
    found = None
    for event in events:
        if (event.get("kind") == "model_response" and trace.phase_group(event.get("phase")) == "research"
                and not event.get("tool_calls") and (event.get("content") or "").strip()):
            parsed, _ = parse_model_output(event["content"])
            if parsed is not None:
                found = parsed
    return found


def evaluate_field(spec: dict, evidence: list[dict], declared: dict | None, output_entry: dict | None,
                   target_market: str) -> dict:
    """Did primary research obtain a usable candidate for this requested field?

    Operational, from the model's own research state only (never a truth check):

    * not_applicable  - not applicable by the schema's `applies_to`, or the model said so;
    * ok              - a candidate value backed by a stored evidence record, and the model
                        did not itself mark it unresolved / conflicting / wrong market or trim;
    * unresolved      - the model said unresolved (report_field_status or its own answer);
    * missing         - no candidate value and no evidence record;
    * weak_provenance - a value only in the model's answer, with no evidence record behind it
                        (or the model itself reported weak provenance);
    * conflicting     - the model reported an unresolved conflict;
    * foreign_market_only - every candidate is explicitly marked as another market and the model
                        has not declared the field found for the target;
    * variant_not_exact   - every candidate is explicitly marked as another trim/variant
                        (variant_match=different) and the model has not declared it found.

    Several stored values for one field are recorded as info (`multiple_values`), not a trigger:
    the model decides how to handle them.
    """
    name = spec["name"]
    info: list[str] = []
    declared_status = (declared or {}).get("status")
    out_provenance = str((output_entry or {}).get("provenance") or "").lower()
    out_value = (output_entry or {}).get("value", (output_entry or {}).get("values")) if output_entry else None
    with_value = [e for e in evidence if _has_value(e.get("value"))]
    target_items = [e for e in with_value if is_target_market(e.get("market"), target_market)]
    if len({_value_key(e.get("value")) for e in (target_items or with_value)}) > 1:
        info.append("multiple_values")

    if (not spec.get("applicable", True) or declared_status == "not_applicable"
            or out_provenance == "not_applicable"):
        state = "not_applicable"
    elif declared_status in RETRY_STATES:
        state = declared_status                       # the model's own report wins
    elif out_provenance == "unresolved" and declared_status != "found":
        state = "unresolved"
    elif not with_value:
        state = "weak_provenance" if _has_value(out_value) else "missing"
    elif declared_status == "found":
        state = "ok"                                  # the model resolved applicability itself
    elif not target_items and all(_market_key(e.get("market")) not in UNKNOWN_MARKETS for e in with_value):
        state = "foreign_market_only"
    elif all(str(e.get("variant_match") or "").lower() == "different" for e in with_value):
        state = "variant_not_exact"
    else:
        state = "ok"
    return {
        "field": name,
        "state": state,
        "info": info,
        "retry_eligible": state in RETRY_STATES,
        "evidence_ids": [e.get("evidence_id") for e in evidence],
        "values": [e.get("value") for e in with_value][:10],
        "markets": sorted({str(e.get("market")) for e in with_value if e.get("market")}),
        "declared": declared,
        "primary_output": output_entry,
    }


def evaluate_fields(specs: list[dict], events: list[dict], target_market: str = DEFAULT_TARGET_MARKET) -> list[dict]:
    evidence_by_field: dict[str, list[dict]] = {}
    for item in trace.evidence_items(events):
        evidence_by_field.setdefault(normalize_field_name(item.get("field")), []).append(item)
    declared = declarations(events)
    output = {normalize_field_name(name): entry for name, entry in iter_fields(primary_output(events))}
    return [evaluate_field(spec, evidence_by_field.get(spec["name"], []), declared.get(spec["name"]),
                           output.get(spec["name"]), target_market) for spec in specs]


def max_attempts_for(spec: dict, default: int) -> int:
    value = spec.get("recovery_attempts")
    try:
        return max(0, int(value)) if value is not None else max(0, int(default))
    except (TypeError, ValueError):
        return max(0, int(default))


def retry_queue(evaluation: list[dict], specs: list[dict], default_attempts: int) -> list[dict]:
    """Fields to retry, in requested order: eligible state and a non-zero attempt budget."""
    by_name = {s["name"]: s for s in specs}
    return [{"field": e["field"], "state": e["state"], "max_attempts": max_attempts_for(by_name[e["field"]],
                                                                                         default_attempts)}
            for e in evaluation if e["retry_eligible"] and max_attempts_for(by_name[e["field"]], default_attempts)]


def _tokens(*texts: Any) -> set[str]:
    words: set[str] = set()
    for text in texts:
        for word in re.split(r"[^\w]+", str(text or "").lower()):
            if len(word) >= 3 and not word.isdigit():
                words.add(word)
    return words - {"the", "and", "with", "for", "per", "max", "time"}


def related_searches(events: list[dict], spec: dict, limit: int = 15) -> list[str]:
    """Earlier search queries that share a word with the field name/description (generic token overlap)."""
    keys = _tokens(spec["name"].replace("_", " "), spec.get("description"))
    queries: list[str] = []
    for pair in trace.tool_pairs(events):
        if pair["name"] in trace.SEARCH_TOOLS:
            query = trace.parse_args(pair["arguments"]).get("query")
            if query and query not in queries and (_tokens(query) & keys):
                queries.append(query)
    return queries[-limit:]


def related_documents(events: list[dict], field_evidence: list[dict], doc_metas: list[dict],
                      limit: int = 15) -> list[dict]:
    """Documents behind this field's evidence first, then the other documents the run touched (metadata only)."""
    preferred = [e.get("document_id") for e in field_evidence if e.get("document_id")]
    urls = {e.get("source_url") for e in field_evidence if e.get("source_url")}
    ranked = sorted(doc_metas, key=lambda m: (0 if m.get("document_id") in preferred
                                              or (m.get("final_url") or m.get("url")) in urls else 1))
    keys = ("document_id", "url", "final_url", "title", "content_type", "doc_type", "pages", "text_chars", "status")
    return [{k: m.get(k) for k in keys if m.get(k) not in (None, "")} for m in ranked[:limit]]


def vehicle_identity(payload: dict, target_market: str) -> dict:
    identity = payload.get("identity") or {}
    engine = payload.get("engine_drivetrain") or {}
    out = {
        "manufacturer": identity.get("manufacturer"),
        "model": identity.get("commercial_name"),
        "year": identity.get("year"),
        "government_trim": identity.get("trim"),
        "model_code": identity.get("model_code"),
        "government_record_id": identity.get("government_record_id"),
        "powertrain": engine.get("propulsion_normalized") or engine.get("propulsion_technology"),
        "power_hp": engine.get("power_hp"),
        "drivetrain": engine.get("drivetrain_normalized") or engine.get("drive"),
        "market": "Israel" if is_target_market(target_market, DEFAULT_TARGET_MARKET) else target_market,
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def retry_packet(*, spec: dict, evaluation: dict, events: list[dict], payload: dict, doc_metas: list[dict],
                 attempt: int, max_attempts: int, max_steps: int, target_market: str,
                 previous_attempts: list[dict], operator_notes: dict | None = None) -> dict:
    """The compact context of a focused retry for ONE field. Never the primary conversation."""
    field_evidence = [e for e in trace.evidence_items(events)
                      if normalize_field_name(e.get("field")) == spec["name"]]
    packet = {
        "vehicle_identity": vehicle_identity(payload, target_market),
        "requested_field": {k: v for k, v in spec.items() if k != "applicable"},
        "failure_reason": evaluation["state"],
        "primary_result": {"info": evaluation.get("info"), "declared": evaluation.get("declared"),
                           "primary_output": evaluation.get("primary_output")},
        "existing_candidates": [{k: e.get(k) for k in ("evidence_id", "value", "unit", "market", "variant",
                                                        "variant_match", "source_url", "document_id")
                                 if e.get(k) is not None} for e in field_evidence],
        "existing_evidence": field_evidence,
        "relevant_documents": related_documents(events, field_evidence, doc_metas),
        "previous_queries_for_this_field": related_searches(events, spec),
        "previous_attempts": previous_attempts,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "turn_budget": max_steps,
    }
    if operator_notes:
        packet["operator_variant_notes"] = operator_notes
    return packet


def parse_retry_reply(text: str | None, field: str) -> dict | None:
    parsed, _ = parse_model_output(text)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("field") and normalize_field_name(parsed["field"]) != field:
        return None
    return parsed

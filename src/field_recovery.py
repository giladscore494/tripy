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

from .document_binding import DEFAULT_REQUIREMENT, LEVELS, binding_gaps, level_index
from .excerpts import select_prior_excerpts
from .fields import normalize_field_name, public_spec
from .schemas import iter_fields, parse_model_output
from .storage import trace

RETRY_STATES = ("unresolved", "missing", "conflicting", "foreign_market_only", "variant_not_exact",
                "weak_provenance")
NO_RETRY_STATES = ("ok", "not_applicable")
UNKNOWN_MARKETS = {"", "unknown", "n/a", "na", "none", "unclear", "?"}
# variant_match values that say a value is NOT about the target variant. "different" may come from the model or
# from the server-side binding veto; "unbound" (the source does not even name the model family) only from the
# server (src/document_binding.py).
NON_TARGET_VARIANTS = ("different", "unbound")
# variant_match values that say the server-side binding did NOT reach the field's binding_requirement ("unclear" from
# src/document_binding.py: the source names the model, but not precisely enough). Never target-safe on their own.
INSUFFICIENT_VARIANTS = ("unclear", "unknown")
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


_NUMERIC = re.compile(r"^\s*(-?\d[\d,]*(?:\.\d+)?)\s*([^\d]*)$")
RESOLVED_STATUSES = ("found", "conflict_resolved")


def material_key(value: Any) -> str:
    """Comparison key for 'materially different' values. A number with an optional unit word is compared
    as a number ("1,066 Nm" == 1066 != 1080); anything else as normalized text. Never picks a value."""
    if isinstance(value, bool):
        return f"txt:{str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"num:{float(value)!r}"
    if isinstance(value, str):
        match = _NUMERIC.match(value)
        if match:
            try:
                return f"num:{float(match.group(1).replace(',', ''))!r}"
            except ValueError:
                pass
    return "txt:" + _value_key(value)


def declarations(events: Iterable[dict]) -> dict[str, dict]:
    """Latest field-status declaration per field (report_field_status tool or a field-retry reply)."""
    out: dict[str, dict] = {}
    for event in events:
        if event.get("kind") == "field_status" and event.get("field"):
            out[normalize_field_name(event["field"])] = {
                **{k: event.get(k) for k in ("status", "note", "source")}, "seq": event.get("seq"),
                "evidence_ids": [str(i) for i in event.get("evidence_ids") or [] if i not in (None, "")]}
    return out


def _unknown_market(item: dict) -> bool:
    return _market_key(item.get("market")) in UNKNOWN_MARKETS


def in_target_scope(item: dict, target_market: str) -> bool:
    """A target-market item, or a foreign item the portability policy lets count for the target market (its own
    `market` is kept; see src/market_portability.py)."""
    return is_target_market(item.get("market"), target_market) or item.get("portable_to_target_market") is True


def same_scope_conflict(evidence: list[dict], target_market: str) -> list[dict]:
    """Target-scope candidates (not explicitly another variant) whose values materially differ.

    Returns those candidates when there are at least two different values, else []. A candidate from
    another market (unless portable), or marked variant_match=different, never makes a same-scope conflict
    on its own.
    """
    scope = [e for e in evidence if _has_value(e.get("value")) and in_target_scope(e, target_market)
             and str(e.get("variant_match") or "").lower() not in NON_TARGET_VARIANTS]
    return scope if len({material_key(e.get("value")) for e in scope}) > 1 else []


def primary_output(events: Iterable[dict], with_seq: bool = False) -> Any:
    """The research model's own final JSON (a research turn with no tool calls), if it gave one.
    With `with_seq`, returns (output, seq of that model_response)."""
    found, seq = None, None
    for event in events:
        if (event.get("kind") == "model_response" and trace.phase_group(event.get("phase")) == "research"
                and not event.get("tool_calls") and (event.get("content") or "").strip()):
            parsed, _ = parse_model_output(event["content"])
            if parsed is not None:
                found, seq = parsed, event.get("seq")
    return (found, seq) if with_seq else found


def declaration_is_current(statement_seq: int | None, last_evidence_seq: int | None) -> bool:
    """A model statement about a field (any field_status declaration, or its primary JSON) is authoritative
    only while no evidence for that field was stored AFTER it. Unknown ordering: it stays authoritative."""
    return statement_seq is None or last_evidence_seq is None or statement_seq > last_evidence_seq


def conflict_start_seq(conflict: list[dict], evidence_seq: dict) -> int | None:
    """Seq of the evidence item that first made the in-scope values differ (None if unknown)."""
    seen: set[str] = set()
    for item in sorted(conflict, key=lambda e: evidence_seq.get(e.get("evidence_id"), 0)):
        seen.add(material_key(item.get("value")))
        if len(seen) > 1:
            return evidence_seq.get(item.get("evidence_id"))
    return None


def field_requirement(spec: dict | None, item: dict | None = None) -> str:
    """The binding level a value of this field needs: the schema's `binding_requirement`, else the requirement the
    admission gate recorded on the item, else the server default. Never a model value."""
    for value in ((spec or {}).get("binding_requirement"), (item or {}).get("binding_requirement")):
        if value in LEVELS:
            return value
    return DEFAULT_REQUIREMENT


def binding_satisfies(item: dict, requirement: str | None = None) -> bool:
    """Does the server-side binding of this evidence item reach the field's binding requirement?

    * variant_match different / unbound (veto, or not even the model family)  -> no
    * variant_match unclear / unknown (binding below the requirement)          -> no
    * a server-computed binding_level                                         -> level >= requirement
      (the stricter of the field's requirement and the one recorded at admission: evidence admitted under a weaker
      requirement does not become exact for a field that now needs more)
    * no binding recorded at all (legacy evidence / fields without a variant distinction) -> yes, unless one of
      the negative variant_match values above says otherwise.
    A model's variant claim is stored as model_variant_claim and is never read here."""
    match = str(item.get("variant_match") or "").strip().lower()
    if match in NON_TARGET_VARIANTS or match in INSUFFICIENT_VARIANTS:
        return False
    level = item.get("binding_level")
    if level is None:
        return True
    if level not in LEVELS:
        return False
    needed = max(level_index(requirement or DEFAULT_REQUIREMENT),
                 level_index(item.get("binding_requirement")) if item.get("binding_requirement") in LEVELS else 0)
    return level_index(level) >= needed


def in_server_scope(item: dict, target_market: str, requirement: str | None = None) -> bool:
    """Server-side scope of one evidence item: its server-computed binding reaches the field's binding requirement
    (see binding_satisfies: `unclear` never does) and it is either from the target market or an item the server-side
    portability policy accepted (`portable_to_target_market`, set by the evaluator from src/market_portability.py,
    never by a model): a known foreign market, or a market the source does not establish where the field's schema
    explicitly allows it. An unknown market is never the target market by default. A model declaration never
    changes this."""
    return binding_satisfies(item, requirement or field_requirement(None, item)) and market_in_scope(item, target_market)


def market_in_scope(item: dict, target_market: str) -> bool:
    """The item's server-side market is the target market, or a market the portability verdict lets count for it."""
    return in_target_scope(item, target_market)


def resolution_is_backed(declared: dict | None, evidence: list[dict], conflict: list[dict],
                         evidence_seq: dict | None, target_market: str | None = None,
                         requirement: str | None = None) -> bool:
    """A conflict_resolved declaration closes a conflict only if it cites real evidence for this field:
    evidence_ids non-empty, every id stored for this field, at least one cited item in the server-side target scope
    (citing another variant's or another market's item resolves nothing for the target), and (when ordering is known)
    at least one cited item stored at or after the moment the conflict became active (citing only candidates that
    predate the conflict is not a resolution). Code never decides which value is true."""
    cited = [str(i) for i in (declared or {}).get("evidence_ids") or []]
    field_ids = {str(e.get("evidence_id")) for e in evidence}
    if not cited or not set(cited) <= field_ids:
        return False
    if target_market is not None and not any(in_server_scope(e, target_market, requirement) for e in evidence
                                             if str(e.get("evidence_id")) in cited):
        return False
    seqs = evidence_seq or {}
    start = conflict_start_seq(conflict, seqs)
    if start is None:
        return True
    return any(seqs.get(c) is not None and seqs[c] >= start for c in cited)


def conditional_not_applicable(spec: dict, evidence_by_field: dict[str, list[dict]],
                               target_market: str = DEFAULT_TARGET_MARKET, ignore_own: bool = False) -> str | None:
    """The schema's `not_applicable_when` rules: the field does not exist when target-market evidence of another
    field that binds EXACTLY to the target variant has one of the listed values (a gear count of a gearbox with no
    discrete gears). Never when the field has target evidence of its own: then the evaluator judges that evidence
    as usual (a contradiction stays visible instead of being silently overruled)."""
    own = [e for e in evidence_by_field.get(spec["name"], []) if _has_value(e.get("value"))
           and is_target_market(e.get("market"), target_market)
           and str(e.get("variant_match") or "").lower() not in NON_TARGET_VARIANTS]
    if own and not ignore_own:
        return None
    for rule in spec.get("not_applicable_when") or []:
        other = normalize_field_name(rule.get("field"))
        values = {_value_key(v) for v in rule.get("values") or []}
        for item in evidence_by_field.get(other, []):
            if (str(item.get("variant_match") or "").lower() == "exact"
                    and is_target_market(item.get("market"), target_market)
                    and _value_key(item.get("value")) in values):
                return f"{other}={item.get('value')} ({rule.get('reason') or 'schema rule'})"
    return None


def evaluate_field(spec: dict, evidence: list[dict], declared: dict | None, output_entry: dict | None,
                   target_market: str, last_evidence_seq: int | None = None, output_seq: int | None = None,
                   evidence_seq: dict | None = None, not_applicable_rule: str | None = None,
                   schema_rule_conflict: str | None = None, portability: dict | None = None) -> dict:
    """Did primary research obtain a usable candidate for this requested field?

    Operational, from the model's own research state only (never a truth check):

    * not_applicable  - not applicable by the schema's `applies_to` or `not_applicable_when` rule, or the model
                        said so;
    * ok              - a candidate value backed by a stored evidence record, and the model
                        did not itself mark it unresolved / conflicting / wrong market or trim;
    * unresolved      - the model said unresolved (report_field_status or its own answer);
    * missing         - no candidate value and no evidence record;
    * weak_provenance - a value only in the model's answer, with no evidence record behind it
                        (or the model itself reported weak provenance);
    * conflicting     - the model reported an unresolved conflict, OR at least two target-market
                        candidates that may apply to the target variant (not variant_match=different)
                        have materially different values and the model has not explicitly resolved
                        them (a `conflict_resolved` declaration newer than the latest evidence). A plain
                        `found` does not resolve it. Both candidates are always kept; no value is chosen;
    * foreign_market_only - no candidate is in the server-side target scope and the ones that may apply to the
                        variant are from a known other market (not portable) or from a market the source does
                        not establish (info `market_not_established`; an unknown market is never the target
                        market unless the field's `unknown_market_policy` and the portability verdict allow it);
    * variant_not_exact   - no candidate's server-side binding reaches the field's binding_requirement in the target
                        scope: a target-market candidate bound only below it (variant_match=unclear), or
                        every candidate is about another trim/variant (variant_match=different, from the
                        model or the server-side binding veto) or from a source that does not name the
                        model at all (variant_match=unbound).

    Server scope is authoritative. The two out-of-scope states are decided from the evidence (server-computed
    variant_match and market) BEFORE any model declaration is read: a `found` or `conflict_resolved` declaration
    cannot turn another variant's or another market's evidence into target evidence. A model declaration can still
    make a field less settled (unresolved, conflicting, ...) or not_applicable.

    Different values across markets, or with a candidate explicitly marked as another variant, are
    recorded as info (`multiple_values`), not a trigger: the model decides how to handle them.

    Portability (`portability`, from src/market_portability.py): a foreign item the field's policy lets count for
    the target market is treated as target-scope; its market is not rewritten. Conflict classification
    (src/conflict_normalizer.py): same-scope values that are the same number in other units, or a trim-bound
    scalar inside a model-line range, are not a conflict; every other class keeps the field conflicting.
    """
    from .conflict_normalizer import classify_conflict

    name = spec["name"]
    requirement = field_requirement(spec)
    info: list[str] = []
    portability = portability or {}
    evidence = [{**e, **portability[str(e.get("evidence_id"))]} if str(e.get("evidence_id")) in portability else e
                for e in evidence]
    portable_ids = [str(e.get("evidence_id")) for e in evidence if e.get("portable_to_target_market") is True]
    if any(e.get("portable_to_target_market") is True and not _unknown_market(e) for e in evidence):
        info.append("portable_foreign_fact")
    if any(e.get("portable_to_target_market") is True and _unknown_market(e) for e in evidence):
        info.append("portable_unknown_market_fact")
    declared_status = (declared or {}).get("status")
    out_provenance = str((output_entry or {}).get("provenance") or "").lower()
    out_value = (output_entry or {}).get("value", (output_entry or {}).get("values")) if output_entry else None
    with_value = [e for e in evidence if _has_value(e.get("value"))]
    target_items = [e for e in with_value if in_target_scope(e, target_market)]
    if len({material_key(e.get("value")) for e in (target_items or with_value)}) > 1:
        info.append("multiple_values")
    conflict = same_scope_conflict(evidence, target_market)
    conflict_class = classify_conflict(spec, conflict, target_market) if conflict else None
    if conflict_class and conflict_class["normalized"]:
        info.append(f"conflict_normalized:{conflict_class['class']}")
        conflict = []
    # Freshness: every declaration (and the primary JSON) only counts while no newer evidence exists.
    declared_current = declaration_is_current((declared or {}).get("seq"), last_evidence_seq)
    if declared_status and not declared_current:
        info.append(f"stale_declaration:{declared_status}")
    declared_status = declared_status if declared_current else None
    if not declaration_is_current(output_seq, last_evidence_seq):
        out_provenance = ""
    resolved = declared_status == "conflict_resolved" and (
        not conflict or resolution_is_backed(declared, evidence, conflict, evidence_seq, target_market, requirement))
    if conflict and declared_status == "conflict_resolved":
        info.append("conflict_resolved_by_model" if resolved else "conflict_resolution_not_evidence_backed")

    if not_applicable_rule:
        info.append(f"not_applicable_by_schema_rule:{not_applicable_rule}")
    elif schema_rule_conflict:
        info.append(f"schema_rule_conflict:{schema_rule_conflict}")   # own target evidence vs the N/A rule
    if (not spec.get("applicable", True) or declared_status == "not_applicable"
            or out_provenance == "not_applicable" or not_applicable_rule):
        state = "not_applicable"
    elif declared_status in RETRY_STATES:
        state = declared_status                       # the model's own (current) report wins
    elif out_provenance == "unresolved" and declared_status not in RESOLVED_STATUSES:
        state = "unresolved"
    elif not with_value:
        state = "weak_provenance" if _has_value(out_value) else "missing"
    elif conflict and not resolved:
        state = "conflicting"                         # same scope, different values, not explicitly resolved
    elif not any(in_server_scope(e, target_market, requirement) for e in with_value):
        # nothing in the server-side target scope: no declaration (found / conflict_resolved) can change that
        if all(str(e.get("variant_match") or "").lower() in NON_TARGET_VARIANTS for e in with_value):
            state = "variant_not_exact"
        elif any(binding_satisfies(e, requirement) for e in with_value):
            state = "foreign_market_only"             # the target variant, but only from another market
        elif any(market_in_scope(e, target_market) for e in with_value):
            state = "variant_not_exact"               # target-scope market, binding below the requirement
        else:
            state = "foreign_market_only"
        if state == "foreign_market_only" and any(_unknown_market(e) for e in with_value):
            info.append("market_not_established")    # an unknown source market is not the target market
        if declared_status in RESOLVED_STATUSES:
            info.append(f"declaration_outside_server_scope:{declared_status}")
    else:
        state = "ok"                                  # in-scope evidence (a found / backed resolution changes nothing)
    if state == "variant_not_exact":
        # observational: the binding dimensions that stopped the target-market items (else all items)
        scoped = [e for e in with_value if market_in_scope(e, target_market)] or with_value
        gaps = {g for e in scoped for g in binding_gaps(e, requirement, spec.get("_sanity_propulsion"))}
        info += [f"binding_gap:{g}" for g in sorted(gaps)]
    return {
        "field": name,
        "state": state,
        "info": info,
        "retry_eligible": state in RETRY_STATES,
        "evidence_ids": [e.get("evidence_id") for e in evidence],
        "values": [e.get("value") for e in with_value][:10],
        "markets": sorted({str(e.get("market")) for e in with_value if e.get("market")}),
        "declared": declared,
        "declared_current": declared_current,
        "primary_output": output_entry,
        "conflict_evidence_ids": [e.get("evidence_id") for e in conflict],
        "conflict_class": conflict_class,
        "portable_evidence_ids": portable_ids,
        "portability": portability,
    }


def evaluate_fields(specs: list[dict], events: list[dict], target_market: str = DEFAULT_TARGET_MARKET) -> list[dict]:
    evidence_by_field: dict[str, list[dict]] = {}
    last_seq: dict[str, int] = {}
    evidence_seq: dict[str, int] = {}
    for event in events:
        item = event.get("evidence") if event.get("kind") == "evidence" else None
        if isinstance(item, dict):
            name = normalize_field_name(item.get("field"))
            evidence_by_field.setdefault(name, []).append(item)
            if isinstance(event.get("seq"), int):
                last_seq[name] = event["seq"]
                evidence_seq[str(item.get("evidence_id"))] = event["seq"]
    from .market_portability import assess

    declared = declarations(events)
    parsed, output_seq = primary_output(events, with_seq=True)
    output = {normalize_field_name(name): entry for name, entry in iter_fields(parsed)}
    return [evaluate_field(spec, evidence_by_field.get(spec["name"], []), declared.get(spec["name"]),
                           output.get(spec["name"]), target_market, last_seq.get(spec["name"]), output_seq,
                           evidence_seq, conditional_not_applicable(spec, evidence_by_field, target_market),
                           conditional_not_applicable(spec, evidence_by_field, target_market, ignore_own=True),
                           assess(spec, evidence_by_field.get(spec["name"], []), target_market, is_target_market))
            for spec in specs]


def current_evaluation(events: list[dict], specs: list[dict], target_market: str | None = None) -> list[dict]:
    """THE operational state of requested fields: evaluate_fields over ALL current events. Shared by live
    recovery, the research bundle and run reconstruction, so there is one evaluator and no stale snapshot."""
    started = trace.first_event(events, "run_started") or {}
    market = target_market or started.get("target_market") or DEFAULT_TARGET_MARKET
    return evaluate_fields(specs, events, market)


UNSPECIFIC_VARIANT = {"unclear", "unknown", "different", "unbound"}


def early_resolution_check(spec: dict, events: list[dict], target_market: str) -> tuple[bool, dict]:
    """(may end the attempt early, current evaluation) for one field.

    Stricter than "state is ok": stopping research needs state ok (so no unresolved same-scope conflict)
    AND at least one target-market evidence item whose variant_match is `exact` or absent (fields
    without a variant distinction). A candidate whose variant scope is explicitly unclear / unknown /
    different never ends an attempt early, even when the evaluator accepts it as usable."""
    evaluation = current_evaluation(events, [spec], target_market)[0]
    if evaluation["state"] != "ok":
        return False, evaluation
    portable = set(evaluation.get("portable_evidence_ids") or [])
    for item in trace.evidence_items(events):
        if (normalize_field_name(item.get("field")) == spec["name"] and _has_value(item.get("value"))
                and (is_target_market(item.get("market"), target_market) or str(item.get("evidence_id")) in portable)
                and str(item.get("variant_match") or "").strip().lower() not in UNSPECIFIC_VARIANT):
            return True, evaluation
    return False, evaluation


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


MAX_ATTEMPTED_OPERATIONS = 40
# Keys that describe where/what came out of an operation, not which operation it was.
OUTCOME_KEYS = {"step", "phase", "field", "attempt", "outcome", "hit_count", "result_count", "tables_total", "kinds",
                "chars", "next_offset", "status", "error"}
READ_ONLY_TOOLS = trace.SEARCH_TOOLS + trace.FETCH_TOOLS + ("extract_html", "extract_tables", "find_in_document",
                                                            "inspect_document_for_fields",
                                                            "get_structured_data", "get_cached_document")


def _operation_outcome(name: str, result: dict | None) -> dict:
    """Compact outcome of one executed read-only call (never its content)."""
    if result is None:
        return {"outcome": "no_result_logged"}
    if result.get("error"):
        return {"outcome": "error", "error": str(result.get("error"))[:60]}
    if name in trace.SEARCH_TOOLS:
        n = len(result.get("results") or [])
        return {"outcome": "results" if n else "no_results", "result_count": n}
    if name in trace.FETCH_TOOLS:
        return {"outcome": "fetched", "document_id": result.get("document_id"), "status": result.get("status")}
    if name == "find_in_document":
        n = int(result.get("hit_count") or len(result.get("hits") or []))
        return {"outcome": "hits", "hit_count": n} if n else {"outcome": "no_hits"}
    if name == "inspect_document_for_fields":
        n = len(result.get("fields_with_matches") or [])
        return {"outcome": "fields_located", "hit_count": n} if n else {"outcome": "no_hits"}
    if name == "extract_tables":
        n = int(result.get("tables_total") or 0)
        return {"outcome": "tables", "tables_total": n} if n else {"outcome": "no_tables"}
    if name == "get_structured_data":
        found = [k for k, v in (result.get("found") or {}).items() if v]
        return {"outcome": "structured_data", "kinds": found} if found else {"outcome": "empty"}
    if name == "get_cached_document" and not result.get("found"):
        return {"outcome": "not_found"}
    out = {"outcome": "loaded", "chars": len(result.get("text") or "")}
    if result.get("next_offset") is not None:
        out["next_offset"] = result["next_offset"]
    return out


def attempted_operations(events: list[dict], spec: dict | None = None,
                         limit: int = MAX_ATTEMPTED_OPERATIONS) -> list[dict]:
    """Operational memory for a retry: read-only operations already executed in this vehicle run
    (primary research and earlier field-recovery attempts) with compact outcomes.

    One entry per distinct operation (its latest execution), bounded to `limit`; operations made
    for this field or related to its wording are kept first. Never includes document contents.
    """
    keys = _tokens(spec["name"].replace("_", " "), spec.get("description")) if spec else set()
    by_op: dict[str, dict] = {}
    for pair in trace.tool_pairs(events):
        name = pair["name"]
        if name not in READ_ONLY_TOOLS:
            continue
        args = trace.parse_args(pair["arguments"])
        entry = {"tool": name, "step": pair["step"], "phase": pair.get("phase") or "research"}
        if pair.get("field"):
            entry["field"], entry["attempt"] = pair["field"], pair.get("attempt")
        for key in ("document_id", "key", "query", "url", "domain", "domains", "offset", "scope", "start_table"):
            if args.get(key) not in (None, "", []):
                entry[key] = args[key]
        entry.update(_operation_outcome(name, pair["result"] if isinstance(pair["result"], dict) else None))
        signature = json.dumps({k: v for k, v in entry.items() if k not in OUTCOME_KEYS}, sort_keys=True,
                               ensure_ascii=False, default=str)
        by_op.pop(signature, None)
        by_op[signature] = entry
    ops = list(by_op.values())
    field_name = spec["name"] if spec else None

    def related(op: dict) -> bool:
        return bool(keys & _tokens(op.get("query"))) or op.get("field") == field_name

    if len(ops) > limit:
        keep = [op for op in ops if related(op)][-limit:]
        rest = [op for op in ops if op not in keep][-(limit - len(keep)):] if len(keep) < limit else []
        ops = sorted(keep + rest, key=lambda op: (op.get("step") or 0))
    return ops


CONFLICT_TASK = (
    "This is conflict-resolution research, not a search from scratch. Investigate why {values} differ. "
    "Check exact trim, model year, drivetrain, normal vs temporary boost/performance mode, nominal vs peak "
    "figure, unit conversion, source wording and manufacturer documentation. Preserve every candidate. If "
    "evidence establishes which scope/value applies to the exact target variant, store that evidence and "
    "reply status=conflict_resolved with the applicable value and evidence_ids. Otherwise reply "
    "status=conflicting.")


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
                 previous_attempts: list[dict], operator_notes: dict | None = None,
                 excerpt_limits: dict | None = None) -> dict:
    """The compact context of a focused retry for ONE field. Never the primary conversation.

    `prior_relevant_excerpts` is bounded working memory of material already exposed in this run
    (see src/excerpts.py); it is context, not evidence.
    """
    field_evidence = [e for e in trace.evidence_items(events)
                      if normalize_field_name(e.get("field")) == spec["name"]]
    excerpts, excerpt_stats = select_prior_excerpts(events, spec, field_evidence, doc_metas, **(excerpt_limits or {}))
    packet = {
        "vehicle_identity": vehicle_identity(payload, target_market),
        "requested_field": {k: v for k, v in public_spec(spec).items() if k != "applicable"},
        "failure_reason": evaluation["state"],
        "primary_result": {"info": evaluation.get("info"), "declared": evaluation.get("declared"),
                           "primary_output": evaluation.get("primary_output")},
        "existing_candidates": [{k: e.get(k) for k in ("evidence_id", "value", "unit", "market", "variant",
                                                        "variant_match", "source_url", "document_id")
                                 if e.get(k) is not None} for e in field_evidence],
        "existing_evidence": field_evidence,
        "relevant_documents": related_documents(events, field_evidence, doc_metas),
        "previous_queries_for_this_field": related_searches(events, spec),
        "already_attempted_operations": attempted_operations(events, spec),
        "prior_relevant_excerpts": excerpts,
        "prior_excerpt_stats": excerpt_stats,
        "previous_attempts": previous_attempts,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "turn_budget": max_steps,
    }
    if evaluation["state"] == "conflicting":
        ids = set(evaluation.get("conflict_evidence_ids") or [])
        candidates = [e for e in field_evidence if e.get("evidence_id") in ids] or field_evidence
        packet["conflict"] = {
            "candidates": [{k: e.get(k) for k in ("evidence_id", "value", "unit", "market", "variant",
                                                   "variant_match", "source_url", "document_id", "quote", "note")
                            if e.get(k) is not None} for e in candidates],
            "task": CONFLICT_TASK.format(values=" vs ".join(str(e.get("value")) for e in candidates)),
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

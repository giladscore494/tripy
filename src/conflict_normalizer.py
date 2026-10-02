"""Deterministic conflict classification (no model call, no web search, never a majority vote).

When target-scope evidence for one field carries materially different values, this module says WHY before any
paid recovery is spent on it:

    unit_equivalent                the values are the same quantity in different units ("146 cm" vs 1460 mm)
    scalar_inside_range            a scalar lies inside another item's range (179990 vs 179990-183990)
    market_difference              each value comes from its own market (target market vs a portable foreign fact)
    variant_scope_difference       each value is bound to its own variant scope (binding level / variant wording)
    internal_source_inconsistency  one document (or one source domain) states more than one value (1435 vs 1460)
    true_conflict                  none of the above

Only two outcomes remove the conflict, and both are value identity, not value selection:

    unit_equivalent      every value is the same number after the schema's unit conversions;
    scalar_inside_range  every scalar is inside every range AND every scalar is bound to the exact market trim
                         (a trim's price inside the model-line price range). A scalar whose trim scope is unclear
                         keeps the conflict (spec: keep unresolved when the exact-trim scope remains unclear).

Every other class keeps the field `conflicting`; the class only shapes a compact resolver packet. Nothing here
counts sources, ranks authorities, removes an evidence item or picks a value.
"""

from __future__ import annotations

from typing import Iterable

from .candidate_harvest import OPERATIONS, normalize_term
from .typed_values import typed_value

NORMALIZER_VERSION = "conflict-normalizer-v1"
CLASSES = ("unit_equivalent", "scalar_inside_range", "market_difference", "variant_scope_difference",
           "internal_source_inconsistency", "true_conflict")
NORMALIZING = ("unit_equivalent", "scalar_inside_range")
REL_TOLERANCE = 1e-6


def _unit_map(spec: dict) -> tuple[dict[str, str], list[tuple[set[str], str]]]:
    normalized = spec.get("normalized_unit")
    same = {normalize_term(u) for u in (spec.get("accepted_unit_variants") or []) + (spec.get("expected_units") or [])
            + ([normalized] if normalized else []) if u}
    conversions = []
    for rule in spec.get("conversion_rules") or []:
        if rule.get("operation") in OPERATIONS:
            variants = {normalize_term(v) for v in [rule.get("from_unit", "")] + list(rule.get("from_unit_variants")
                                                                                       or []) if v}
            conversions.append((variants, rule["operation"]))
    return {u: "same" for u in same}, conversions


def _convert(number: float, unit: str | None, spec: dict) -> float | None:
    """`number` in the field's normalized unit, or None when its unit is not one the schema knows."""
    if not unit:
        return number
    same, conversions = _unit_map(spec)
    key = normalize_term(unit)
    if key in same:
        return number
    for variants, operation in conversions:
        if key in variants:
            return float(OPERATIONS[operation](number))
    return None


def interval(item: dict, spec: dict) -> tuple[float, float] | None:
    """(low, high) of an evidence value in the field's normalized unit; None for text or an unknown unit."""
    typed = item.get("typed_value")
    if not isinstance(typed, dict) or typed.get("type") not in ("scalar", "range"):
        typed = typed_value(item.get("value"), unit=item.get("unit"), value_type=spec.get("value_type"),
                            matcher=spec.get("matcher"))
    if typed.get("type") == "scalar" and isinstance(typed.get("value"), (int, float)):
        value = _convert(float(typed["value"]), typed.get("unit") or item.get("unit"), spec)
        return None if value is None else (value, value)
    if typed.get("type") == "range":
        low = _convert(float(typed["min"]), typed.get("unit") or item.get("unit"), spec)
        high = _convert(float(typed["max"]), typed.get("unit") or item.get("unit"), spec)
        return None if low is None or high is None else (min(low, high), max(low, high))
    return None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= REL_TOLERANCE * max(1.0, abs(a), abs(b))


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return _close(a[0], b[0]) and _close(a[1], b[1])


def _classes(items: list[dict], spec: dict) -> list[list[dict]] | None:
    """Group items whose normalized values are identical; None when any value is not numeric."""
    groups: list[tuple[tuple[float, float], list[dict]]] = []
    for item in items:
        span = interval(item, spec)
        if span is None:
            return None
        for key, members in groups:
            if _same(key, span):
                members.append(item)
                break
        else:
            groups.append((span, [item]))
    return [members for _, members in groups]


def _partition_by(groups: list[list[dict]], key) -> bool:
    """Every value group has ONE key value and the groups' keys all differ (each value lives in its own scope)."""
    keys = []
    for members in groups:
        values = {key(m) for m in members}
        if len(values) != 1:
            return False
        keys.append(values.pop())
    return len(set(keys)) == len(keys)


def classify_conflict(spec: dict, items: Iterable[dict], target_market: str | None = None) -> dict:
    """{class, normalized, detail, evidence_ids, version}: why these same-field values differ.
    `normalized` True means the values do not actually disagree (see the module docstring)."""
    items = [i for i in items if isinstance(i, dict)]
    ids = [str(i.get("evidence_id")) for i in items]
    out = {"version": NORMALIZER_VERSION, "evidence_ids": ids}
    groups = _classes(items, spec)
    if groups is not None and len(groups) == 1:
        units = sorted({str(i.get("unit") or (i.get("typed_value") or {}).get("unit") or "") for i in items})
        return {**out, "class": "unit_equivalent", "normalized": True,
                "detail": f"the same value in the field's normalized unit ({spec.get('normalized_unit')}); "
                          f"stated units {units}"}
    if groups is not None:
        spans = {str(i.get("evidence_id")): interval(i, spec) for i in items}
        scalars = [i for i in items if spans[str(i.get("evidence_id"))][0] == spans[str(i.get("evidence_id"))][1]]
        ranges = [i for i in items if i not in scalars]
        if scalars and ranges and len(_classes(scalars, spec) or []) == 1:
            point = spans[str(scalars[0].get("evidence_id"))][0]
            inside = all(spans[str(r.get("evidence_id"))][0] - 1e-9 <= point <= spans[str(r.get("evidence_id"))][1]
                         + 1e-9 for r in ranges)
            if inside:
                trim_bound = all(s.get("binding_level") == "exact_market_trim" for s in scalars)
                return {**out, "class": "scalar_inside_range", "normalized": trim_bound,
                        "detail": (f"{point:g} lies inside every stated range; " +
                                   ("the scalar is bound to the exact market trim, so the range is the wider "
                                    "model-line span" if trim_bound else
                                    "the scalar's exact-trim scope is not established, so the field stays "
                                    "conflicting"))}
    groups = groups or [[i] for i in items]
    markets = {str(i.get("market") or "unknown") for i in items}
    if len(markets) > 1 and _partition_by(groups, lambda m: str(m.get("market") or "unknown")):
        return {**out, "class": "market_difference", "normalized": False,
                "detail": f"each value comes from its own market: {sorted(markets)}"}
    docs = [{str(m.get("document_id") or m.get("source_url")) for m in members} for members in groups]
    domains = [{str(m.get("source_domain") or "") for m in members} - {""} for members in groups]
    shared_doc = any(docs[a] & docs[b] for a in range(len(docs)) for b in range(a + 1, len(docs)))
    shared_domain = any(domains[a] & domains[b] for a in range(len(domains)) for b in range(a + 1, len(domains)))
    if shared_doc or shared_domain:
        return {**out, "class": "internal_source_inconsistency", "normalized": False,
                "detail": "one " + ("document" if shared_doc else "source domain") + " states more than one value"}
    if _partition_by(groups, lambda m: (m.get("binding_level") or "", str(m.get("variant") or "").strip().lower())):
        return {**out, "class": "variant_scope_difference", "normalized": False,
                "detail": "each value is bound to its own variant scope (binding level / variant wording)"}
    return {**out, "class": "true_conflict", "normalized": False, "detail": "the values disagree"}


def resolver_packet(*, spec: dict, items: list[dict], classification: dict, identity: dict,
                    prior_queries: list[str] | None = None) -> dict:
    """The compact context a model needs to settle ONE conflict: identity, the field's meaning, each competing
    evidence item with its quote, source, binding and authority, and the queries already run. Never the whole
    research bundle."""
    keys = ("evidence_id", "value", "unit", "typed_value", "condition", "market", "variant", "variant_match",
            "binding_level", "binding_veto", "source_authority", "source_domain", "source_url", "document_id",
            "quote", "valid_as_of")
    return {
        "vehicle_identity": identity,
        "field": spec.get("name"),
        "field_semantics": spec.get("semantic_definition") or spec.get("description"),
        "conflict_class": classification.get("class"),
        "conflict_detail": classification.get("detail"),
        "competing_evidence": [{k: i.get(k) for k in keys if i.get(k) not in (None, "", [], {})} for i in items],
        "prior_queries": list(prior_queries or [])[-6:],
        "task": ("Establish which value applies to the exact target variant. Keep every item; store new evidence "
                 "and reply conflict_resolved with its evidence_ids only when a source settles the scope."),
    }


def describe(classes: Iterable[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in classes:
        out[c.get("class") or "unknown"] = out.get(c.get("class") or "unknown", 0) + 1
    return out


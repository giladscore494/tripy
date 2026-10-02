"""Market portability: when may an official foreign-market fact count for the target market?

The schema says, per field, how market-dependent a value is (`market_sensitivity`: high | medium | low) and at
which binding level a foreign fact may be ported (`portability_scope`: none | exact_technical_variant). A
foreign-market evidence item is portable ONLY when all of these hold:

    1. the field's policy allows it (portability_scope != none and market_sensitivity != high);
    2. the item binds server-side to the target at (at least) the policy's level, with variant_match = exact;
    3. official evidence exists: among those items at least one comes from an official source class
       (government / manufacturer / importer / media, see src/source_authority.py);
    4. every such foreign item states the same value (unit-normalized): foreign markets that disagree are never
       resolved automatically;
    5. no credible target-market evidence for the field (admitted, not bound to another variant) states a
       different value. Any such contradiction vetoes portability for every foreign item.

The item keeps its source market (a UK fact stays UK: `market` is never rewritten); the evaluator only reads the
derived `portable_to_target_market`, `portability_basis` and `portability_policy`. This is a policy decision about
applicability, never a truth score, and it never removes or reorders evidence.
"""

from __future__ import annotations

from .conflict_normalizer import interval
from .document_binding import LEVELS
from .fields import with_dictionary
from .source_authority import OFFICIAL_CLASSES

PORTABILITY_VERSION = "portability-v1"
NON_TARGET = ("different", "unbound")
UNKNOWN_MARKETS = {"", "unknown", "n/a", "na", "none", "unclear", "?"}


def policy_of(spec: dict) -> str:
    return f"{spec.get('portability_scope') or 'none'}/{spec.get('market_sensitivity') or 'unspecified'}"


def _level(value) -> int:
    return LEVELS.index(value) if value in LEVELS else -1


def _value_key(item: dict, spec: dict):
    span = interval(item, spec)
    if span is not None:
        return ("num", round(span[0], 6), round(span[1], 6))
    return ("txt", str(item.get("value")).strip().lower())


def assess(spec: dict, evidence: list[dict], target_market: str, is_target) -> dict[str, dict]:
    """{evidence_id: {portable_to_target_market, portability_basis, portability_policy}} for every foreign-market
    item with a value. `is_target(market, target_market)` is the evaluator's own market test."""
    spec = with_dictionary(spec)
    policy = policy_of(spec)
    with_value = [e for e in evidence if e.get("value") not in (None, "", [], {})]
    foreign = [e for e in with_value if str(e.get("market") or "").strip().lower() not in UNKNOWN_MARKETS
               and not is_target(e.get("market"), target_market)]
    if not foreign:
        return {}

    def verdict(portable: bool, basis: str) -> dict:
        return {"portable_to_target_market": portable, "portability_basis": basis, "portability_policy": policy}

    scope = spec.get("portability_scope") or "none"
    if scope == "none" or scope not in LEVELS or spec.get("market_sensitivity") == "high":
        return {str(e.get("evidence_id")): verdict(False, "field_policy_not_portable") for e in foreign}
    eligible = [e for e in foreign if str(e.get("variant_match") or "").lower() == "exact"
                and _level(e.get("binding_level")) >= _level(scope)]
    out = {str(e.get("evidence_id")): verdict(False, "binding_below_portability_scope") for e in foreign
           if e not in eligible}
    if not eligible:
        return out
    target = [e for e in with_value if is_target(e.get("market"), target_market)
              and str(e.get("variant_match") or "").lower() not in NON_TARGET]
    values = {_value_key(e, spec) for e in eligible}
    contradiction = next((e for e in target if _value_key(e, spec) not in values), None) if len(values) == 1 else None
    official = [e for e in eligible if e.get("source_authority") in OFFICIAL_CLASSES]
    if contradiction is not None:
        basis = f"vetoed_by_target_market_evidence:{contradiction.get('evidence_id')}"
    elif len(values) > 1:
        basis = "foreign_markets_disagree"
    elif not official:
        basis = "no_official_source"
    else:
        lead = official[0]
        basis = (f"official {lead.get('source_authority')} source ({lead.get('market')}) binds at "
                 f"{lead.get('binding_level')}; policy {policy}; no target-market contradiction")
        out.update({str(e.get("evidence_id")): verdict(True, basis) for e in eligible})
        return out
    out.update({str(e.get("evidence_id")): verdict(False, basis) for e in eligible})
    return out

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

An open-dataset offer admitted by the open-data layer (source_authority `open_dataset`, src/open_data/engine.py) is
official evidence of its own market (EU / FR / US / CA); its `portability_scope_override` (the D-4 table entry of its
(source, field, route) triple, data/open_datasets.json) replaces the field's portability_scope for that item only.
Rules 2-5 apply unchanged.

The item keeps its source market (a UK fact stays UK: `market` is never rewritten); the evaluator only reads the
derived `portable_to_target_market`, `portability_basis` and `portability_policy`. This is a policy decision about
applicability, never a truth score, and it never removes or reorders evidence.

An item whose market the source does not establish (`market` = unknown) is NOT a target-market item either: it
means the server could not establish the source market, not "valid for the target". It is usable for the target
only when the field's schema says so explicitly (`unknown_market_policy: portable`) AND it passes the same
deterministic checks as a portable foreign fact (1-5 above, judged together with the foreign items). Without that
policy (the default, `not_target`), or on a high-sensitivity / portability_scope=none field, it never counts for
the target market. Its verdict carries `market_established: false`.
"""

from __future__ import annotations

from .conflict_normalizer import interval
from .document_binding import LEVELS
from .fields import with_dictionary
from .source_authority import OFFICIAL_CLASSES

PORTABILITY_VERSION = "portability-v1"
NON_TARGET = ("different", "unbound")
UNKNOWN_MARKETS = {"", "unknown", "n/a", "na", "none", "unclear", "?"}
UNKNOWN_MARKET_POLICIES = ("not_target", "portable")
# verdict bases that only restate the field's policy: not a rejected portability candidate (see agent.tail_metrics)
POLICY_ONLY_BASES = ("field_policy_not_portable", "unknown_market_not_target")


def unknown_market_policy(spec: dict) -> str:
    """The schema's explicit policy for items whose market the source does not establish (default not_target)."""
    value = str(spec.get("unknown_market_policy") or "not_target").strip().lower()
    return value if value in UNKNOWN_MARKET_POLICIES else "not_target"


def market_unknown(item: dict) -> bool:
    return str(item.get("market") or "").strip().lower() in UNKNOWN_MARKETS


def policy_of(spec: dict) -> str:
    return f"{spec.get('portability_scope') or 'none'}/{spec.get('market_sensitivity') or 'unspecified'}"


def _level(value) -> int:
    return LEVELS.index(value) if value in LEVELS else -1


def _value_key(item: dict, spec: dict):
    span = interval(item, spec)
    if span is not None:
        return ("num", round(span[0], 6), round(span[1], 6))
    return ("txt", str(item.get("value")).strip().lower())


OPEN_DATASET = "open_dataset"


def _assess_datasets(spec: dict, items: list[dict], with_value: list[dict], target_market: str, is_target,
                     policy: str) -> dict[str, dict]:
    """Rules 2-5 for open-dataset offers, each at its own portability_scope_override."""
    out: dict[str, dict] = {}
    eligible = [e for e in items if str(e.get("variant_match") or "").lower() == "exact"
                and _level(e.get("binding_level")) >= _level(e.get("portability_scope_override"))]
    for e in items:
        if e not in eligible:
            out[str(e.get("evidence_id"))] = {"portable_to_target_market": False,
                                              "portability_basis": "binding_below_portability_scope",
                                              "portability_policy": f"open_dataset:{e.get('portability_scope_override')}"}
    if not eligible:
        return out
    target = [e for e in with_value if is_target(e.get("market"), target_market)
              and str(e.get("variant_match") or "").lower() not in NON_TARGET]
    values = {_value_key(e, spec) for e in eligible}
    contradiction = next((e for e in target if _value_key(e, spec) not in values), None) if len(values) == 1 else None
    if contradiction is not None:
        basis, ok = f"vetoed_by_target_market_evidence:{contradiction.get('evidence_id')}", False
    elif len(values) > 1:
        basis, ok = "foreign_markets_disagree", False
    else:
        lead = eligible[0]
        basis, ok = (f"official open dataset ({lead.get('market')}) binds at {lead.get('binding_level')}; D-4 scope "
                     f"{lead.get('portability_scope_override')}; no target-market contradiction"), True
    for e in eligible:
        out[str(e.get("evidence_id"))] = {"portable_to_target_market": ok, "portability_basis": basis,
                                          "portability_policy": f"open_dataset:{e.get('portability_scope_override')}"}
    return out


def assess(spec: dict, evidence: list[dict], target_market: str, is_target) -> dict[str, dict]:
    """{evidence_id: {portable_to_target_market, portability_basis, portability_policy}} for every item with a value
    that is not from the target market: a known foreign market, or a market the source does not establish
    (those also get `market_established: false`). `is_target(market, target_market)` is the evaluator's own
    market test."""
    spec = with_dictionary(spec)
    policy = policy_of(spec)
    with_value = [e for e in evidence if e.get("value") not in (None, "", [], {})]
    unknown = [e for e in with_value if market_unknown(e)]
    foreign = [e for e in with_value if not market_unknown(e) and not is_target(e.get("market"), target_market)]
    if not foreign and not unknown:
        return {}

    def verdict(portable: bool, basis: str, item: dict | None = None) -> dict:
        out = {"portable_to_target_market": portable, "portability_basis": basis, "portability_policy": policy}
        if item is not None and market_unknown(item):
            out["market_established"] = False
        return out

    scope = spec.get("portability_scope") or "none"
    out: dict[str, dict] = {}
    # open-dataset offers: their own D-4 scope (src/open_data); everything else: the field's policy
    datasets = [e for e in foreign if e.get("source_authority") == OPEN_DATASET
                and e.get("portability_scope_override") in LEVELS]
    if datasets:
        out.update(_assess_datasets(spec, datasets, with_value, target_market, is_target, policy))
        foreign = [e for e in foreign if e not in datasets]
        if not foreign and not unknown:
            return out
    if scope == "none" or scope not in LEVELS or spec.get("market_sensitivity") == "high":
        out.update({str(e.get("evidence_id")): verdict(False, "field_policy_not_portable", e)
                    for e in foreign + unknown})
        return out
    if unknown_market_policy(spec) != "portable":
        out.update({str(e.get("evidence_id")): verdict(False, "unknown_market_not_target", e) for e in unknown})
        unknown = []
    foreign = foreign + unknown
    if not foreign:
        return out
    eligible = [e for e in foreign if str(e.get("variant_match") or "").lower() == "exact"
                and _level(e.get("binding_level")) >= _level(scope)]
    out.update({str(e.get("evidence_id")): verdict(False, "binding_below_portability_scope", e) for e in foreign
                if e not in eligible})
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
        out.update({str(e.get("evidence_id")): verdict(True, basis, e) for e in eligible})
        return out
    out.update({str(e.get("evidence_id")): verdict(False, basis, e) for e in eligible})
    return out

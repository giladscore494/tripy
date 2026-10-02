"""market=unknown means the server could not establish the source market, NOT "valid for the target market".
An unknown-market item counts for the target only where the field's schema says so explicitly
(`unknown_market_policy: portable`) and the deterministic portability verdict accepts it; a known foreign item
only through `portable_to_target_market = true`. A model's `found` (or its market claim) changes neither."""

import pytest

from test_reliability_foundation import GENERIC, corolla, state, store  # noqa: F401  (corolla: fixture)

from src.field_recovery import current_evaluation, in_server_scope, is_target_market
from src.fields import load_schema
from src.market_portability import assess
from src.tools import dispatch

SPECS = {s["name"]: {**s, "applicable": True} for s in load_schema()}
FOUND = {"status": "found"}


def item(eid, field, value, market="IL", level="exact_technical_variant", match="exact", authority="official_manufacturer",
         **kw):
    spec = SPECS[field]
    return {"evidence_id": eid, "field": field, "value": value, "market": market, "binding_level": level,
            "variant_match": match, "binding_requirement": spec.get("binding_requirement"),
            "source_authority": authority, "admission_status": "accepted", **kw}


def evaluate(field, *items, declared=None):
    events = [{"kind": "run_started", "seq": 0, "target_market": "IL"}]
    events += [{"kind": "evidence", "seq": i + 1, "evidence": e} for i, e in enumerate(items)]
    if declared:
        events.append({"kind": "field_status", "seq": 99, "field": field, **declared,
                       "evidence_ids": [e["evidence_id"] for e in items]})
    return current_evaluation(events, [SPECS[field]])[0]


def test_schema_policy_is_explicit():
    assert SPECS["screen_size_in"]["market_sensitivity"] == "high"
    assert SPECS["curb_weight_kg"]["portability_scope"] == "none"
    assert SPECS["torque_nm"]["unknown_market_policy"] == "portable"          # low-sensitivity technical fact
    assert all(s.get("unknown_market_policy") != "portable" for s in SPECS.values()
               if s.get("market_sensitivity") != "low" or s.get("portability_scope") in (None, "none"))


def test_unknown_market_is_not_target_scope_by_itself():
    unknown = item("u", "torque_nm", 610, market="unknown")
    assert not in_server_scope(unknown, "IL")
    assert not is_target_market("unknown", "IL")


@pytest.mark.parametrize("declared", [None, FOUND])
def test_high_sensitivity_field_with_unknown_market_is_not_ok(declared):
    trim_bound = item("u", "screen_size_in", 12.3, market="unknown", level="exact_market_trim")
    result = evaluate("screen_size_in", trim_bound, declared=declared)
    assert result["state"] == "foreign_market_only" and result["retry_eligible"]
    assert "market_not_established" in result["info"] and result["portable_evidence_ids"] == []
    assert result["portability"]["u"]["market_established"] is False
    assert result["portability"]["u"]["portability_basis"] == "field_policy_not_portable"


def test_exact_market_trim_field_with_unknown_market_is_not_ok():
    # exact_market_trim needs the target market inside the binding itself: an unknown market only reaches the
    # technical level, and the field is high-sensitivity / never portable on top of that
    result = evaluate("tire_size_front", item("u", "tire_size_front", "225/45 R17", market="unknown",
                                              match="unclear"), declared=FOUND)
    assert result["state"] != "ok" and result["retry_eligible"]
    assert result["portable_evidence_ids"] == []


def test_portability_scope_none_with_unknown_market_is_not_silently_il():
    result = evaluate("curb_weight_kg", item("u", "curb_weight_kg", 1385, market="unknown"), declared=FOUND)
    assert result["state"] == "foreign_market_only" and "market_not_established" in result["info"]
    assert "declaration_outside_server_scope:found" in result["info"]


def test_low_sensitivity_unknown_market_needs_the_explicit_policy_and_the_portability_verdict():
    unknown = item("u", "torque_nm", 610, market="unknown")
    # explicit schema policy + official source + exact binding + no target contradiction: usable, market kept
    accepted = evaluate("torque_nm", unknown)
    assert accepted["state"] == "ok" and accepted["portable_evidence_ids"] == ["u"]
    assert "portable_unknown_market_fact" in accepted["info"] and accepted["markets"] == ["unknown"]
    assert accepted["portability"]["u"]["market_established"] is False
    # the same field without the explicit policy: never target-usable
    spec = {k: v for k, v in SPECS["torque_nm"].items() if k != "unknown_market_policy"}
    verdict = assess(spec, [unknown], "IL", is_target_market)["u"]
    assert verdict["portable_to_target_market"] is False and verdict["portability_basis"] == "unknown_market_not_target"
    # with the policy, every portability check still applies
    for weak, basis in ((item("u", "torque_nm", 610, market="unknown", authority="aggregator"), "no_official_source"),
                        (item("u", "torque_nm", 610, market="unknown", level="body_powertrain", match="unclear"),
                         "binding_below_portability_scope")):
        result = evaluate("torque_nm", weak, declared=FOUND)
        assert result["state"] != "ok" and result["portability"]["u"]["portability_basis"] == basis
    vetoed = evaluate("torque_nm", unknown, item("i", "torque_nm", 650))
    assert vetoed["portability"]["u"]["portable_to_target_market"] is False
    assert vetoed["portability"]["u"]["portability_basis"].startswith("vetoed_by_target_market_evidence")


def test_known_il_with_correct_binding_is_ok():
    result = evaluate("curb_weight_kg", item("i", "curb_weight_kg", 1385))
    assert result["state"] == "ok" and result["portability"] == {}
    trim = evaluate("screen_size_in", item("i", "screen_size_in", 12.3, level="exact_market_trim"))
    assert trim["state"] == "ok"


def test_known_uk_without_portability_is_foreign_market_only():
    result = evaluate("curb_weight_kg", item("k", "curb_weight_kg", 1385, market="UK"), declared=FOUND)
    assert result["state"] == "foreign_market_only"
    assert result["portability"]["k"]["portable_to_target_market"] is False
    assert "market_not_established" not in result["info"]


def test_known_uk_with_explicit_portability_is_ok_and_stays_uk():
    uk = item("k", "torque_nm", 610, market="UK")
    result = evaluate("torque_nm", uk)
    assert result["state"] == "ok" and result["portable_evidence_ids"] == ["k"]
    assert result["portability"]["k"]["portable_to_target_market"] is True
    assert "market_established" not in result["portability"]["k"]
    assert result["markets"] == ["UK"] and uk["market"] == "UK"                # the source market is never rewritten


def test_model_market_claim_and_found_cannot_make_an_unknown_market_source_il(corolla):
    """Through the admission gate: a source that does not establish its market stays `unknown` whatever market the
    model claims, and a `found` declaration on top cannot make it target-market evidence."""
    stored = store(corolla, field="cargo_volume_l", value=596, document_id=corolla.docs[GENERIC],
                   quote="Boot space: 596 litres", market="IL", variant_match="exact")
    dispatch(corolla, "report_field_status", {"field": "cargo_volume_l", "status": "found",
                                              "evidence_ids": [stored["evidence_id"]]})
    result = state(corolla, "cargo_volume_l")
    assert result["markets"] == ["unknown"] and result["state"] != "ok" and result["retry_eligible"]

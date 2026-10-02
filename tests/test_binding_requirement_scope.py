"""variant_match=unclear is never target-safe Evidence: a value counts for the target only when its server-side
binding reaches the field's binding_requirement. Model declarations (`found`, `conflict_resolved`, a claimed
variant_match) never change that. current_evaluation() stays the one operational field-state authority."""

from src.field_recovery import (RETRY_STATES, binding_satisfies, current_evaluation, early_resolution_check,
                                evaluate_field, in_server_scope)

TRIM_SPEC = {"name": "tire_size_front", "applicable": True, "binding_requirement": "exact_market_trim"}
TECH_SPEC = {"name": "torque_nm", "applicable": True, "binding_requirement": "exact_technical_variant"}


def ev(eid, field, value, market="IL", level="exact_technical_variant", match="exact",
       requirement="exact_technical_variant", **kw):
    return {"evidence_id": eid, "field": field, "value": value, "market": market, "binding_level": level,
            "variant_match": match, "binding_requirement": requirement, "admission_status": "accepted", **kw}


def events(*items, declarations=()):
    out = [{"kind": "run_started", "seq": 0, "target_market": "IL"}]
    out += [{"kind": "evidence", "seq": i + 1, "evidence": item} for i, item in enumerate(items)]
    out += [{"kind": "field_status", "seq": 100 + i, **d} for i, d in enumerate(declarations)]
    return out


def state_of(spec, *items, declarations=()):
    return current_evaluation(events(*items, declarations=declarations), [spec])[0]


# exact_market_trim required, the binding reached only exact_technical_variant (variant_match=unclear)
UNCLEAR_TRIM = ev("e1", "tire_size_front", "255/45 R20", level="exact_technical_variant", match="unclear",
                  requirement="exact_market_trim", model_variant_claim="exact")


def test_unclear_below_exact_market_trim_is_not_ok_and_stays_retry_eligible():
    result = state_of(TRIM_SPEC, UNCLEAR_TRIM)
    assert result["state"] == "variant_not_exact" and result["state"] in RETRY_STATES and result["retry_eligible"]
    assert not in_server_scope(UNCLEAR_TRIM, "IL", "exact_market_trim")
    assert not binding_satisfies(UNCLEAR_TRIM, "exact_market_trim")
    may_stop, _ = early_resolution_check(TRIM_SPEC, events(UNCLEAR_TRIM), "IL")
    assert may_stop is False


def test_model_found_or_resolution_cannot_bypass_an_unclear_binding():
    found = {"field": "tire_size_front", "status": "found", "evidence_ids": ["e1"]}
    result = state_of(TRIM_SPEC, UNCLEAR_TRIM, declarations=[found])
    assert result["state"] == "variant_not_exact" and "declaration_outside_server_scope:found" in result["info"]
    # two unclear target-market values: a conflict that citing an unclear item cannot resolve
    other = ev("e2", "tire_size_front", "235/60 R18", match="unclear", requirement="exact_market_trim")
    resolved = {"field": "tire_size_front", "status": "conflict_resolved", "evidence_ids": ["e2"]}
    result = state_of(TRIM_SPEC, UNCLEAR_TRIM, other, declarations=[resolved])
    assert result["state"] == "conflicting" and "conflict_resolution_not_evidence_backed" in result["info"]


def test_the_binding_level_is_compared_with_the_requirement_not_the_label():
    # a level below the requirement is insufficient even if variant_match were (inconsistently) "exact"
    below = ev("e1", "tire_size_front", "255/45 R20", level="exact_technical_variant", match="exact",
               requirement="exact_market_trim")
    assert state_of(TRIM_SPEC, below)["state"] == "variant_not_exact"
    # the field now requires more than the requirement the item was admitted under: still insufficient
    weaker = ev("e1", "tire_size_front", "255/45 R20", level="exact_technical_variant", match="exact")
    assert state_of(TRIM_SPEC, weaker)["state"] == "variant_not_exact"
    # admitted under a stricter requirement than the field has now: the stricter one still applies
    stricter = ev("e1", "torque_nm", 610, level="exact_technical_variant", match="unclear",
                  requirement="exact_market_trim")
    assert state_of(TECH_SPEC, stricter)["state"] == "variant_not_exact"
    # an unknown level string is never sufficient
    assert state_of(TECH_SPEC, ev("e1", "torque_nm", 610, level="trust_me"))["state"] == "variant_not_exact"
    # the exact_market_trim level does satisfy an exact_market_trim field
    trim_bound = ev("e1", "tire_size_front", "255/45 R20", level="exact_market_trim", requirement="exact_market_trim")
    assert state_of(TRIM_SPEC, trim_bound)["state"] == "ok"


def test_exact_technical_binding_in_the_target_market_is_ok():
    item = ev("e1", "torque_nm", 610)
    result = state_of(TECH_SPEC, item)
    assert result["state"] == "ok" and not result["retry_eligible"]
    assert early_resolution_check(TECH_SPEC, events(item), "IL")[0] is True


def test_different_or_unbound_plus_found_is_variant_not_exact():
    for match, level in (("different", "generation"), ("unbound", "unknown")):
        item = ev("e1", "torque_nm", 610, level=level, match=match, model_variant_claim="exact")
        found = {"field": "torque_nm", "status": "found", "evidence_ids": ["e1"]}
        result = state_of(TECH_SPEC, item, declarations=[found])
        assert result["state"] == "variant_not_exact", match
        assert "declaration_outside_server_scope:found" in result["info"]


def test_out_of_scope_classification():
    unclear_il = ev("e1", "torque_nm", 610, level="generation", match="unclear")
    exact_us = ev("e2", "torque_nm", 650, market="US")
    unclear_us = ev("e3", "torque_nm", 650, market="US", level="generation", match="unclear")
    assert state_of(TECH_SPEC, unclear_il)["state"] == "variant_not_exact"     # target market, binding too weak
    assert state_of(TECH_SPEC, exact_us)["state"] == "foreign_market_only"     # target variant, foreign market
    assert state_of(TECH_SPEC, unclear_il, exact_us)["state"] == "foreign_market_only"
    assert state_of(TECH_SPEC, unclear_us)["state"] == "foreign_market_only"
    # one target-safe item is enough; the unclear one stays a kept candidate
    assert state_of(TECH_SPEC, unclear_il, ev("e4", "torque_nm", 610))["state"] == "ok"


def test_legacy_items_without_a_server_binding_keep_their_behaviour():
    legacy = {"evidence_id": "e1", "field": "torque_nm", "value": 610, "market": "IL"}
    assert evaluate_field(TECH_SPEC, [legacy], None, None, "IL")["state"] == "ok"
    assert evaluate_field(TECH_SPEC, [{**legacy, "variant_match": "unclear"}], None, None, "IL")["state"] == \
        "variant_not_exact"


def test_admitted_trim_field_below_its_requirement_is_retried_end_to_end(tmp_path, make_ctx):
    """Through the real admission gate: the XPeng spec page binds tire_size_front at exact_technical_variant
    (its trim "MAX" is a generic word, so the trim is never bound) while the field needs exact_market_trim."""
    from test_field_recovery import _call, ev as store_args, say, scripted_run, turn

    script = [turn(_call("p1", "store_evidence", {**store_args("tire_size_front", "255/45 R20"),
                                                  "variant_match": "exact"}),
                   _call("p2", "report_field_status", {"field": "tire_size_front", "status": "found"})),
              say({"vehicle_id": "101122", "summary": "primary", "fields": {}}),
              say({"field": "tire_size_front", "status": "unresolved"}),
              say({"field": "tire_size_front", "status": "unresolved"}),
              say({"vehicle_id": "101122", "summary": "final", "fields": {}})]
    result, _, _ = scripted_run(tmp_path, make_ctx, script, requested_fields=["tire_size_front"],
                                layered_harvest_enabled=False)
    [item] = result["evidence"]
    assert (item["variant_match"], item["binding_level"], item["binding_requirement"], item["model_variant_claim"]) \
        == ("unclear", "exact_technical_variant", "exact_market_trim", "exact")
    rec = result["field_recovery"]
    assert rec["primary_states"] == {"variant_not_exact": 1} and rec["queue"] == ["tire_size_front"]
    assert result["research_bundle"]["field_states"]["tire_size_front"]["state"] != "ok"

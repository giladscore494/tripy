"""PR 1 reliability foundation: evidence admission, server-side variant binding, source authority, schema semantics
and applicability, typed values, temporal evidence and cross-field sanity checks. Regression-driven by the first real
Toyota Corolla Touring Sports 1.8 Hybrid run. Offline: no network, no model call."""

import json

import pytest

from fixtures.corolla_touring import (CARTUBE, COMPARE, GENERIC, LAUNCH, PAYLOAD, TOYOTA_UK, TWO_LITRE, VEHICLE,
                                      put_documents)

from src.bundle import build_research_bundle
from src.candidate_harvest import harvest_text
from src.consistency_checks import run_checks
from src.document_binding import level_index, target_identity
from src.evidence_admission import AdmissionContext
from src.field_recovery import current_evaluation, early_resolution_check, evaluate_fields
from src.fields import load_schema, propulsion_of, public_spec, resolve_requested_fields
from src.source_authority import classify_source, source_market
from src.tools import dispatch
from src.tools.evidence import EvidenceNotAdmitted, EvidenceStore
from src.typed_values import range_contains, typed_value

PLUG_FIELDS = {"electric_range_km", "electric_range_standard", "ac_charging_time", "dc_charging_time",
               "dc_charging_window_pct", "ac_max_charging_power_kw", "dc_max_charging_power_kw",
               "energy_consumption_kwh_100km"}


@pytest.fixture
def corolla(make_ctx):
    """A tool context for the Corolla run: admission context from the Level 1.5 record, fixture documents cached,
    every emitted event recorded (with seq) so the shared evaluator can read them."""
    ctx = make_ctx()
    specs = resolve_requested_fields(None, propulsion=propulsion_of(PAYLOAD))
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    events = [{"kind": "run_started", "seq": 0, "target_market": "IL",
               "requested_field_specs": [public_spec(s) for s in specs]}]
    ctx.log = lambda kind, **data: events.append({"kind": kind, "seq": len(events), **data})
    ctx.docs, ctx.events, ctx.specs = put_documents(ctx.cache), events, specs
    return ctx


def store(ctx, **args):
    return dispatch(ctx, "store_evidence", args)


def item(ctx, evidence_id):
    return next(e for e in ctx.evidence.items if e["evidence_id"] == evidence_id)


def state(ctx, field):
    return next(e for e in current_evaluation(ctx.events, ctx.specs, "IL") if e["field"] == field)


# --- cargo contamination: the 2.0 Hybrid fact never becomes exact-target evidence ---------------------------

def test_cargo_fact_of_the_2_0_hybrid_is_not_bound_to_the_1_8_target(corolla):
    out = store(corolla, field="cargo_volume_l", value=581, unit="l", document_id=corolla.docs[TWO_LITRE],
                quote="Boot space: 581 litres", market="IL", variant="Corolla Touring Sports Hybrid",
                variant_match="exact")
    assert out["stored"] and out["variant_match"] == "different"           # the model's "exact" is not authority
    ev = item(corolla, out["evidence_id"])
    assert ev["model_variant_claim"] == "exact" and ev["variant_match"] == "different"
    assert level_index(ev["binding_level"]) < level_index("exact_technical_variant")
    assert any(v.startswith("displacement_mismatch") for v in ev["binding_veto"])
    # manufacturer, model, body and year all match: the powertrain alone vetoes exact binding
    dims = ev["binding_dimensions"]
    assert {dims[d]["status"] for d in ("model", "year", "body", "propulsion")} == {"match"}
    assert dims["displacement"]["status"] == "mismatch"
    evaluation = state(corolla, "cargo_volume_l")
    assert evaluation["state"] == "variant_not_exact" and evaluation["retry_eligible"]     # back to recovery
    assert early_resolution_check({"name": "cargo_volume_l", "applicable": True}, corolla.events, "IL")[0] is False


def test_a_multi_variant_table_binds_each_column_to_its_own_powertrain(corolla):
    doc = corolla.docs[COMPARE]
    wrong = store(corolla, field="cargo_volume_l", value=581, unit="l", document_id=doc, quote="581 ליטר",
                  variant_match="exact")
    right = store(corolla, field="cargo_volume_l", value=596, unit="l", document_id=doc, quote="596 ליטר")
    assert (wrong["variant_match"], right["variant_match"]) == ("different", "exact")
    assert item(corolla, wrong["evidence_id"])["binding_dimensions"]["displacement"] == {"status": "mismatch",
                                                                                         "basis": "column_header"}
    assert item(corolla, right["evidence_id"])["binding_level"] in ("exact_technical_variant", "exact_market_trim")
    assert state(corolla, "cargo_volume_l")["state"] == "ok"                 # 596 is target evidence; 581 is info
    assert state(corolla, "cargo_volume_l")["conflict_evidence_ids"] == []   # a 2.0 value is never a same-scope conflict


def test_model_exact_declarations_never_override_server_binding(corolla):
    unbound = store(corolla, field="cargo_volume_l", value=596, unit="l", document_id=corolla.docs[GENERIC],
                    quote="Boot space: 596 litres", market="IL", variant_match="exact")
    assert unbound["variant_match"] == "unbound" and item(corolla, unbound["evidence_id"])["model_variant_claim"] == "exact"
    assert state(corolla, "cargo_volume_l")["state"] == "variant_not_exact"
    # a model's own "different" is always respected (it can lower, never raise)
    lowered = store(corolla, field="gearbox_type", value="e-CVT", document_id=corolla.docs[CARTUBE],
                    quote="תיבת הילוכים: e-CVT", variant_match="different")
    assert lowered["variant_match"] == "different"
    # every stored item carries the SERVER binding: no item can be "exact" by declaration alone
    assert all(e["variant_match"] in ("exact", "unclear", "different", "unbound") and "binding_level" in e
               for e in corolla.evidence.items)


# --- value / source entailment -----------------------------------------------------------------------------

def test_gearbox_type_is_admitted_but_a_gear_count_inferred_from_e_cvt_is_not(corolla):
    doc = corolla.docs[CARTUBE]
    gearbox = store(corolla, field="gearbox_type", value="e-CVT", document_id=doc, quote="תיבת הילוכים: e-CVT")
    assert gearbox["stored"] and gearbox["variant_match"] == "exact"
    inferred = store(corolla, field="gear_count", value=1, document_id=doc, quote="תיבת הילוכים: e-CVT")
    assert inferred == {**inferred, "stored": False, "rejected": True, "reasons": ["unsupported_inference"]}
    assert [e["field"] for e in corolla.evidence.items] == ["gearbox_type"]
    rejected = [e for e in corolla.events if e["kind"] == "evidence_rejected"]
    assert rejected[0]["field"] == "gear_count" and rejected[0]["reasons"] == ["unsupported_inference"]
    # schema policy: no discrete stepped gear count for an e-CVT -> gear_count is not applicable, not "missing"
    evaluation = state(corolla, "gear_count")
    assert evaluation["state"] == "not_applicable" and not evaluation["retry_eligible"]
    assert any(i.startswith("not_applicable_by_schema_rule:gearbox_type") for i in evaluation["info"])
    # a stated count is still admitted
    count = harvest_text("8-speed automatic", load_schema())
    assert ("gear_count", 8) in {(c["field"], c["value"]) for c in count}


def test_numeric_values_need_the_number_or_an_approved_conversion(corolla):
    doc = corolla.docs[CARTUBE]
    converted = store(corolla, field="height_mm", value=1460, unit="mm", document_id=doc, quote='גובה 146.0 ס"מ')
    assert converted["stored"] and converted["entailment"].startswith("deterministic_parse")
    not_normalized = store(corolla, field="height_mm", value=146, unit="cm", document_id=doc, quote='גובה 146.0 ס"מ')
    assert not_normalized["reasons"] == ["unit_not_normalized"]
    wrong_value = store(corolla, field="height_mm", value=1470, document_id=doc, quote='גובה 146.0 ס"מ')
    assert wrong_value["reasons"] == ["value_not_in_quote"]
    not_in_doc = store(corolla, field="height_mm", value=1460, document_id=doc, quote="Height 1,460 mm")
    assert not_in_doc["reasons"] == ["quote_not_in_source"]
    bare = store(corolla, field="height_mm", value=1460, document_id=doc, quote="146.0")
    assert bare["reasons"] == ["quote_too_short"]                                       # a number alone says nothing


def test_semantic_definition_keeps_hybrid_torque_to_the_engine(corolla):
    doc = corolla.docs[CARTUBE]
    system = store(corolla, field="torque_nm", value=185, unit="Nm", document_id=doc,
                   quote="מומנט מנוע בנזין: 142 ניוטון-מטר; מומנט משולב: 185 ניוטון-מטר")
    assert system["reasons"] == ["semantic_mismatch"]
    engine = store(corolla, field="torque_nm", value=142, unit="Nm", document_id=doc,
                   quote="מומנט מנוע בנזין: 142 ניוטון-מטר; מומנט משולב: 185 ניוטון-מטר")
    assert engine["stored"]
    spec = next(s for s in load_schema() if s["name"] == "torque_nm")
    assert "COMBUSTION ENGINE" in spec["semantic_definition"] and "semantic_definition" in public_spec(spec)
    assert "semantic_exclusions" not in public_spec(spec)                            # policy is not model input


def test_climate_zones_need_an_explicit_schema_mapping(corolla):
    doc = corolla.docs[CARTUBE]
    mapped = store(corolla, field="climate_zones", value=2, document_id=doc, quote="בקרת אקלים: מפוצלת")
    assert mapped["stored"] and mapped["entailment"] == "deterministic_parse:pattern"
    spec = next(s for s in load_schema() if s["name"] == "climate_zones")
    assert any("מפוצל" in p["regex"] and p["value"] == 2 and "schema mapping" in p["note"] for p in spec["patterns"])
    guessed = store(corolla, field="climate_zones", value=3, document_id=doc, quote="בקרת אקלים: מפוצלת")
    assert guessed["reasons"] in (["unsupported_inference"], ["value_not_in_quote"])


# --- booleans --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, field, expected", [
    ("מושבים חשמליים: אין", "power_seats", False),
    ("חימום מושבים: אין", "heated_seats", False),
    ("חלון גג: אין", "sunroof_panoramic", False),
    ("מושבים חשמליים: יש", "power_seats", True),
    ("מושבים חשמליים\nאין", "power_seats", False),
    ("ללא חלון גג", "sunroof_panoramic", False),
    ("Heated seats | ✓", "heated_seats", True),
    ("מושבים חשמליים", "power_seats", None),           # label only: never true
    ("חימום מושבים", "heated_seats", None),
    ("גג פנורמי", "sunroof_panoramic", None),
])
def test_boolean_parser_reads_the_stated_value_and_never_a_bare_label(text, field, expected):
    values = [c["value"] for c in harvest_text(text, load_schema()) if c["field"] == field]
    assert values == ([] if expected is None else [expected])


def test_boolean_admission_explicit_absent_and_label_only(corolla):
    doc = corolla.docs[CARTUBE]
    absent = store(corolla, field="power_seats", value=False, document_id=doc, quote="מושבים חשמליים: אין")
    present = store(corolla, field="power_seats", value=True, document_id=doc, quote="מושבים חשמליים: אין")
    assert absent["stored"] and present["reasons"] == ["value_not_stated"]
    label_only = store(corolla, field="heated_seats", value=True, document_id=corolla.docs[COMPARE],
                       quote="טויוטה קורולה טורינג ספורט 2024")
    assert label_only["reasons"] == ["value_not_stated"]
    assert [(e["field"], e["value"]) for e in corolla.evidence.items] == [("power_seats", False)]


# --- notes are commentary only -----------------------------------------------------------------------------

def test_notes_never_supply_a_value_market_variant_or_provenance(corolla):
    doc = corolla.docs[CARTUBE]
    note = "Toyota IL typically 3y/100,000 km; this figure came from European Auto-Data"
    years = store(corolla, field="warranty_years", value=3, document_id=doc, quote="אחריות: שלוש שנים", note=note,
                  market="IL")
    km = store(corolla, field="warranty_km", value=100000, document_id=doc, quote="אחריות: שלוש שנים", note=note)
    assert years["stored"] and km["reasons"] == ["value_not_in_quote"]
    ev = item(corolla, years["evidence_id"])
    assert ev["source_url"] == CARTUBE and ev["source_authority"] == "aggregator" and ev["market"] == "IL"
    assert ev["market_basis"] == "domain_tld:.il"                       # provenance from the source, not the note
    bundle = build_research_bundle(corolla.events, PAYLOAD, cache=corolla.cache, target_market="IL")
    text = json.dumps(bundle, ensure_ascii=False)
    assert "Auto-Data" not in text and "100,000" not in text            # the note never reaches the finalizer
    assert bundle["evidence"][0]["source_authority"] == "aggregator"
    assert bundle["evidence"][0]["sanity_status"] == "not_checkable"


# --- provenance: retrieval, market, authority ---------------------------------------------------------------

def test_only_retrieved_documents_are_sources(corolla):
    snippet = store(corolla, field="fuel_tank_l", value=43, source_url="https://www.toyota.co.uk/never-fetched",
                    quote="Fuel tank capacity 43 l")
    assert snippet["reasons"] == ["source_not_retrieved"]
    missing_quote = store(corolla, field="fuel_tank_l", value=50, document_id=corolla.docs[TOYOTA_UK])
    assert missing_quote["reasons"] == ["quote_missing"]
    from_candidate = store(corolla, field="fuel_tank_l", value=43, document_id=corolla.docs[TOYOTA_UK])
    ev = item(corolla, from_candidate["evidence_id"])
    assert ev["admission_checks"]["quote"] == "from_deterministic_candidate" and "43" in ev["quote"]
    hev_only = store(corolla, field="electric_range_km", value=50, document_id=corolla.docs[CARTUBE],
                     quote="אחריות: שלוש שנים")
    assert hev_only["reasons"] == ["field_not_applicable_for_vehicle"]


def test_market_comes_from_the_source_and_foreign_official_facts_keep_their_market(corolla):
    uk = store(corolla, field="fuel_tank_l", value=43, unit="l", document_id=corolla.docs[TOYOTA_UK],
               quote="Fuel tank capacity 43 l", market="IL")
    ev = item(corolla, uk["evidence_id"])
    assert (ev["market"], ev["market_basis"], ev["model_market_claim"]) == ("UK", "domain_tld:.uk", "IL")
    assert ev["source_authority"] == "official_manufacturer" and ev["variant_match"] == "exact"
    assert state(corolla, "fuel_tank_l")["state"] == "foreign_market_only"     # never rewritten as IL
    unknown = store(corolla, field="cargo_volume_l", value=596, document_id=corolla.docs[GENERIC],
                    quote="Boot space: 596 litres", market="IL")
    assert item(corolla, unknown["evidence_id"])["market"] == "unknown"        # an IL claim is not verified
    assert source_market("https://www.example.com/x", "טויוטה קורולה טורינג ספורט " * 5) == ("IL", "hebrew_text:1.00")


@pytest.mark.parametrize("url, expected", [
    ("https://www.gov.il/he/departments/x", "government"),
    ("https://data.gov.il/dataset/x", "government"),
    ("https://www.toyota.co.uk/x.pdf", "official_manufacturer"),
    ("https://www.toyota-europe.com/x", "official_manufacturer"),
    ("https://www.toyota.co.il/models/corolla", "official_importer"),
    ("https://newsroom.toyota.eu/corolla", "official_media"),
    ("https://www.cartube.co.il/toyota/corolla", "aggregator"),
    ("https://www.auto-data.net/en/toyota-corolla", "aggregator"),
    ("https://www.yad2.co.il/vehicles/x", "marketplace"),
    ("https://www.icar.co.il/x", "publisher"),
    ("https://www.cadillac.com/x", "unknown"),           # another brand's official site is not authoritative here
    ("https://random.example/x", "unknown"),
])
def test_source_authority_classes(url, expected):
    assert classify_source(url, "טויוטה")["source_authority"] == expected


def test_authority_is_not_identity(corolla):
    """An official manufacturer page about another variant is still another variant."""
    from src.document_binding import bind, document_profile

    identity = target_identity(PAYLOAD, VEHICLE)
    profile = document_profile(text="Toyota Corolla Touring Sports 2024 2.0 Hybrid. Boot 581 l.", title="",
                               url="https://www.toyota.co.uk/corolla-2-0", identity=identity)
    assert classify_source("https://www.toyota.co.uk/corolla-2-0", "טויוטה")["source_authority"] == "official_manufacturer"
    assert bind(identity, profile["statuses"], market="UK")["variant_match"] == "different"


# --- typed values and conditions ----------------------------------------------------------------------------

def test_ranges_are_typed_and_scalars_compare_against_them(corolla):
    out = store(corolla, field="cargo_volume_l", value="581-588", unit="l", document_id=corolla.docs[CARTUBE],
                quote="נפח תא מטען 581-588 ליטר", condition="seats folded")
    ev = item(corolla, out["evidence_id"])
    assert ev["value"] == "581-588"                                                 # raw / display value kept
    assert ev["typed_value"] == {"type": "range", "min": 581, "max": 588, "unit": "l", "condition": None}
    assert ev["admission_checks"]["condition"] == "dropped_not_in_quote"           # a condition is never invented
    assert typed_value(4.5, unit="l/100km") == {"type": "scalar", "value": 4.5, "unit": "l/100km", "condition": None}
    assert typed_value("4.6-5.1", value_type="number")["type"] == "range"
    price = typed_value("179,990-183,990", matcher="price", value_type="number")
    assert (price["min"], price["max"]) == (179990, 183990)
    assert range_contains(price, typed_value(179990, matcher="price")) is True
    assert range_contains(price, typed_value(183991)) is False
    assert typed_value("581–588 (VDA)", value_type="number")["condition"] == "VDA"
    assert typed_value(False, value_type="boolean") == {"type": "boolean", "value": False}
    assert typed_value("5 years / 100,000 km", matcher="warranty")["type"] == "compound"


def test_a_condition_stated_by_the_quote_is_kept(make_ctx):
    from conftest import cache_source

    ctx = make_ctx()
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, resolve_requested_fields(None, propulsion="hybrid"))
    doc = cache_source(ctx.cache, "https://www.example.co.il/corolla-ts",
                       "טויוטה קורולה טורינג ספורט 2024 1.8 היברידי: גובה 1,460 מ\"מ with roof rails")
    out = dispatch(ctx, "store_evidence", {"field": "height_mm", "value": 1460, "document_id": doc,
                                           "quote": "גובה 1,460 מ\"מ with roof rails", "condition": "with roof rails"})
    assert ctx.evidence.items[0]["condition"] == "with roof rails" and out["condition"] == "with roof rails"


# --- applicability -----------------------------------------------------------------------------------------

def test_propulsion_applicability_and_coverage_denominator():
    from src.benchmark import compute_metrics

    names = {p: {s["name"] for s in resolve_requested_fields(None, propulsion=p) if s["applicable"]}
             for p in ("conventional", "hybrid", "plug_in", "battery_electric")}
    assert not names["hybrid"] & PLUG_FIELDS and len(names["hybrid"]) == 37          # a regular HEV does not plug in
    assert PLUG_FIELDS <= names["plug_in"] and len(names["plug_in"]) == 45
    assert PLUG_FIELDS <= names["battery_electric"] and "fuel_tank_l" not in names["battery_electric"]
    assert not names["conventional"] & (PLUG_FIELDS | {"battery_gross_kwh"}) and "fuel_tank_l" in names["conventional"]
    assert {"battery_gross_kwh", "battery_usable_kwh"} <= names["hybrid"]
    hev = {"requested_fields": {n: n for n in names["hybrid"]}, "output": {"fields": {}}}
    assert compute_metrics(hev)["target_fields"] == 37                                 # the coverage denominator
    evaluation = evaluate_fields(resolve_requested_fields(None, propulsion="hybrid"), [], "IL")
    assert {e["field"] for e in evaluation if e["state"] == "not_applicable"} == PLUG_FIELDS


# --- time ----------------------------------------------------------------------------------------------------

def test_valid_as_of_is_preserved_never_fabricated(corolla):
    launch = corolla.docs[LAUNCH]
    stated = store(corolla, field="list_price", value=179990, unit="ILS", document_id=launch,
                   quote='מחיר: 179,990 ש"ח', valid_as_of="2024-01")
    ev = item(corolla, stated["evidence_id"])
    assert (ev["valid_as_of"], ev["temporal_basis"], ev["temporal_status"]) == ("2024-01", "stated_in_source", "dated")
    corolla.evidence = EvidenceStore()
    invented = store(corolla, field="list_price", value=179990, unit="ILS", document_id=launch,
                     quote='מחיר: 179,990 ש"ח', valid_as_of="2025-06-01")
    ev = item(corolla, invented["evidence_id"])
    assert ev["admission_checks"]["valid_as_of"] == "dropped_not_in_source"
    assert (ev["valid_as_of"], ev["temporal_basis"]) == ("2024-01-10", "json_ld:datePublished")   # metadata date
    undated = store(corolla, field="warranty_years", value=3, document_id=corolla.docs[CARTUBE],
                    quote="אחריות: שלוש שנים")
    ev = item(corolla, undated["evidence_id"])
    assert ev["temporal_status"] == "undated" and "valid_as_of" not in ev                 # no date invented
    assert ev["observed_at"]                                                              # fetch time is separate
    technical = store(corolla, field="fuel_tank_l", value=43, document_id=corolla.docs[TOYOTA_UK],
                      quote="Fuel tank capacity 43 l")
    assert "temporal_status" not in item(corolla, technical["evidence_id"])              # not time-sensitive


# --- cross-field sanity checks -------------------------------------------------------------------------------

def _ev(eid, field, value, variant_match="exact"):
    return {"evidence_id": eid, "field": field, "value": value, "variant_match": variant_match}


def test_co2_and_fuel_consumption_signal():
    ok = run_checks([_ev("e1", "fuel_consumption_combined_l_100km", 4.5)], PAYLOAD)
    co2 = next(c for c in ok["checks"] if c["check"] == "co2_vs_fuel_consumption")
    assert co2["status"] == "consistent" and "103" in co2["detail"]                   # Corolla: 103 g/km ~ 4.4 l
    bad = run_checks([_ev("e1", "fuel_consumption_combined_l_100km", 9.0)], PAYLOAD)
    assert next(c for c in bad["checks"] if c["check"] == "co2_vs_fuel_consumption")["status"] == "suspicious"
    assert bad["by_evidence"]["e1"] == "suspicious"
    other_variant = run_checks([_ev("e1", "fuel_consumption_combined_l_100km", 9.0, "different")], PAYLOAD)
    assert next(c for c in other_variant["checks"]
                if c["check"] == "co2_vs_fuel_consumption")["status"] == "not_checkable"
    assert run_checks([], {})["summary"]["suspicious"] == 0


def test_curb_gross_and_tire_rim_signals():
    curb = lambda value: next(c for c in run_checks([_ev("e1", "curb_weight_kg", value)], PAYLOAD)["checks"]
                              if c["check"] == "curb_vs_gross_mass")["status"]
    assert (curb(1420), curb(1900), curb(500)) == ("consistent", "suspicious", "suspicious")   # gross 1835 kg
    rims = lambda rim: next(c for c in run_checks([_ev("e1", "rim_diameter_in", rim),
                                                   _ev("e2", "tire_size_front", "225/45 R17")], PAYLOAD)["checks"]
                            if c["check"] == "rim_vs_tire_size")["status"]
    assert (rims(17), rims(18)) == ("consistent", "suspicious")
    hev_charging = run_checks([_ev("e1", "ac_charging_time", "3 h")], PAYLOAD)
    assert next(c for c in hev_charging["checks"] if c["check"] == "fields_vs_propulsion")["status"] == "suspicious"


def test_sanity_checks_never_change_values_or_states(corolla):
    store(corolla, field="fuel_consumption_combined_l_100km", value=4.5, document_id=corolla.docs[CARTUBE],
          quote='צריכת דלק משולבת: 4.5 ליטר ל-100 ק"מ')
    before = [dict(e) for e in corolla.evidence.items]
    states = current_evaluation(corolla.events, corolla.specs, "IL")
    bundle = build_research_bundle(corolla.events, PAYLOAD, cache=corolla.cache, target_market="IL")
    assert corolla.evidence.items == before and current_evaluation(corolla.events, corolla.specs, "IL") == states
    check = next(c for c in bundle["consistency_checks"] if c["check"] == "co2_vs_fuel_consumption")
    assert check["status"] == "consistent" and bundle["evidence"][0]["sanity_status"] == "consistent"


# --- the store refuses anything that bypassed admission; old runs still load --------------------------------

def test_evidence_store_refuses_unadmitted_payloads():
    store_ = EvidenceStore()
    with pytest.raises(EvidenceNotAdmitted):
        store_.add_or_reuse({"field": "torque_nm", "value": 142, "variant_match": "exact"})
    assert store_.items == [] and not hasattr(store_, "add")


def test_legacy_evidence_without_admission_fields_is_evaluated_as_before():
    legacy = [{"kind": "evidence", "seq": 1, "evidence": {"evidence_id": "e1", "field": "torque_nm", "value": 142,
                                                         "market": "IL", "variant_match": "exact",
                                                         "source_url": "https://x.co.il"}}]
    spec = [{"name": "torque_nm", "applicable": True}]
    assert current_evaluation(legacy, spec, "IL")[0]["state"] == "ok"
    assert early_resolution_check(spec[0], legacy, "IL")[0] is True
    bundle = build_research_bundle(legacy, {}, target_market="IL")
    assert bundle["evidence"][0]["evidence_id"] == "e1" and bundle["evidence_admission"]["admitted"] == 1


# --- end to end: the real run's polluted stores, replayed through run_vehicle --------------------------------

def test_corolla_run_rejects_the_polluted_evidence_end_to_end(tmp_path, make_ctx):
    from test_tools_smoke import ScriptedGLM, _call

    from src.agent import AgentConfig, run_vehicle
    from src.benchmark import compute_metrics
    from src.storage.run_log import RunLog
    from src.tools import ToolConfig

    ctx = make_ctx()
    docs = put_documents(ctx.cache)
    stores = [  # what the real run stored, every item claiming variant_match=exact and market IL
        ("cargo_volume_l", 581, docs[TWO_LITRE], "Boot space: 581 litres"),
        ("gearbox_type", "e-CVT", docs[CARTUBE], "תיבת הילוכים: e-CVT"),
        ("gear_count", 1, docs[CARTUBE], "תיבת הילוכים: e-CVT"),
        ("power_seats", True, docs[CARTUBE], "מושבים חשמליים: אין"),
        ("warranty_years", 3, docs[CARTUBE], "אחריות: שלוש שנים"),
        ("warranty_km", 100000, docs[CARTUBE], "אחריות: שלוש שנים"),
        ("fuel_tank_l", 43, docs[TOYOTA_UK], "Fuel tank capacity 43 l"),
    ]
    calls = [_call(f"p{i}", "store_evidence", {"field": f, "value": v, "document_id": d, "quote": q, "market": "IL",
                                               "variant_match": "exact",
                                               "note": "Toyota IL typically 3y/100,000 km"})
             for i, (f, v, d, q) in enumerate(stores)]
    client = ScriptedGLM([{"role": "assistant", "content": "", "tool_calls": calls},
                          {"role": "assistant", "content": json.dumps({"summary": "primary", "fields": {}})},
                          {"role": "assistant", "content": json.dumps({"summary": "final", "fields": {}})}])
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path / "runs", "b", "38626"), vehicle_meta=VEHICLE,
                         config=AgentConfig(max_steps=2, no_new_research_turns=0, field_recovery_enabled=False),
                         tool_config=ToolConfig(), session=ctx.session)
    admitted = {(e["field"], e["variant_match"], e["market"]) for e in result["evidence"]}
    assert admitted == {("cargo_volume_l", "different", "unknown"), ("gearbox_type", "exact", "IL"),
                        ("warranty_years", "exact", "IL"), ("fuel_tank_l", "exact", "UK")}
    assert result["evidence_admission"]["rejected_by_reason"] == {"unsupported_inference": 1, "value_not_in_quote": 1,
                                                                  "value_not_stated": 1}
    states = {name: s["state"] for name, s in result["research_bundle"]["field_states"].items()}
    assert (states["cargo_volume_l"], states["gear_count"], states["power_seats"], states["fuel_tank_l"]) == \
        ("variant_not_exact", "not_applicable", "missing", "foreign_market_only")
    assert states["electric_range_km"] == "not_applicable" and len(result["requested_fields"]) == 37   # HEV: no plug
    m = compute_metrics(result)
    assert (m["target_fields"], m["evidence_rejected"], m["evidence_variant_exact"], m["evidence_variant_different"]) \
        == (37, 3, 3, 1)
    assert m["evidence_aggregator_source"] == 2 and m["evidence_official_source"] == 1
    assert "100,000" not in json.dumps(result["research_bundle"]["evidence"], ensure_ascii=False)

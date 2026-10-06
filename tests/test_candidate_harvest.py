"""Deterministic candidate harvesting: strategies (structured key/value, alias proximity, textual forms,
booleans), reversed-Hebrew PDF lines, the candidate cache, and the hard invariant candidate != evidence."""

import json

import pytest

from fixtures.cadillac_lyriq import (EU_HTML, IL_SPEC, IL_SPEC_HTML, PAYLOAD, PRICE_PDF_TEXT, VEHICLE,
                                     put_documents)

import src.candidate_harvest as harvest_mod
from src.candidate_harvest import (RunHarvester, candidate_matrix, harvest_document, harvest_text, looks_reversed,
                                   normalize_text, reverse_hebrew_line, schema_hash)
from src.field_recovery import current_evaluation
from src.fields import load_schema, resolve_requested_fields
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.tools.extract import _html_tables, _structured, visible_text

BEV = resolve_requested_fields(None, propulsion="battery_electric")


@pytest.fixture(scope="module")
def specs():
    return load_schema()


def by_field(cands):
    out = {}
    for c in cands:
        out.setdefault(c["field"], []).append(c)
    return out


def test_normalization_is_conservative_and_length_preserving():
    raw = "מומנט מרבי 610 נ״מ – 0‑100 קמ”ש"
    norm = normalize_text(raw)
    assert len(norm) == len(raw) and 'נ"מ' in norm and "0-100 קמ\"ש" in norm
    assert normalize_text("Maximum TORQUE") == "maximum torque"


def test_reversed_hebrew_pdf_lines_are_repaired_only_when_detected():
    visual = 'מ"מ 4880 ךרוא'
    assert looks_reversed(visual) and reverse_hebrew_line(visual) == 'אורך 4880 מ"מ'
    assert not looks_reversed('אורך 4880 מ"מ')                          # logical text is left alone
    cands = by_field(harvest_text(PRICE_PDF_TEXT, load_schema(), is_pdf=True))
    assert cands["list_price"][0]["value"] == 399990 and cands["list_price"][0]["reversed_hebrew_normalized"]
    assert cands["registration_licence_fee"][0]["value"] == 2595
    assert "יושיר" in cands["registration_licence_fee"][0]["quote"] or "רישוי" in cands["registration_licence_fee"][0]["quote"]
    # never applied to non-PDF text
    assert not harvest_text('מ"מ 4880 ךרוא', load_schema())


def test_hebrew_spec_table_key_value(specs):
    cands = by_field(harvest_text(visible_text(IL_SPEC_HTML), specs, tables=_html_tables(IL_SPEC_HTML), url=IL_SPEC))
    expect = {"torque_nm": 610, "acceleration_0_100_s": 5.3, "battery_gross_kwh": 102,
              "energy_consumption_kwh_100km": 22.5, "wheelbase_mm": 3094, "cargo_volume_l": 588, "gear_count": 1,
              "screen_size_in": 33, "apple_carplay": True, "heated_seats": True, "rim_diameter_in": 22,
              "tire_size_front": "265/50 R22", "vehicle_warranty": "5 years / 100,000 km",
              "battery_hybrid_warranty": "8 years / 160,000 km"}
    for field, value in expect.items():
        assert value in [c["value"] for c in cands[field]], field
    torque = next(c for c in cands["torque_nm"] if c["extraction_method"] == "table_row")
    assert torque["parser_confidence"] >= 0.9 and torque["market_hint"] == "IL" and torque["unit"] == "Nm"
    assert torque["quote"] == 'מומנט מרבי | 610 נ"מ' and torque["source_url"] == IL_SPEC
    assert torque["table_index"] == 0 and torque["row_index"] == 1
    assert "warranty_years" in cands and cands["warranty_years"][0]["value"] == 5   # vehicle warranty only


def test_english_table_multi_column_keeps_variant_hints(specs):
    html = ("<table><tr><th>Spec</th><th>Luxury</th><th>Sport</th></tr>"
            "<tr><td>Max. torque (Nm)</td><td>610</td><td>650</td></tr>"
            "<tr><td>Front tyres</td><td>255/45 R20</td><td>275/40 R22</td></tr>"
            "<tr><td>Ventilated seats</td><td>-</td><td>Standard</td></tr></table>")
    cands = by_field(harvest_text("", specs, tables=_html_tables(html)))
    assert {(c["value"], c["variant_hint"]) for c in cands["torque_nm"]} == {(610, "Luxury"), (650, "Sport")}
    assert {(c["value"], c["variant_hint"]) for c in cands["tire_size_front"]} == {("255/45 R20", "Luxury"),
                                                                                   ("275/40 R22", "Sport")}
    assert {(c["value"], c["availability"]) for c in cands["ventilated_seats"]} == {(False, "absent"),
                                                                                    (True, "standard")}


def test_json_ld_and_plain_text_strategies(specs):
    structured = by_field(harvest_text("", specs, structured=_structured(EU_HTML)))
    assert structured["wheelbase_mm"][0]["value"] == 3094 and structured["wheelbase_mm"][0]["extraction_method"] == \
        "structured_data"
    assert structured["cargo_volume_l"][0]["value"] == 588 and structured["torque_nm"][0]["value"] == 610
    text = by_field(harvest_text("The LYRIQ produces 610 Nm of torque and has a 102 kWh battery.", specs))
    assert text["torque_nm"][0]["value"] == 610 and text["battery_gross_kwh"][0]["value"] == 102
    assert text["torque_nm"][0]["extraction_method"] == "alias_proximity"
    split = by_field(harvest_text("מומנט מרבי\n610 נ\"מ", specs))           # label on one line, value on the next
    assert split["torque_nm"][0]["extraction_method"] == "label_next_line"


def test_compound_phrases_emit_several_fields(specs):
    cands = by_field(harvest_text("DC fast charging from 10%-80% in 33 minutes.", specs))
    assert cands["dc_charging_time"][0]["value"] == "33 min" and cands["dc_charging_time"][0]["minutes"] == 33
    assert cands["dc_charging_window_pct"][0]["value"] == "10-80"
    warranty = by_field(harvest_text("Vehicle warranty: 5 years / 150,000 km", specs))
    assert warranty["warranty_years"][0]["value"] == 5 and warranty["warranty_km"][0]["value"] == 150000
    gearbox = by_field(harvest_text("8AT gearbox", specs))
    assert gearbox["gear_count"][0]["value"] == 8 and gearbox["gearbox_type"][0]["value"] == "automatic"
    cvt = by_field(harvest_text("Transmission: e-CVT", specs))
    assert cvt["gearbox_type"][0]["value"] == "e-cvt" and "gear_count" not in cvt


def test_tire_axles_are_never_assumed(specs):
    labelled = by_field(harvest_text("Front 255/45 R21, rear 285/40 R21; optional 275/35 R22", specs))
    assert labelled["tire_size_front"][0]["value"] == "255/45 R21"
    assert labelled["tire_size_rear"][0]["value"] == "285/40 R21"
    assert labelled["alternative_tire_sizes"][0]["value"] == "275/35 R22"
    unlabelled = by_field(harvest_text("Tyres 255/45 R20", specs))
    front = unlabelled["tire_size_front"][0]
    assert front["ambiguity"] == "single_size_all_wheels" and front["parser_confidence"] >= 0.5


# --- candidate != evidence ----------------------------------------------------------------------------

def test_a_candidate_is_never_evidence(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    record = cache.put("fetch", "https://x.example/spec", b"Maximum torque 610 Nm",
                       {"doc_type": "text", "final_url": "https://x.example/spec"}, "Maximum torque 610 Nm")
    log = RunLog(tmp_path / "runs", "b", "1")
    specs = resolve_requested_fields(["torque_nm"])
    log.event("run_started", requested_field_specs=specs, target_market="IL")
    harvester = RunHarvester(cache, specs, log)
    harvester.observe([record["document_id"]], "research")
    events = read_events(log.events_path)
    harvested = [e for e in events if e["kind"] == "candidates_harvested"]
    assert harvested and harvested[0]["candidates"][0]["value"] == 610           # the candidate exists
    assert not [e for e in events if e["kind"] in ("evidence", "field_status")]  # EvidenceStore untouched
    assert current_evaluation(events, specs, "IL")[0]["state"] == "missing"      # never ok from a candidate
    matrix = candidate_matrix(events, specs)
    assert matrix["fields"]["torque_nm"][0]["value"] == 610 and matrix["candidate_count"] == 1


# --- one parse per document, cache reuse -------------------------------------------------------------

def test_one_document_is_parsed_once_for_all_fields_and_the_harvest_is_reused(tmp_path, monkeypatch):
    cache = DocumentCache(tmp_path / "cache")
    ids = put_documents(cache)
    calls = {"segments": 0}
    original = harvest_mod.document_segments

    def counting(*args, **kwargs):
        calls["segments"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(harvest_mod, "document_segments", counting)
    cands, hit = harvest_document(cache, ids[0], BEV)
    assert not hit and calls["segments"] == 1                                   # ONE pass for every field
    fields = {c["field"] for c in cands}
    assert len(fields) >= 25 and {"torque_nm", "battery_gross_kwh", "dc_max_charging_power_kw", "length_mm",
                                  "width_mm", "wheelbase_mm", "screen_size_in", "apple_carplay",
                                  "rim_diameter_in", "tire_size_front", "vehicle_warranty"} <= fields
    again, hit = harvest_document(cache, ids[0], BEV)                            # another vehicle, same schema
    assert hit and again == cands and calls["segments"] == 1                     # no rescan
    derived = list((tmp_path / "cache" / "documents" / ids[0]).glob("derived_field_candidates_*.json"))
    assert [p.name for p in derived] == [f"derived_field_candidates_{schema_hash(BEV)}.json"]
    ice = resolve_requested_fields(None, propulsion="conventional")              # propulsion-scoped sanity is part of the cache key
    assert harvest_document(cache, ids[0], ice)[1] is False


def test_cadillac_fixture_harvests_all_documents_across_all_fields(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    ids = put_documents(cache)
    log = RunLog(tmp_path / "runs", "b", "85095")
    log.event("run_started", requested_field_specs=BEV, target_market="IL")
    harvester = RunHarvester(cache, BEV, log)
    harvester.observe(ids, "research")
    matrix = candidate_matrix(read_events(log.events_path), BEV, {**VEHICLE, **PAYLOAD["identity"]})
    assert matrix["applicable_fields"] == 45 and matrix["documents"] >= 6
    assert len(matrix["fields_with_candidates"]) >= 35 and matrix["candidate_count"] >= 50
    # conflicts preserved, nothing merged: Israeli 610 Nm and US 650 Nm stay separate candidates
    assert {610, 650} <= {c["value"] for c in matrix["fields"]["torque_nm"]}
    assert any(c.get("trim_mentioned") for c in matrix["fields"]["torque_nm"])   # hints, not truth
    assert any(c.get("official_domain") for c in matrix["fields"]["torque_nm"])
    assert harvester.stats["documents_harvested"] == 10

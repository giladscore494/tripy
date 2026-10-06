"""Identity anchors PR: government identity anchors (A1-A4), importer spec sheets (B1-B3), the production source policy
(C) and the decision fixes of Part 0 (C4, D1-D4). No network: fixtures in tests/fixtures/pr48_*.

The open structured data layer (Part D) is tested in tests/test_pr48_open_data.py.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from fixtures.corolla_harvest import put
from fixtures import pr48_m4_sheets as M
from src.candidate_harvest import (harvest_text, line_marker, logical_lines, map_logical_span, rtl_dictionary,
                                   visual_to_logical_map, dictionary_for)
from src.db import build_level15_payload
from src.document_binding import (apply_fingerprint, gov_code_table_codes, power_anchor, power_gate,
                                  version_by_codes)
from src.evidence_admission import AdmissionContext, admit, logical_quote_span
from src.fields import load_schema, resolve_requested_fields
from src.gov_registry import facts, identity_fingerprint
from src.storage.cache import DocumentCache

ROOT = Path(__file__).resolve().parent.parent
FAMILY_ROWS = json.loads((ROOT / "tests/fixtures/pr48_catalog_families.json").read_text("utf-8"))["rows"]
SNAPSHOT = {r["upstream_record_id"]: r for r in
            json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]}
SPECS = resolve_requested_fields(None, propulsion="conventional")


def _code_types() -> dict:
    out: dict = {}
    for row in FAMILY_ROWS:
        out.setdefault(int(row["degem_cd"]), {"type_codes": [], "years": []})["type_codes"].append(row["degem_nm"])
    return out


def payload(record: str) -> dict:
    return build_level15_payload(SNAPSHOT[record])


def fingerprint(record: str) -> dict:
    return identity_fingerprint(payload(record), FAMILY_ROWS, _code_types(), catalog="fixture")


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


def m4_adm() -> AdmissionContext:
    adm = AdmissionContext.for_run(payload("23678"), None, SPECS, "IL")
    apply_fingerprint(adm.identity, fingerprint("23678"))
    return adm


def _admit_candidates(cache, url: str, text: str, adm=None, fields=None) -> dict[str, dict]:
    from src.candidate_harvest import harvest_document

    adm = adm or m4_adm()
    doc = put(cache, url, text, "pdf")
    cands, _ = harvest_document(cache, doc, SPECS)
    out = {}
    for cand in cands:
        if fields and cand["field"] not in fields:
            continue
        decision = admit(adm, cache, {"field": cand["field"], "value": cand["value"], "quote": cand["quote"],
                                      "document_id": doc}, [doc])
        if decision["accepted"] and cand["field"] not in out:
            out[cand["field"]] = decision["record"]
    return out


# --- Part 0 -------------------------------------------------------------------------------------------------------------

def _logical(text: str) -> tuple[str, dict]:
    d = dictionary_for(load_schema())
    rows, verdict = logical_lines(text, is_pdf=True, words=rtl_dictionary(d.hebrew_aliases),
                                  hebrew_aliases=d.hebrew_aliases)
    return "\n".join(line for line, _, _ in rows), verdict


def test_c4_code_table_is_read_from_the_visually_reversed_sheet():
    assert gov_code_table_codes(M.SHEET_2024_05_TEXT) == []          # the raw visual text has no readable table
    logical, verdict = _logical(M.SHEET_2024_05_TEXT)
    assert verdict["reversed"] is True and verdict["backwards"] > verdict["as_read"]
    assert gov_code_table_codes(logical) == [2423]
    assert gov_code_table_codes(M.SHEET_2023_TEXT) == [1847, 2009]
    # a rating scale ("1 2 3 ... 15") and the regulation's year (התשס"ט 2009) are never codes
    assert 2009 not in gov_code_table_codes(logical) and 15 not in gov_code_table_codes(logical)


def _item(eid: str, value, raw: str, operation: str, **extra) -> dict:
    return {"evidence_id": eid, "field": "torque_nm", "value": value, "unit": "Nm", "market": "IL",
            "variant_match": "exact", "binding_level": "exact_technical_variant",
            "binding_requirement": "exact_technical_variant", "source_authority": "official_importer",
            "stated_number": {"raw": raw, "operation": operation}, **extra}


def test_d1_values_that_differ_only_by_conversion_rounding_are_one_value():
    from src.field_recovery import evaluate_fields
    from src.final_assembly import assemble_output

    items = [_item("a", 647, "66", "multiply_by_9_80665"), _item("b", 649, "66.2", "multiply_by_9_80665"),
             _item("c", 650, "650", "identity"), _item("d", 650, "66.3", "multiply_by_9_80665")]
    events = [{"kind": "run_started", "target_market": "IL", "seq": 1}] + \
        [{"kind": "evidence", "evidence": i, "seq": n + 2} for n, i in enumerate(items)]
    specs = resolve_requested_fields(["torque_nm"], propulsion="conventional")
    state = evaluate_fields(specs, events, "IL")[0]
    assert state["state"] == "ok" and state["conflict_class"]["class"] == "rounding_equivalent"
    assert state["conflict_class"]["representative"] == "c"
    output, _ = assemble_output(events, payload("23678"), specs)
    assert output["fields"]["torque_nm"]["value"] == 650                    # the most precise statement
    # two different values stated in Nm stay a conflict
    two = [_item("x", 650, "650", "identity"), _item("y", 640, "640", "identity")]
    events = [{"kind": "run_started", "target_market": "IL", "seq": 1}] + \
        [{"kind": "evidence", "evidence": i, "seq": n + 2} for n, i in enumerate(two)]
    assert evaluate_fields(specs, events, "IL")[0]["state"] == "conflicting"


def test_d2_front_and_rear_rims_are_split_and_a_staggered_set_is_not_a_conflict():
    found = {(c["field"], c["value"]) for c in harvest_text("צמיגים ק' 275/35R19 , א' 285/30R20 ●", SPECS)}
    assert {("rim_diameter_front_in", 19), ("rim_diameter_rear_in", 20), ("tire_size_front", "275/35 R19"),
            ("tire_size_rear", "285/30 R20")} <= found
    from src.field_recovery import evaluate_fields

    def ev(eid, field, value):
        return {"kind": "evidence", "evidence": {"evidence_id": eid, "field": field, "value": value, "unit": "in",
                                                 "market": "IL", "variant_match": "exact",
                                                 "binding_level": "exact_market_trim"}}
    events = [{"kind": "run_started", "target_market": "IL"}, ev("f", "rim_diameter_front_in", 19),
              ev("r", "rim_diameter_rear_in", 20), ev("a", "rim_diameter_in", 19), ev("b", "rim_diameter_in", 20)]
    specs = resolve_requested_fields(["rim_diameter_in", "rim_diameter_front_in", "rim_diameter_rear_in"])
    states = {e["field"]: e for e in evaluate_fields(specs, events, "IL")}
    assert states["rim_diameter_front_in"]["state"] == "ok" and states["rim_diameter_rear_in"]["state"] == "ok"
    assert states["rim_diameter_in"]["state"] == "not_applicable"
    assert "staggered_axles:front=19,rear=20" in states["rim_diameter_in"]["info"]


def test_d2_the_registry_emits_each_axle_and_one_rim_only_when_they_agree():
    def axle(size):
        return {"majority": size, "share": 0.95, "n": 40, "top": [{"size": size, "n": 38}]}
    staggered = {r["field"]: r["value"] for r in facts({"front": axle("275/35 R19"), "rear": axle("285/30 R20")})}
    assert staggered["rim_diameter_front_in"] == 19 and staggered["rim_diameter_rear_in"] == 20
    assert "rim_diameter_in" not in staggered
    square = {r["field"]: r["value"] for r in facts({"front": axle("255/40 R19"), "rear": axle("255/40 R19")})}
    assert square["rim_diameter_in"] == 19


def test_d3_km_per_litre_becomes_l_per_100km_with_one_decimal():
    found = {c["field"]: c["value"] for c in harvest_text('צריכת דלק משולבת 10.2 ק"מ לליטר', SPECS)}
    assert found["fuel_consumption_combined_l_100km"] == 9.8


def test_d4_imperial_units_convert_exactly_and_never_across_test_cycles():
    def values(text):
        return {c["field"]: c["value"] for c in harvest_text(text, SPECS)}
    assert values("Torque: 295 lb-ft @ 3000 rpm")["torque_nm"] == 400
    assert values("Wheelbase: 114.6 in")["wheelbase_mm"] == 2911
    assert values("Fuel tank 19.0 gal")["fuel_tank_l"] == 71.9
    assert values("Curb weight 3,616 lbs")["curb_weight_kg"] == 1640
    assert "fuel_consumption_combined_l_100km" not in values("Combined fuel economy 25 mpg")
    assert "acceleration_0_100_s" not in values("0-60 mph 5.4 s")


# --- A1: the identity fingerprint ----------------------------------------------------------------------------------------

def test_a1_fingerprint_of_23678():
    fp = fingerprint("23678")
    assert (fp["tozeret_cd"], fp["degem_cd"], fp["type_code"], fp["year"]) == (143, 2276, "31AZ", 2024)
    assert {1847, 2218, 2276} <= set(fp["code_family"])
    # MILO: degem_cd 2009 is type 31BA (the M4 Competition xDrive), not the 31AZ family (same tozeret_cd + type code)
    assert 2009 not in fp["code_family"] and 2423 not in fp["code_family"]
    assert fp["approval_route"]["route"] == "european"
    assert fp["code_years"] == {"degem_cd": [2023, 2024], "type_code": [2021, 2024]}
    assert fp["homologation"]["co2_wltp"] == 223 and fp["equipment_on"] == 390856
    assert fp["equivalent_codes"] == []
    assert fp["other_code_types"][2423] == ["21HK"] and fp["other_code_types"][2009] == ["31BA"]


def test_a1_fingerprint_of_85095_records_27_as_an_equivalent_code():
    fp = fingerprint("85095")
    assert {19, 22, 23, 25, 27, 30, 33, 44, 45} <= set(fp["code_family"])
    assert fp["equivalent_codes"] == [27]                   # identical registry row and equipment bitmask, not merged
    assert fp["approval_route"]["route"] == "european" and fp["homologation"]["co2_wltp"] is None


def test_a1_without_a_catalog_the_family_is_the_own_code():
    fp = identity_fingerprint(payload("23678"))
    assert fp["code_family"] == [2276] and fp["catalog"] == "unavailable"


# --- A2: version by codes -------------------------------------------------------------------------------------------------

def test_a2_the_2023_sheet_is_the_targets_version_and_the_2024_05_sheet_another():
    identity = m4_adm().identity
    assert version_by_codes([1847, 2009], identity)["status"] == "target"
    assert version_by_codes([1847, 2009], identity)["basis"] == "code_family"
    assert version_by_codes([2276], identity)["basis"] == "degem_cd"
    other = version_by_codes([2423], identity)
    assert other["status"] == "other_code_family" and other["type_codes"] == {"2423": ["21HK"]}
    assert version_by_codes([], identity) is None                              # no codes: nothing changes
    assert version_by_codes([], identity, "BMW M4 type 31AZ")["basis"] == "type_code"
    assert version_by_codes([9999], identity)["status"] == "unknown"           # a code the catalog does not know


# --- A3: visually reversed PDFs ------------------------------------------------------------------------------------------

def test_a3_the_reversed_sheet_is_read_for_codes_engine_and_trim_and_quotes_map_back(cache):
    logical, _ = _logical(M.SHEET_2024_05_TEXT)
    assert "2423 BMW M4" in logical and "נפח מנוע סמ\"ק 2,993" in logical
    assert "רמת אבזור COMPETITION" in logical
    power = next(line for line in logical.splitlines() if line.startswith("הספק המנוע"))
    assert power == 'הספק המנוע / בסל"ד סל"ד / כ"ס 6,250 / 510'      # rpm / hp, in the order of its units
    found = {c["field"]: c["value"] for c in harvest_text(logical, SPECS)}
    assert found.get("torque_nm") == 650                                       # 66.3 kgm
    line = 'ˆ 275/35R19 הדימב םיימדק םיגימצ'
    logical_line, mapping = visual_to_logical_map(line)
    assert logical_line == 'צמיגים קדמיים במידה 275/35R19 ˆ'
    start = logical_line.index("קדמיים")
    span = map_logical_span(mapping, start, start + len("קדמיים"))
    assert line[span[0]:span[1]] == "םיימדק"
    doc = put(cache, "https://www.bmw.co.il/x/bmw-m4-coupe-mifrat-2024-05.pdf", M.SHEET_2024_05_TEXT, "pdf")
    adm = m4_adm()
    material = adm.material(cache, doc, None, [doc])
    assert material.doc.reversed_document is True and material.doc.gov_codes == [2423]
    back = logical_quote_span(material, "צמיגים קדמיים במידה 275/35R19")
    assert back["original"].startswith("275/35R19") and back["original_line"] == line


# --- A4: the power anchor ------------------------------------------------------------------------------------------------

def test_a4_power_matches_either_definition_of_koah_sus():
    assert power_anchor(272, 203, "kw")["definition"] == "hp_from_kw"           # CTS: mechanical hp
    assert power_anchor(272, 172, "kw")["match"] is False
    assert power_anchor(272, 268, "hp")["definition"] == "ps_from_hp"           # GM 268 hp -> PS
    assert power_anchor(510, 375, "kw")["definition"] == "ps_from_kw"           # M4: PS
    assert power_anchor(252, 245, "hp")["match"] is False                       # +-3 % is never a gate
    from src.document_binding import StatedPower

    assert power_gate(StatedPower(276.0, "kw", 203), 272) is True
    assert power_gate(StatedPower(475.0, "hp"), 486, 0.03, neighbour=475) is False     # the neighbour's power


# --- B1: spec-sheet discovery ---------------------------------------------------------------------------------------------

IMPORTER_PAGE = """<html><head><title>BMW M4 Coupe</title></head><body><h1>BMW M4 Competition</h1>
<a href="/content/dam/bmw/marketIL/price-lists/bmw-m4-mifrat-2023.pdf">מפרט להורדה</a>
<a href="https://www.cartube.co.il/images/mifrat/bmw/bmw-m4-mifrat-2023.pdf">מפרט טכני (cartube)</a>
<a href="/he/topics/offers.html">מבצעים</a></body></html>"""


def _allow(tmp_path, *domains: str) -> None:
    from src import source_authority

    source_authority.set_policy_overlay_dir(tmp_path / "derived")
    for domain in domains:
        source_authority.update_policy(domain, "allowed", terms_clause="test: reviewed terms", checked_at="2026-10-06")


class _Ctx:
    def __init__(self, cache):
        from src.tools import ToolConfig

        self.cache, self.counters, self.vehicle, self.config = cache, Counter(), {"manufacturer": "ב מ וו"}, ToolConfig()
        self.events: list = []
        self.admission = m4_adm()

    def emit(self, kind, **data):
        self.events.append((kind, data))

    def note_document(self, *a, **k):
        pass


@pytest.mark.real_source_policy
def test_b1_a_sheet_link_is_followed_from_an_allowed_importer_page_and_never_guessed(tmp_path, cache):
    from src.il_version_pages import resolve_spec_sheets
    from src.tools.extract import html_title, visible_text

    _allow(tmp_path, "bmw.co.il")
    page_url = "https://www.bmw.co.il/he/all-models/m-series/m4/bmw-m4-coupe.html"
    searched, fetched = [], []

    def search(query, domain):
        searched.append((query, domain))
        return {"results": [{"url": page_url, "title": "BMW M4 Coupe"}]}

    def fetch(url):
        fetched.append(url)
        if url == page_url:
            meta = {"status": 200, "final_url": url, "doc_type": "html", "title": html_title(IMPORTER_PAGE)}
            return cache.put("fetch", url, IMPORTER_PAGE.encode("utf-8"), meta, visible_text(IMPORTER_PAGE))
        return {"document_id": put(cache, url, M.SHEET_2023_TEXT, "pdf")}

    out = resolve_spec_sheets(_Ctx(cache), None, payload=payload("23678"), search=search, fetch=fetch)
    assert out["allowed"] == ["bmw.co.il"] and out["searches"] == 1
    assert searched[0][0] == "site:bmw.co.il M4 COMPETITION מפרט 2024"
    sheet = "https://www.bmw.co.il/content/dam/bmw/marketIL/price-lists/bmw-m4-mifrat-2023.pdf"
    assert fetched == [page_url, sheet]                       # only offered links; nothing built from a pattern
    assert [s["url"] for s in out["sheets"]] == [sheet]
    refused = [c for c in out["candidates"] if c.get("decision") == "policy_blocked_host"]
    assert [c["url"] for c in refused] == [M.SHEET_2023_CARTUBE_URL]    # a cartube-hosted importer sheet: never fetched


@pytest.mark.real_source_policy
def test_b1_no_search_at_all_without_an_allowed_importer_domain(cache):
    from src.il_version_pages import resolve_spec_sheets

    out = resolve_spec_sheets(_Ctx(cache), None, payload=payload("23678"),
                              search=lambda q, d: pytest.fail("searched"), fetch=lambda u: pytest.fail("fetched"))
    assert out["searches"] == 0 and out["skipped"][0]["reason"] == "no_allowed_importer_domain"


# --- B2: spec-sheet identity and precedence -------------------------------------------------------------------------------

@pytest.mark.real_source_policy
def test_b2_the_2023_sheet_binds_the_market_trim_and_the_2024_05_sheet_is_vetoed(tmp_path, cache):
    _allow(tmp_path, "bmw.co.il")
    records = _admit_candidates(cache, M.SHEET_2023_URL, M.SHEET_2023_TEXT)
    for field, value in (("curb_weight_kg", 1816), ("screen_size_in", 10.25), ("torque_nm", 650),
                         ("rim_diameter_front_in", 19), ("rim_diameter_rear_in", 20), ("apple_carplay", True),
                         ("heated_seats", True), ("climate_zones", 3), ("fuel_consumption_combined_l_100km", 9.8),
                         ("ground_clearance_mm", 120), ("fuel_tank_l", 59), ("cargo_volume_l", 440)):
        record = records[field]
        assert record["value"] == value, field
        assert record["binding_basis"] == "importer_spec_sheet" and record["variant_match"] == "exact", field
        assert record["spec_sheet"]["status"] == "target" and record["spec_sheet"]["hosted_by"] == "bmw.co.il"
        assert record["source_authority"] == "official_importer"
    assert records["curb_weight_kg"]["binding_level"] == "exact_market_trim"
    other = _admit_candidates(cache, "https://www.bmw.co.il/x/bmw-m4-coupe-mifrat-2024-05.pdf", M.SHEET_2024_05_TEXT)
    assert other, "the reversed sheet is still harvested"
    for record in other.values():
        assert record["variant_match"] == "different"
        assert "other_code_family_mismatch@document_codes" in record["binding_veto"]
        assert record["binding_level"] in ("unknown", "model_family", "generation", "body_powertrain")


def _ev(eid, field, value, authority, basis=None, unit=None):
    item = {"evidence_id": eid, "field": field, "value": value, "unit": unit, "market": "IL", "variant_match": "exact",
            "binding_level": "exact_market_trim", "binding_requirement": "exact_market_trim",
            "source_authority": authority}
    if basis:
        item["binding_basis"] = basis
    return {"kind": "evidence", "evidence": item}


def test_b2_the_sheet_beats_a_non_official_document_and_keeps_its_value_as_an_alternative():
    from src.field_recovery import evaluate_fields
    from src.final_assembly import assemble_output

    events = [{"kind": "run_started", "target_market": "IL"},
              _ev("sheet-w", "curb_weight_kg", 1816, "official_importer", "importer_spec_sheet", "kg"),
              _ev("pub-w", "curb_weight_kg", 1800, "publisher", None, "kg"),
              _ev("sheet-s", "screen_size_in", 10.25, "official_importer", "importer_spec_sheet", "in"),
              _ev("pub-s", "screen_size_in", 14.9, "unknown", None, "in")]
    specs = resolve_requested_fields(["curb_weight_kg", "screen_size_in"])
    states = {e["field"]: e for e in evaluate_fields(specs, events, "IL")}
    assert states["curb_weight_kg"]["state"] == "ok" and states["curb_weight_kg"]["superseded_evidence_ids"] == ["pub-w"]
    output, _ = assemble_output(events, payload("23678"), specs)
    weight = output["fields"]["curb_weight_kg"]
    assert weight["value"] == 1816 and weight["evidence_ids"] == ["sheet-w"]
    assert [a["value"] for a in weight["alternatives"]] == [1800] and "superseded_by_spec_sheet" in weight["notes"]
    assert output["fields"]["screen_size_in"]["value"] == 10.25
    # two target sheets that disagree: conflicting, nothing superseded
    events.append(_ev("sheet-w2", "curb_weight_kg", 1825, "official_importer", "importer_spec_sheet", "kg"))
    states = {e["field"]: e for e in evaluate_fields(specs, events, "IL")}
    assert states["curb_weight_kg"]["state"] == "conflicting"
    # an official document that disagrees is never superseded
    events = [{"kind": "run_started", "target_market": "IL"},
              _ev("sheet-w", "curb_weight_kg", 1816, "official_importer", "importer_spec_sheet", "kg"),
              _ev("site-w", "curb_weight_kg", 1800, "official_importer", None, "kg")]
    assert evaluate_fields(specs, events, "IL")[0]["state"] == "conflicting"


# --- B3: equipment from spec sheets -------------------------------------------------------------------------------------

def test_b3_markers_state_equipment_and_an_icon_list_item_states_nothing():
    def values(text):
        return {c["field"]: c["value"] for c in harvest_text(text, SPECS)}
    assert values("Apple CarPlay ●")["apple_carplay"] is True
    assert values("חימום מושבים קדמיים ˆ")["heated_seats"] is True
    assert values("בקרת אקלים 3 אזורים ●")["climate_zones"] == 3
    assert values("גג שמש X")["sunroof_panoramic"] is False
    assert "sunroof_panoramic" not in values("חלון גג")                          # icons lost in text extraction
    d = dictionary_for(load_schema())
    assert line_marker(d, "חימום מושבים ●") is True and line_marker(d, "4X4 הנעה") is None


# --- C: the production source policy ------------------------------------------------------------------------------------

@pytest.mark.real_source_policy
def test_c_unlisted_and_blocked_domains_and_the_seed():
    from src.source_authority import dataset_policy, evidence_allowed, fetch_allowed, policy_of

    assert policy_of("https://www.cartube.co.il/x")["policy"] == "blocked"
    assert policy_of("https://press.bmwgroup.com/x")["policy"] == "blocked"
    assert policy_of("https://www.bmw.co.il/x")["policy"] == "blocked"            # blocked until reviewed
    assert policy_of("https://example.org/x") == {**policy_of("https://example.org/x"), "policy": "blocked",
                                                   "reason": "unlisted"}
    assert evidence_allowed("https://data.gov.il/dataset/x") is True
    assert fetch_allowed("https://vpic.nhtsa.dot.gov/api/x") and not evidence_allowed("https://vpic.nhtsa.dot.gov/x")
    assert dataset_policy("eea_co2_cars")["licence"] == "CC-BY-4.0"


@pytest.mark.real_source_policy
def test_c_a_blocked_search_result_is_dropped_before_any_fetch(cache):
    from src.tools import ToolConfig, ToolContext
    from src.tools.evidence import EvidenceStore
    from src.tools.search import policy_filter

    ctx = ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig())
    seen = []
    ctx.log = lambda kind, **data: seen.append((kind, data))
    kept = policy_filter(ctx, [{"url": "https://www.cartube.co.il/a"}, {"url": "https://data.gov.il/b"},
                               {"url": "https://unknown.example/c"}], query="m4")
    assert [r["url"] for r in kept] == ["https://data.gov.il/b"]
    assert ctx.counters["policy_blocked"] == 2 and ctx.counters["policy_blocked:cartube.co.il"] == 1
    assert seen[0][0] == "policy_blocked" and seen[0][1]["domains"] == {"cartube.co.il": 1, "unknown.example": 1}
    from src.tools.fetch import fetch_url

    refused = fetch_url(ctx, "https://www.cartube.co.il/images/mifrat/bmw/bmw-m4-mifrat-2023.pdf")
    assert refused["error"] == "policy_blocked" and ctx.counters["policy_blocked:cartube.co.il"] == 2


@pytest.mark.real_source_policy
def test_c_a_cached_document_of_a_blocked_domain_is_kept_but_never_evidence(cache):
    doc = put(cache, M.SHEET_2023_CARTUBE_URL, M.SHEET_2023_TEXT, "pdf")
    decision = admit(m4_adm(), cache, {"field": "curb_weight_kg", "value": 1816, "quote": 'משקל עצמי* ק"ג 1,816',
                                       "document_id": doc}, [doc])
    assert decision["accepted"] is False and decision["reasons"] == ["source_policy_blocked"]
    assert cache.get(doc) is not None                                              # kept on disk


@pytest.mark.real_source_policy
def test_c_an_operator_overlay_allows_a_domain_and_every_change_is_logged(tmp_path):
    from src import source_authority as sa

    sa.set_policy_overlay_dir(tmp_path / "derived")
    with pytest.raises(ValueError):
        sa.update_policy("bmw.co.il", "allowed", terms_clause="", checked_at="2026-10-06")
    change = sa.update_policy("bmw.co.il", "allowed", terms_clause="Use for comparison permitted.",
                              checked_at="2026-10-06", now="2026-10-06T04:00:00+00:00")
    assert change == {"at": "2026-10-06T04:00:00+00:00", "domain": "bmw.co.il", "from": "blocked", "to": "allowed",
                      "terms_clause": "Use for comparison permitted.", "checked_at": "2026-10-06",
                      "changed_by": "operator"}
    assert sa.policy_of("https://www.bmw.co.il/x")["policy"] == "allowed"
    assert sa.policy_of("https://www.toyota.co.il/x")["policy"] == "blocked"       # the rest of its entry is unchanged
    sa.update_policy("bmw.co.il", "blocked", terms_clause=None, checked_at=None)
    assert sa.policy_of("https://bmw.co.il/x")["policy"] == "blocked"
    assert [c["to"] for c in sa.policy_changes()] == ["allowed", "blocked"]
    table = sa.policy_table()
    assert any(e["id"] == "bmw.co.il" and e["overlay"] for e in table["entries"])
    with pytest.raises(ValueError):
        sa.update_policy("vpic.nhtsa.dot.gov", "identity_only", terms_clause="x", checked_at="2026-10-06")


@pytest.mark.real_source_policy
def test_c_an_identity_only_source_never_yields_a_value(cache):
    url = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/1G6A85SX*J?format=json&modelyear=2018"
    doc = cache.put("vpic", url, b'{"Results": [{"DisplacementL": "2.0"}]}',
                    {"status": 200, "final_url": url, "doc_type": "text"}, "Engine displacement 2.0 l, wheelbase 2910 mm")
    decision = admit(m4_adm(), cache, {"field": "wheelbase_mm", "value": 2910, "quote": "wheelbase 2910 mm",
                                       "document_id": doc["document_id"]}, [doc["document_id"]])
    assert decision["accepted"] is False and decision["reasons"] == ["source_policy_blocked"]


def test_c_exports_carry_the_attribution_of_every_licence_that_contributed_a_value():
    from src.final_assembly import assemble_output

    attribution = "Contains information licensed under the Open Government Licence – Canada."
    events = [{"kind": "run_started", "target_market": "IL"},
              {"kind": "evidence", "evidence": {"evidence_id": "w", "field": "wheelbase_mm", "value": 2910,
                                                "unit": "mm", "market": "IL", "variant_match": "exact",
                                                "binding_level": "exact_technical_variant",
                                                "attribution": attribution}},
              {"kind": "evidence", "evidence": {"evidence_id": "x", "field": "length_mm", "value": 4970, "unit": "mm",
                                                "market": "IL", "variant_match": "different",
                                                "binding_level": "body_powertrain",
                                                "attribution": "never: no value carried"}}]
    specs = resolve_requested_fields(["wheelbase_mm", "length_mm"])
    output, _ = assemble_output(events, payload("85095"), specs)
    assert output["provenance_summary"]["licence_attributions"] == [attribution]

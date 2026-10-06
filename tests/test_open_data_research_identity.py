"""The open-data identity drives the web research (V1-V4): the research identity of an exact open-data match (22010:
BMW 530e xDrive iPerformance 2020, EEA Va JP91 at co2 47), its identity searches on allowed domains only, page
verification by its keys (V2), corroboration of web values by its offers (V3) and the per-run report (V4). No network;
the EEA rows are verbatim from the 2020 shard (tests/fixtures/eea_type_code.py)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from fixtures import eea_type_code as T
from fixtures.corolla_harvest import put
from src.candidate_harvest import harvest_document
from src.db import build_level15_payload
from src.document_binding import apply_fingerprint, open_data_keys_verdict
from src.evidence_admission import AdmissionContext, admit
from src.fields import resolve_requested_fields
from src.gov_registry import identity_fingerprint
from src.open_data.identity import (apply_to_admission, identity_queries, identity_search_wave, research_identity,
                                    research_note, research_report)
from src.open_data.match import match
from src.open_data.offers import field_offers
from src.storage.cache import DocumentCache

ROOT = Path(__file__).resolve().parent.parent
SPECS = resolve_requested_fields(None, propulsion="plug_in")


def _payload() -> dict:
    rows = json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    return build_level15_payload(next(r for r in rows if r["upstream_record_id"] == "22010"))


def _result() -> dict:
    payload = _payload()
    return match(identity_fingerprint(payload), payload, rows_by_source={"eea_co2_cars": T.BMW_530E_2020})


def _adm(result: dict | None = None) -> AdmissionContext:
    payload = _payload()
    result = result or _result()
    adm = AdmissionContext.for_run(payload, None, SPECS, "IL")
    apply_fingerprint(adm.identity, identity_fingerprint(payload))
    apply_to_admission(adm, {"identity": research_identity(result), "offers": field_offers(result)})
    return adm


def _admit_page(tmp_path, text: str, adm=None, url: str = "https://example.com/530e") -> dict[str, dict]:
    cache = DocumentCache(tmp_path / "cache")
    adm = adm or _adm()
    doc = put(cache, url, text, "text")
    cands, _ = harvest_document(cache, doc, SPECS)
    out = {}
    for cand in cands:
        decision = admit(adm, cache, {"field": cand["field"], "value": cand["value"], "quote": cand["quote"],
                                      "document_id": doc}, [doc])
        if decision["accepted"] and cand["field"] not in out:
            out[cand["field"]] = decision["record"]
    return out


class _Ctx:
    def __init__(self):
        self.counters, self.events, self.offered = Counter(), [], []
        self.admission = _adm()

    def emit(self, kind, **data):
        self.events.append((kind, data))


class _Log:
    def __init__(self):
        self.events: list[dict] = []

    def event(self, kind, **data):
        self.events.append({"kind": kind, **data})


# --- the research identity ------------------------------------------------------------------------------------------------

def test_the_22010_research_identity_is_the_jp91_configuration():
    identity = research_identity(_result())
    assert identity["level"] == "exact_technical_variant" and identity["source"] == "eea_co2_cars"
    assert (identity["model"], identity["eea_type_code"], identity["power_kw"], identity["co2_wltp"],
            identity["displacement_cc"]) == ("530e xDrive iPerformance", "JP91", 135, 47, 1998)
    assert identity["terms"] == ["530e xDrive iPerformance", "JP91", "135 kW"]
    assert {"JA91", "JP92"} <= set(identity["sibling_type_codes"])
    with_t = research_identity({**_result(), "sources": {"eea_co2_cars": {
        **_result()["sources"]["eea_co2_cars"],
        "survivors": [{**r, "type_approval": "e1*2007/46*1234*15"} for r in _result()["sources"]["eea_co2_cars"][
            "survivors"]]}}})
    assert with_t["type_approvals"] == ["e1*2007/46*1234*15"] and with_t["terms"][-1] == "e1*2007/46*1234*15"
    assert research_identity({**_result(), "level": "body_powertrain"}) is None


def test_run_open_data_records_the_identity_on_its_event_and_the_admission_context():
    from src.open_data.engine import run_open_data

    ctx, log = _Ctx(), _Log()
    ctx.admission.identity.open_data_keys = {}
    payload = _payload()
    out = run_open_data(ctx, log, payload, identity_fingerprint(payload), "shadow",
                        rows_by_source={"eea_co2_cars": T.BMW_530E_2020})
    event = next(e for e in log.events if e["kind"] == "open_data_match")
    assert event["identity"]["eea_type_code"] == "JP91" and out["identity"] == event["identity"]
    assert ctx.admission.identity.open_data_keys["power_kw"] == 135
    assert ctx.admission.open_data_offers["wheelbase_mm"][0]["value"] == 2975
    replayed = AdmissionContext.for_run(payload, None, SPECS, "IL")      # binding replay reads the same event
    apply_to_admission(replayed, event)
    assert replayed.identity.open_data_keys == ctx.admission.identity.open_data_keys
    assert replayed.open_data_offers == ctx.admission.open_data_offers


# --- V1: search terms -----------------------------------------------------------------------------------------------------

def test_unit_replay_the_22010_identity_queries_name_the_designation_and_jp91_on_allowed_domains_only():
    identity = research_identity(_result())
    queries = identity_queries(identity, ["bmw.co.il"])
    assert queries == [("bmw.co.il", "site:bmw.co.il 530e xDrive iPerformance JP91"),
                       ("bmw.co.il", "site:bmw.co.il 530e xDrive iPerformance 135 kW")]
    assert all("530e xDrive iPerformance" in q for _, q in queries)
    assert identity_queries(identity, []) == []


@pytest.mark.real_source_policy
def test_the_identity_wave_searches_nothing_while_the_policy_allows_no_brand_domain():
    ctx, log = _Ctx(), _Log()
    out = identity_search_wave(ctx, log, identity=research_identity(_result()), manufacturer="ב מ וו",
                               search=lambda q, d: pytest.fail("searched"))
    assert out["searches"] == 0 and out["skipped"].startswith("no_allowed_domain")
    assert "bmw.co.il" in out["blocked_domains"] and "bmw.com" in out["blocked_domains"]
    assert log.events[0]["kind"] == "open_data_identity_search"
    assert "allows none of the brand" in research_note(research_identity(_result()), out)


@pytest.mark.real_source_policy
def test_the_identity_wave_searches_only_the_allowed_importer_domain(tmp_path):
    from src import source_authority

    source_authority.set_policy_overlay_dir(tmp_path / "derived")
    source_authority.update_policy("bmw.co.il", "allowed", terms_clause="test: reviewed terms",
                                   checked_at="2026-10-06")
    searched: list[tuple[str, str]] = []

    def search(query, domain):
        searched.append((query, domain))
        return {"results": [{"url": "https://www.bmw.co.il/5-series/530e.html", "title": "BMW 530e"}]}
    ctx, log = _Ctx(), _Log()
    try:
        out = identity_search_wave(ctx, log, identity=research_identity(_result()), manufacturer="ב מ וו",
                                   search=search)
    finally:
        source_authority.set_policy_overlay_dir(None)
    assert out["allowed_domains"] == ["bmw.co.il"] and out["searches"] == 2
    assert [q for q, _ in searched] == ["site:bmw.co.il 530e xDrive iPerformance JP91",
                                        "site:bmw.co.il 530e xDrive iPerformance 135 kW"]
    assert all("site:bmw.co.il" in q for q, _ in searched)               # never bmw.com / bmwgroup.com (blocked)
    assert ctx.counters["open_data_identity_searches"] == 2
    note = research_note(research_identity(_result()), out)
    assert '"530e xDrive iPerformance"' in note and '"JP91"' in note and "https://www.bmw.co.il/5-series" in note


def test_the_resolver_and_the_reacquire_packet_carry_the_identity():
    from src.agent import reacquire_packet
    from src.il_version_pages import identity_site_queries

    identity = research_identity(_result())
    queries = identity_site_queries({"od_model": identity["model"], "make_en": "bmw", "year": "2020"})
    assert queries and all("530e xDrive iPerformance" in q for _, q in queries)
    assert identity_site_queries({"make_en": "bmw"}) == []
    packet = reacquire_packet(cluster="dimensions", fields=["wheelbase_mm"], specs=SPECS, evaluation={},
                              identity={}, target_market="IL", source_type="official", site_urls=[], fetched_urls=[],
                              negative={}, search_budget=2, fetch_budget=2, turns=2, open_data_identity=identity)
    assert packet["open_data_identity"]["search_terms"] == ["530e xDrive iPerformance", "JP91", "135 kW"]


def test_the_spec_sheet_search_runs_the_identity_query_first(tmp_path):
    from src.il_version_pages import resolve_spec_sheets

    ctx = _Ctx()
    ctx.cache, ctx.vehicle = DocumentCache(tmp_path / "cache"), {"manufacturer": "ב מ וו"}
    searched = []
    out = resolve_spec_sheets(ctx, None, payload=_payload(), search=lambda q, d: searched.append(q) or {"results": []},
                              fetch=lambda u: pytest.fail("fetched"))
    assert searched[0] == "site:bmw.co.il 530e xDrive iPerformance JP91" and out["searches"] == 2


# --- V2: page verification by identity keys -------------------------------------------------------------------------------

PAGE_KEYS = ("BMW 530e xDrive iPerformance Sedan 2020\nTechnical data\nEngine: 4-cylinder petrol, 1998 cm³, 135 kW\n"
             "CO2 emissions combined (WLTP): 47 g/km\nWheelbase: 2975 mm\nTop speed: 235 km/h\nLength: 4936 mm\n")
PAGE_185 = ("BMW 530e xDrive iPerformance Sedan 2020\nTechnical data\nSystem output: 185 kW\n"
            "CO2 emissions combined (WLTP): 47 g/km\nWheelbase: 2975 mm\nTop speed: 235 km/h\n")
PAGE_CODE = "BMW 530e xDrive iPerformance Sedan 2020 (JP91)\nTop speed: 235 km/h\nLength: 4936 mm\n"
# the government type code JP91 is also the target's model code: the existing binding already names it


def test_the_verdict_reads_type_code_kw_co2_and_displacement():
    adm = _adm()
    assert open_data_keys_verdict(PAGE_KEYS, adm.identity) == {
        "status": "target", "basis": "power_co2_displacement", "power_kw": 135, "co2_wltp": 47,
        "displacement_cc": 1998}
    assert open_data_keys_verdict(PAGE_CODE, adm.identity)["basis"] == "type_code"
    assert open_data_keys_verdict(PAGE_185, adm.identity)["contradicts"] == "power_kw"
    assert open_data_keys_verdict("BMW 530e iPerformance JA91 sedan", adm.identity)["contradicts"] == "type_code"
    charging = "BMW 530e xDrive iPerformance 2020: charging power 3.7 kW, 47 g/km"
    assert open_data_keys_verdict(charging, adm.identity) is None           # a charging kW never contradicts
    assert open_data_keys_verdict("135 kW and 185 kW", adm.identity) is None
    adm.identity.open_data_keys = {}
    assert open_data_keys_verdict(PAGE_KEYS, adm.identity) is None


def test_a_page_stating_135_kw_47_g_km_and_1998_cm3_binds_exact(tmp_path):
    records = _admit_page(tmp_path, PAGE_KEYS)
    for field in ("wheelbase_mm", "top_speed_kmh", "length_mm"):
        record = records[field]
        assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "exact", field
        assert record["open_data_keys"]["status"] == "target" and not record.get("binding_veto")


def test_a_page_stating_the_type_approval_number_binds_exact_by_the_open_data_keys(tmp_path):
    """The deciding case: a page naming the model, the year and the EU type-approval number (T) but no power or
    displacement is the configuration only through the open-data keys."""
    page = "BMW 530e xDrive iPerformance Sedan 2020\nEC type approval e1*2007/46*1234*15\nTop speed: 235 km/h\n"
    adm = _adm()
    adm.identity.open_data_keys = {**adm.identity.open_data_keys, "type_approvals": ["e1*2007/46*1234*15"]}
    record = _admit_page(tmp_path, page, adm=adm)["top_speed_kmh"]
    assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "exact"
    assert record["binding_basis"] == "open_data_identity_keys"
    assert record["open_data_keys"] == {"status": "target", "basis": "type_approval",
                                        "type_approval": "e1*2007/46*1234*15"}
    without = _adm()
    without.identity.open_data_keys, without.open_data_offers = {}, {}
    plain = _admit_page(tmp_path / "w", page, adm=without)["top_speed_kmh"]
    assert plain["binding_level"] == "body_powertrain"                      # the same page without V2


def test_a_page_stating_185_kw_of_the_same_model_is_a_veto(tmp_path):
    records = _admit_page(tmp_path, PAGE_185)
    for record in records.values():
        assert record["variant_match"] == "different" and "open_data_keys_mismatch@document" in record["binding_veto"]
        assert record["open_data_keys"]["status"] == "other_variant"
        assert "open_data_corroboration" not in record                       # a vetoed value is never corroborated


# --- V3: corroboration ----------------------------------------------------------------------------------------------------

def test_a_web_wheelbase_2975_equal_to_the_eea_offer_is_open_data_corroborated(tmp_path):
    page = "BMW 530e xDrive iPerformance Sedan 2020\nPlug-in hybrid sedan\nWheelbase: 2975 mm\nTop speed: 235 km/h\n"
    records = _admit_page(tmp_path, page)
    wheelbase = records["wheelbase_mm"]
    assert wheelbase["binding_level"] == "exact_technical_variant" and wheelbase["variant_match"] == "exact"
    assert wheelbase["binding_basis"] == "open_data_corroborated"
    offer = wheelbase["open_data_corroboration"]["offer"]
    assert offer["source"] == "eea_co2_cars" and offer["value"] == 2975 and offer["identified_by"] == "all_survivors_agree"
    assert wheelbase["open_data_corroboration"]["web_value"] == 2975 and wheelbase["source_url"].startswith("https://")
    assert wheelbase["without_open_data"]["binding_level"] == "body_powertrain"
    assert records["top_speed_kmh"]["binding_level"] == "body_powertrain"     # no open-data offer for it
    other = _admit_page(tmp_path / "o", page.replace("2975", "2980"))["wheelbase_mm"]
    assert "open_data_corroboration" not in other and other["binding_level"] == "body_powertrain"


def test_d1_rounding_a_converted_value_corroborates_within_its_stated_precision():
    from src.open_data.identity import matching_offer

    adm = _adm()
    spec = adm.spec("wheelbase_mm")
    assert matching_offer(adm, "wheelbase_mm", spec, 2975, None)["value"] == 2975
    assert matching_offer(adm, "wheelbase_mm", spec, 2974, None) is None      # two numbers in mm are two values
    # 117.1 in = 2974.3 mm: the inch's last digit (0.1 in = 2.54 mm) covers the EEA 2975
    stated = {"raw": "117.1", "operation": "multiply_by_25_4"}
    assert matching_offer(adm, "wheelbase_mm", spec, 2974, stated) is not None
    assert matching_offer(adm, "wheelbase_mm", spec, 2940, {"raw": "115.7", "operation": "multiply_by_25_4"}) is None


# --- V4: the per-run report -----------------------------------------------------------------------------------------------

def test_the_run_report_counts_queries_pages_values_and_the_fields_gained():
    identity = research_identity(_result())
    specs = [s for s in SPECS if s["name"] in ("wheelbase_mm", "top_speed_kmh")]

    def evidence(eid, field, value, doc, **extra):
        item = {"evidence_id": eid, "field": field, "value": value, "document_id": doc, "admission_status": "accepted",
                "binding_level": "exact_technical_variant", "variant_match": "exact", "binding_requirement":
                "exact_technical_variant", "market": "IL", "source_authority": "official_importer",
                "typed_value": {"type": "scalar", "value": value}, "unit": "mm", **extra}
        return {"kind": "evidence", "evidence": item}
    events = [{"kind": "run_started", "requested_field_specs": specs, "target_market": "IL"},
              {"kind": "open_data_match", "identity": identity},
              {"kind": "search", "query": "site:bmw.co.il 530e xDrive iPerformance JP91"},
              {"kind": "search", "query": "BMW 530e 2020 מפרט"},
              evidence("e1", "wheelbase_mm", 2975, "d1", open_data_corroboration={"offer": {"source": "eea_co2_cars"}},
                       without_open_data={"binding_level": "body_powertrain", "variant_match": "unclear",
                                          "binding_basis": None}),
              evidence("e2", "top_speed_kmh", 235, "d2", open_data_keys={"status": "target", "basis": "type_code"})]
    report = research_report(events, specs, "IL")
    assert report["queries_with_open_data_identity"] == 1
    assert report["pages_verified_by_open_data_keys"] == 1 and report["verified_documents"] == ["d2"]
    assert report["values_corroborated_by_open_data"] == 1
    assert report["fields_gained"] == ["wheelbase_mm"] and report["fields_lost"] == []

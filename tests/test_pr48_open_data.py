"""Identity anchors PR, Part D: the open structured data layer (src/open_data). No network: the snapshot rows are the
fixtures of tests/fixtures/pr48_open_data.py (SYNTHETIC rows written from the CTS 2018 benchmark's stated values; the
dataset hosts were not reachable from the build host), the builders run on recorded CSV / JSON bodies."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from fixtures import pr48_open_data as F
from src.db import build_level15_payload
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.gov_registry import identity_fingerprint
from src.open_data import datasets as ds
from src.open_data.engine import run_open_data
from src.open_data.match import match, transmission_of
from src.open_data.offers import field_offers
from src.storage.cache import DocumentCache
from src.tools.evidence import EvidenceStore

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = {r["upstream_record_id"]: r for r in
            json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]}


def payload(record: str) -> dict:
    return build_level15_payload(SNAPSHOT[record])


def run_match(record: str, rows: dict) -> dict:
    p = payload(record)
    result = match(identity_fingerprint(p), p, rows_by_source=rows)
    return {**result, "offers": field_offers(result)}


def offer(result: dict, field: str, source: str) -> dict:
    return next(o for o in result["offers"] if o["field"] == field and o["source"] == source)


# --- the CTS 2018 benchmark (record 85095) ------------------------------------------------------------------------------

def test_the_route_is_chosen_by_the_approval_type():
    assert run_match("85095", F.CTS_ROWS)["route"] == "european"           # sug_tkina אירופאית, not "EPA first"
    assert run_match("85410", {})["route"] == "american"                   # Escalade: אמריקאית


def test_eea_keeps_the_203_kw_rows_and_vetoes_172_and_477():
    eea = run_match("85095", F.CTS_ROWS)["sources"]["eea_co2_cars"]
    assert eea["status"] == "ambiguous" and eea["source_ids"] == ["eea-2018-cts-a", "eea-2019-cts-b"]
    vetoed = {v["row_id"]: v["vetoes"] for v in eea["vetoed"]}
    assert "power" in vetoed["eea-2018-cts-172"] and {"displacement", "power"} <= set(vetoed["eea-2018-cts-v"])
    assert eea["power_definition"] == "hp_from_kw"                         # 203 kW = 272.2 mechanical hp


def test_epa_nrcan_and_cvs_each_isolate_the_2_0_rwd():
    sources = run_match("85095", F.CTS_ROWS)["sources"]
    assert (sources["epa_fueleconomy"]["status"], sources["epa_fueleconomy"]["source_ids"]) == ("unique", ["38704"])
    assert (sources["nrcan_fuel_ratings"]["status"], sources["nrcan_fuel_ratings"]["source_ids"]) == \
        ("unique", ["nrcan-3457"])
    assert sources["tc_cvs"]["source_ids"] == ["cvs-153"]
    assert {"displacement", "drive", "transmission"} <= set(sources["epa_fueleconomy"]["keys_matched"])
    assert transmission_of("Automatic (S8)") == {"class": "automatic", "gears": 8, "gearbox_type": "automatic"}
    assert transmission_of("AS8")["gears"] == 8 and transmission_of("Automatic (AM-S7)")["gearbox_type"] is None


def test_the_european_cts_without_co2_stays_at_body_powertrain_and_its_offers_are_recorded():
    result = run_match("85095", F.CTS_ROWS)
    assert result["level"] == "body_powertrain"                 # no co2 key, no ADEME survivor: never exact
    wheelbase = offer(result, "wheelbase_mm", "eea_co2_cars")
    assert wheelbase["value"] == 2910 and wheelbase["identified_by"] == "all_survivors_agree"
    weight = offer(result, "curb_weight_kg", "eea_co2_cars")
    assert (weight["value"], weight["definition"], weight["routes"]) == (1734, "eu_running_order", ["european"])
    assert weight["status"] == "below_level"
    cvs = offer(result, "curb_weight_kg", "tc_cvs")
    assert (cvs["value"], cvs["definition"], cvs["status"]) == (1642, "na_curb", "alternative_definition")
    for field, source in (("cargo_volume_l", "epa_fueleconomy"), ("fuel_consumption_combined_l_100km",
                                                                   "epa_fueleconomy"),
                          ("fuel_consumption_combined_l_100km", "nrcan_fuel_ratings")):
        assert offer(result, field, source)["status"] == "reference_only", (field, source)


def test_cvs_dimensions_need_a_second_source_on_the_wheelbase():
    p = payload("85410")
    p["identity"] = {**p["identity"], "commercial_name": "CTS", "year": 2018}
    p["engine_drivetrain"] = {**p["engine_drivetrain"], "engine_cc": 1998, "power_hp": 272,
                              "drivetrain_normalized": "two_wheel_drive"}
    p["structure"] = {**p["structure"], "body_normalized": "sedan", "gross_weight_kg": 2150}
    fp = identity_fingerprint(p)
    rows = {"epa_fueleconomy": F.CTS_EPA, "nrcan_fuel_ratings": F.CTS_NRCAN, "tc_cvs": F.CTS_CVS}
    result = match(fp, p, rows_by_source=rows)
    assert result["route"] == "american" and result["level"] == "exact_technical_variant"
    offers = {(o["field"], o["source"]): o for o in field_offers(result)}
    assert offers[("length_mm", "tc_cvs")]["status"] == "uncorroborated"       # only CVS states a wheelbase here
    with_eea = match(fp, p, rows_by_source={**rows, "eea_co2_cars": F.CTS_EEA})
    offers = {(o["field"], o["source"]): o for o in field_offers(with_eea)}
    assert offers[("length_mm", "tc_cvs")]["value"] == 4970 and offers[("length_mm", "tc_cvs")]["status"] == "offered"
    assert offers[("curb_weight_kg", "tc_cvs")]["status"] == "offered"           # na_curb is the american definition


# --- M4 (record 23678) and a BEV (record 29053) -------------------------------------------------------------------------

def test_m4_co2_key_selects_the_31az_configuration_and_vetoes_21hk():
    """Without the type code (K1) the co2 key selects 31AZ and vetoes 21HK."""
    p = payload("23678")
    p["identity"] = {**p["identity"], "model_code": None}
    p["raw_row"] = {k: v for k, v in (p.get("raw_row") or {}).items() if k != "degem_nm"}
    fp = {**identity_fingerprint(p), "type_code": None}
    result = match(fp, p, rows_by_source={"eea_co2_cars": F.M4_EEA})
    result = {**result, "offers": field_offers(result)}
    eea = result["sources"]["eea_co2_cars"]
    assert "type_code" not in eea
    assert eea["status"] == "unique" and eea["source_ids"] == ["eea-2024-m4-31az"]
    assert eea["vetoed"][0]["row_id"] == "eea-2024-m4-21hk" and eea["vetoed"][0]["vetoes"] == ["co2_wltp"]
    assert result["level"] == "exact_technical_variant" and result["route_detail"] == "european_co2"
    assert offer(result, "fuel_consumption_combined_l_100km", "eea_co2_cars")["status"] == "offered"


def test_m4_type_code_31az_names_the_configuration_before_any_other_key():
    """K1: the government type code 31AZ is EEA Va 31AZ (BMW exact_va); 21HK is never a candidate."""
    result = run_match("23678", {"eea_co2_cars": F.M4_EEA})
    eea = result["sources"]["eea_co2_cars"]
    assert result["keys"]["type_code"] == "31AZ"
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "exact_va"
    assert eea["candidates"] == 1 and eea["source_ids"] == ["eea-2024-m4-31az"] and not eea["vetoed"]
    assert "type_code_match" in eea["keys_matched"] and "co2_wltp" in eea["keys_matched"]
    assert result["level"] == "exact_technical_variant" and result["level_basis"] == "european_type_code"
    assert offer(result, "fuel_consumption_combined_l_100km", "eea_co2_cars")["status"] == "offered"


def test_bev_version_tokens_and_kw_select_one_configuration_and_another_battery_is_vetoed():
    result = run_match("29053", {"eea_co2_cars": F.KONA_EEA, "ademe_car_labelling": F.KONA_ADEME})
    ademe = result["sources"]["ademe_car_labelling"]
    assert ademe["status"] == "unique" and ademe["source_ids"] == ["ademe-kona-64"]
    vetoed = {v["row_id"]: v["vetoes"] for v in ademe["vetoed"]}
    assert "battery" in vetoed["ademe-kona-65"] and "power" in vetoed["ademe-kona-48"]
    assert result["level"] == "exact_technical_variant" and result["lead_source"] == "ademe_car_labelling"
    without = run_match("29053", {"eea_co2_cars": F.KONA_EEA})
    assert without["level"] == "body_powertrain"                 # exact only with one survivor in BOTH sources


# --- modes -----------------------------------------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, tmp_path, record: str):
        self.cache = DocumentCache(tmp_path / "cache")
        self.evidence = EvidenceStore()
        self.counters: Counter = Counter()
        self.events: list = []
        p = payload(record)
        self.admission = AdmissionContext.for_run(p, None, resolve_requested_fields(None, propulsion="conventional"),
                                                  "IL")

    def emit(self, kind, **data):
        self.events.append({"kind": kind, **data})

    def note_document(self, *a, **k):
        pass


class _Log:
    def __init__(self):
        self.events: list = []

    def event(self, kind, **data):
        self.events.append({"kind": kind, **data})


def test_shadow_mode_records_the_match_and_admits_nothing(tmp_path):
    ctx, log = _Ctx(tmp_path, "23678"), _Log()
    p = payload("23678")
    out = run_open_data(ctx, log, p, identity_fingerprint(p), "shadow", rows_by_source={"eea_co2_cars": F.M4_EEA})
    assert out["mode"] == "shadow" and out["level"] == "exact_technical_variant" and out["admitted"] == []
    assert [e["kind"] for e in log.events] == ["open_data_match"] and ctx.events == []
    assert {o["field"] for o in out["offers"] if o["status"] == "offered"} == {
        "wheelbase_mm", "curb_weight_kg", "fuel_consumption_combined_l_100km"}
    assert run_open_data(ctx, log, p, identity_fingerprint(p), "off") == {"version": out["version"], "mode": "off"}


def test_admit_mode_admits_only_the_allowlisted_triples_and_they_port_through_the_ladder(tmp_path, monkeypatch):
    from src.field_recovery import evaluate_fields
    from src.final_assembly import assemble_output

    allow = tmp_path / "admission.json"
    allow.write_text(json.dumps({"triples": [{"source": "eea_co2_cars", "field": "wheelbase_mm",
                                              "route": "european"}]}), "utf-8")
    monkeypatch.setattr(ds, "ADMISSION_PATH", allow)
    ctx, log = _Ctx(tmp_path, "23678"), _Log()
    p = payload("23678")
    out = run_open_data(ctx, log, p, identity_fingerprint(p), "admit", rows_by_source={"eea_co2_cars": F.M4_EEA})
    assert [(a["field"], a["value"]) for a in out["admitted"]] == [("wheelbase_mm", 2857)]
    item = ctx.evidence.items[0]
    assert (item["source_authority"], item["market"], item["binding_level"], item["variant_match"]) == \
        ("open_dataset", "EU", "exact_technical_variant", "exact")
    assert item["licence"] == "CC-BY-4.0" and item["attribution"].startswith("European Environment Agency / DG CLIMA")
    specs = resolve_requested_fields(["wheelbase_mm", "curb_weight_kg"])
    events = [{"kind": "run_started", "target_market": "IL"}] + [e for e in ctx.events if e["kind"] == "evidence"]
    states = {e["field"]: e for e in evaluate_fields(specs, events, "IL")}
    assert states["wheelbase_mm"]["state"] == "ok" and "portable_foreign_fact" in states["wheelbase_mm"]["info"]
    assert states["curb_weight_kg"]["state"] == "missing"          # offered, but its triple is not allowlisted
    output, _ = assemble_output(events, p, specs)
    assert output["fields"]["wheelbase_mm"]["value"] == 2857
    assert output["fields"]["wheelbase_mm"]["provenance"] == "foreign_direct"
    assert output["provenance_summary"]["licence_attributions"] == [item["attribution"]]


def test_an_identity_only_dataset_never_becomes_evidence(tmp_path):
    from src.open_data.engine import admit_offer

    ctx = _Ctx(tmp_path, "23678")
    fake = {"field": "wheelbase_mm", "source": "nhtsa_vpic", "column": "x", "value": 2857,
            "raw": {"value": 2857, "unit": "mm"}, "row_ids": ["v"], "portability_scope": "exact_technical_variant"}
    assert admit_offer(ctx, fake, {"route": "american", "level": "exact_technical_variant"}) is None


def test_the_designation_is_a_search_term_and_resolved_fields_leave_the_research_plan():
    from src.open_data.engine import research_note

    state = {"designation": "CTS 2.0 Turbo RWD", "route": "european",
             "admitted": [{"field": "wheelbase_mm"}, {"field": "curb_weight_kg"}]}
    note, skipped = research_note(state, {"wheelbase_mm"})
    assert "CTS 2.0 Turbo RWD" in note and skipped == ["wheelbase_mm"]


# --- snapshots and builders ----------------------------------------------------------------------------------------------

def test_a_snapshot_round_trips_and_the_engine_reads_only_it(tmp_path):
    folder = tmp_path / "open"
    ds.write_snapshot("eea_co2_cars", F.CTS_EEA, {"built_at": "2026-10-06T03:00:00+00:00"}, folder)
    rows = ds.query_rows("eea_co2_cars", makes=["cadillac"], years=[2018, 2019], folder=folder)
    assert sorted(r["row_id"] for r in rows) == sorted(r["row_id"] for r in F.CTS_EEA)
    assert ds.snapshot_meta("eea_co2_cars", folder)["rows"] == 4
    p = payload("85095")
    result = match(identity_fingerprint(p), p, folder=folder)
    assert result["sources"]["eea_co2_cars"]["status"] == "ambiguous"
    assert result["sources"]["epa_fueleconomy"]["status"] == "no_snapshot"


# --- vPIC ------------------------------------------------------------------------------------------------------------------

def test_vpic_builds_a_partial_vin_only_for_a_vds_shaped_type_code():
    from src.open_data.vpic import partial_vin

    p = payload("85410")                                         # Escalade S9GRL, 2025
    assert partial_vin(identity_fingerprint(p), p) == ("1G6S9GRL*S", "ok")
    p = payload("23678")
    assert partial_vin(identity_fingerprint(p), p)[0] is None     # 31AZ: not a 5-character descriptor


def test_vpic_vetoes_by_its_identity_keys_and_never_yields_a_value(tmp_path):
    from src.open_data.vpic import vpic_check

    class Resp:
        status_code = 200
        text = json.dumps({"Results": [{"Model": "Escalade", "DisplacementL": "6.2", "DriveType": "4WD/4-Wheel Drive"}]})

    class Session:
        def get(self, url, timeout=None):
            return Resp()

    ctx = _Ctx(tmp_path, "85410")
    from src.tools import ToolConfig

    ctx.session, ctx.config = Session(), ToolConfig()
    p = payload("85410")
    result = {"sources": {"epa_fueleconomy": {"status": "ambiguous"}},
              "keys": {"cc": 6162, "drivetrain": "awd", "models": ["ESCALADE"]}}
    out = vpic_check(ctx, identity_fingerprint(p), p, result)
    assert out["status"] == "decoded" and out["vetoes"] == [] and out["identity"]["displacement_l"] == "6.2"
    result["keys"]["cc"] = 3600
    assert vpic_check(ctx, identity_fingerprint(p), p, result)["vetoes"] == ["displacement"]
    assert vpic_check(ctx, identity_fingerprint(p), p, {"sources": {}, "keys": {}})["status"] == "not_run"


# --- diagnostics -----------------------------------------------------------------------------------------------------------

def test_diagnostics_row_carries_the_open_data_and_policy_columns():
    from src.diagnostics import anchor_columns, identity_anchors_summary

    events = [{"kind": "run_started", "requested_field_specs": []},
              {"kind": "identity_fingerprint", "approval_route": {"route": "european"}, "code_family": [27, 44],
               "equivalent_codes": [27]},
              {"kind": "open_data_match", "mode": "shadow", "route": "european", "level": "body_powertrain",
               "sources": {"eea_co2_cars": {"status": "ambiguous"}, "epa_fueleconomy": {"status": "unique"}},
               "offers": [{"status": "offered"}, {"status": "reference_only"}], "admitted": []},
              {"kind": "policy_blocked", "stage": "search", "domains": {"cartube.co.il": 3}},
              {"kind": "policy_blocked", "stage": "fetch", "domain": "cartube.co.il"},
              {"kind": "document", "document": {"document_id": "d1", "url": "https://data.gov.il/x"}}]
    summary = identity_anchors_summary(events)
    assert summary["open_data_route"] == "european" and summary["code_family_size"] == 2
    assert summary["policy_blocked"] == {"cartube.co.il": 4} and summary["open_data_offers"] == 1
    row = anchor_columns(summary)
    assert row["open_data_match_status"] == "eea_co2_cars:ambiguous, epa_fueleconomy:unique"
    assert row["policy_blocked"] == 4 and row["fields_skipped_open_data"] == 0


# --- the Data page API -----------------------------------------------------------------------------------------------------

from test_api import ACCESS, client, ctx, data_root, gate, make_context  # noqa: E402,F401


@pytest.mark.real_source_policy
def test_the_data_api_lists_datasets_and_switches_a_domain_with_a_logged_overlay(client, ctx):
    from src import source_authority

    source_authority.set_policy_overlay_dir(ctx.manager.paths.data_dir / "derived")
    ds.set_repo_dir(ctx.manager.paths.data_dir / "no-open-data")     # not the committed data/open/ snapshots
    datasets = client.get("/api/data/datasets").json()
    ds.set_repo_dir(None)
    names = {d["dataset"] for d in datasets["datasets"]}
    assert {"eea_co2_cars", "ademe_car_labelling", "epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs",
            "nhtsa_vpic"} == names
    eea = next(d for d in datasets["datasets"] if d["dataset"] == "eea_co2_cars")
    assert eea["licence"] == "CC-BY-4.0" and eea["policy"] == "allowed" and eea["built_at"] is None
    assert eea["available"] is False and eea["problem"] == "no_manifest_entry"
    table = client.get("/api/data/policy").json()
    assert table["unlisted"] == "blocked" and any(e["id"] == "cartube.co.il" for e in table["entries"])
    refused = client.post("/api/data/policy", json={"domain": "bmw.co.il", "policy": "allowed"})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "policy_change_refused"
    ok = client.post("/api/data/policy", json={"domain": "bmw.co.il", "policy": "allowed",
                                               "terms_clause": "Comparison use permitted.", "checked_at": "2026-10-06"})
    assert ok.status_code == 200 and ok.json()["change"]["to"] == "allowed"
    assert source_authority.policy_of("https://www.bmw.co.il/x")["policy"] == "allowed"
    assert client.get("/api/data/policy").json()["changes"][-1]["domain"] == "bmw.co.il"
    assert client.post("/api/data/datasets/rebuild").status_code in (404, 405)       # nothing is built on the server

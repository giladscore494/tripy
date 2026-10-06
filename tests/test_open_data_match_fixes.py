"""Match fixes from run 20261006T184604Z (record 22010) and the shadow report on the server: M1 the drive in the
source's own model text and rows superseded by an exact key, M2 null-tolerant agreement (n_null), M3 the narrowest
exact subset for measured values, M4 the ADEME body / power vetoes and catalogue period, R the in-process shadow report
(API, MCP). No network; the EEA rows are verbatim from the 2020 shard (tests/fixtures/eea_type_code.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures import eea_type_code as T
from src.db import build_level15_payload
from src.gov_registry import identity_fingerprint
from src.open_data import datasets as ds
from src.open_data.match import drive_from_model_text, match, match_source, target_keys
from src.open_data.offers import field_offers

ROOT = Path(__file__).resolve().parent.parent


def _row(record: str) -> dict:
    rows = json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    rows += json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    return next(r for r in rows if r["upstream_record_id"] == record)


def _keys(record: str = "22010", **override) -> dict:
    payload = build_level15_payload(_row(record))
    return {**target_keys(identity_fingerprint(payload), payload), **override}


JP91_47 = [r for r in T.BMW_530E_2020 if r["variant"] == "JP91" and r.get("co2_wltp") == 47 and r["power_kw"] == 135]


# --- M1 -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("make, text, drive", [
    ("BMW", "530e xDrive iPerformance", "awd"), ("BMW", "X3 xDrive30e", "awd"), ("BMW", "520d sDrive", "two_wheel_drive"),
    ("MERCEDES-BENZ", "C 300 4MATIC+", "awd"), ("AUDI", "A6 Avant 40 TDI quattro", "awd"), ("SKODA", "OCTAVIA 4X4", "awd"),
    ("BMW", "530e iPerformance", None), ("TOYOTA", "COROLLA AWD-i", "awd"), ("AUDI", "A4 2.0 TDI", None),
])
def test_the_drive_named_in_the_model_text(make, text, drive):
    config = ds.config()
    assert drive_from_model_text({"make": make, "model": text}, "eea_co2_cars", config) == drive
    assert drive_from_model_text({"make": make, "model": text}, "epa_fueleconomy", config) is None    # not listed


def test_an_awd_text_row_is_vetoed_for_a_two_wheel_drive_target_and_the_reverse():
    rwd = _keys(type_code=None, drivetrain="two_wheel_drive")
    xdrive = {**JP91_47[0], "variant": "OTHER"}                       # no type code: the ordinary keys
    out = match_source("eea_co2_cars", rwd, rows=[xdrive])
    assert out["status"] == "none" and out["vetoed"][0]["vetoes"] == ["drive"]
    awd = _keys(type_code=None, drivetrain="awd")
    sdrive = {**xdrive, "model": "530e sDrive iPerformance"}
    assert match_source("eea_co2_cars", awd, rows=[sdrive])["vetoed"][0]["vetoes"] == ["drive"]
    assert match_source("eea_co2_cars", awd, rows=[xdrive])["status"] == "unique"


def test_rows_without_the_co2_key_drop_when_co2_exact_rows_exist():
    keys = _keys(type_code=None)
    exact = {**JP91_47[0], "variant": "OTHER"}
    no_co2 = {k: v for k, v in exact.items() if k != "co2_wltp"} | {"row_id": "no-co2", "version": "DAW5XXXX"}
    out = match_source("eea_co2_cars", keys, rows=[exact, no_co2])
    assert [r["row_id"] for r in out["survivors"]] == [exact["row_id"]] and out["status"] == "unique"
    assert out["superseded_by_exact_key"] == {"key": "co2_wltp", "rows": 1, "row_ids": ["no-co2"]}
    assert out["exact_subset"]["basis"] == "co2"
    alone = match_source("eea_co2_cars", keys, rows=[no_co2])      # without an exact row it still survives
    assert alone["status"] == "unique" and "superseded_by_exact_key" not in alone


# --- M2 / M3 --------------------------------------------------------------------------------------------------------------

def test_a_single_value_with_nulls_is_all_survivors_agree_and_n_null_is_recorded():
    entry = next(e for e in ds.config()["field_map"] if e["source"] == "eea_co2_cars" and e["field"] == "wheelbase_mm")
    rows = [{"row_id": "a", "wheelbase_mm": "2975", "wheelbase_mm_min": "2975", "wheelbase_mm_max": "2975"},
            {"row_id": "b"}, {"row_id": "c", "wheelbase_mm": None}]
    offer = field_offers({"route": "european", "level": "exact_technical_variant",
                          "sources": {"eea_co2_cars": {"status": "ambiguous", "survivors": rows,
                                                       "configurations": ["a", "b", "c"]}}}, [entry])[0]
    assert offer["status"] == "offered" and offer["value"] == 2975
    assert offer["identified_by"] == "all_survivors_agree" and offer["n_null"] == 2
    two = [*rows, {"row_id": "d", "wheelbase_mm": "2980", "wheelbase_mm_min": "2980", "wheelbase_mm_max": "2980"}]
    disagree = field_offers({"route": "european", "level": "exact_technical_variant",
                             "sources": {"eea_co2_cars": {"status": "ambiguous", "survivors": two}}}, [entry])[0]
    assert disagree["status"] == "survivors_disagree" and disagree["values"] == ["2975", "2980"]


def test_the_22010_mass_offer_comes_from_the_jp91_co2_47_rows_only():
    payload = build_level15_payload(_row("22010"))
    result = match(identity_fingerprint(payload), payload, rows_by_source={"eea_co2_cars": T.BMW_530E_2020})
    eea = result["sources"]["eea_co2_cars"]
    assert eea["exact_subset"]["basis"] == "type_code+co2"
    assert set(eea["exact_subset"]["row_ids"]) == {r["row_id"] for r in JP91_47}
    mass = next(o for o in field_offers(result) if o["field"] == "curb_weight_kg" and o["source"] == "eea_co2_cars")
    assert mass["status"] == "offered" and mass["value"] == 1935 and mass["subset"] == "type_code+co2"
    assert set(mass["row_ids"]) <= {r["row_id"] for r in JP91_47}
    # a broader survivor set never reaches the offer: survivors outside the exact subset are not read
    other = {**JP91_47[0], "row_id": "outside", "mass_running_order_kg": 2035.0, "mass_running_order_kg_min": 2035.0,
             "mass_running_order_kg_max": 2035.0}
    widened = {**result, "sources": {**result["sources"], "eea_co2_cars": {
        **eea, "survivors": eea["survivors"] + [other]}}}
    mass = next(o for o in field_offers(widened) if o["field"] == "curb_weight_kg" and o["source"] == "eea_co2_cars")
    assert mass["value"] == 1935 and "outside" not in mass["row_ids"]


# --- M4: ADEME ------------------------------------------------------------------------------------------------------------

ADEME_530E = {"row_id": "ademe-1", "make": "BMW", "model": "SERIE 5", "version": "530e xDrive Touring", "fuel": "EE",
              "body": "BREAK", "power_kw": "135", "catalogue_date": "2026-09-30T00:00:00+00:00"}


def test_an_ademe_break_row_is_vetoed_for_a_sedan_and_berline_stays():
    keys = _keys("22010", year=2026, body="sedan", type_code=None)
    out = match_source("ademe_car_labelling", keys, rows=[ADEME_530E])
    assert out["status"] == "none" and "body" in out["vetoed"][0]["vetoes"]
    berline = match_source("ademe_car_labelling", keys, rows=[{**ADEME_530E, "body": "BERLINE"}])
    assert "body" not in (berline.get("vetoed") or [{}])[0].get("vetoes", []) and berline["status"] == "unique"
    weak = match_source("ademe_car_labelling", keys, rows=[{**ADEME_530E, "body": "BERLINE", "power_kw": "100"}])
    assert "power" in weak["vetoed"][0]["vetoes"]                                          # A4 on Puissance maximale


def test_a_2020_target_is_out_of_the_ademe_catalogue_period():
    keys = _keys("22010", type_code=None)
    assert keys["year"] == 2020
    out = match_source("ademe_car_labelling", keys, rows=[{**ADEME_530E, "body": "BERLINE"}])
    assert out["status"] == "out_of_period" and out["candidates"] == 0
    assert out["catalogue"] == {"status": "out_of_period", "catalogue_date": "2026-09-30T00:00:00+00:00",
                                "basis": "rows", "catalogue_year": 2026, "target_year": 2020, "window_years": 1}
    in_period = match_source("ademe_car_labelling", {**keys, "year": 2025, "body": "sedan"},
                             rows=[{**ADEME_530E, "body": "BERLINE"}])
    assert in_period["catalogue"]["status"] == "in_period" and in_period["status"] == "unique"


def test_without_a_row_date_the_ademe_snapshot_date_decides(tmp_path):
    folder = tmp_path / "open"
    row = {k: v for k, v in ADEME_530E.items() if k != "catalogue_date"} | {"body": "BERLINE"}
    ds.write_snapshot("ademe_car_labelling", [row], {"built_at": "2026-10-06T18:23:12+00:00"}, folder=folder)
    out = match_source("ademe_car_labelling", _keys("22010", type_code=None), folder=folder)
    assert out["status"] == "out_of_period" and out["catalogue"]["basis"] == "built_at"
    ds.write_snapshot("ademe_car_labelling", [row], {"built_at": "2026-10-06T18:23:12+00:00",
                                                     "catalogue_date": "2021-05-01"}, folder=folder)
    out = match_source("ademe_car_labelling", _keys("22010", type_code=None), folder=folder)
    assert out["catalogue"]["basis"] == "catalogue_date" and out["catalogue"]["status"] == "in_period"


def test_the_ademe_build_records_the_catalogue_date_from_the_dataset_metadata():
    from src.open_data.build import ademe_catalogue_date

    cfg = ds.datasets()["ademe_car_labelling"]
    meta = {"title": "ADEME - Car Labelling", "updatedAt": "2026-09-30T08:00:00Z", "dataUpdatedAt": "2026-09-29T21:00:00Z"}
    assert ademe_catalogue_date(lambda url: json.dumps(meta).encode(), cfg) == {"date": "2026-09-29T21:00:00Z",
                                                                                 "basis": "dataUpdatedAt"}
    out = ademe_catalogue_date(lambda url: json.dumps({"updatedAt": "2026-09-30"}).encode(), cfg)
    assert out == {"date": "2026-09-30", "basis": "updatedAt"}
    assert ademe_catalogue_date(lambda url: (_ for _ in ()).throw(RuntimeError("down")), cfg)["date"] is None


# --- R: the shadow report -------------------------------------------------------------------------------------------------

def _runs(tmp_path: Path, wheelbases: list[int]) -> Path:
    """One run per value: record 22010 (its Level 1.5 payload), its result stating curb weight 1935 and the wheelbase."""
    runs = tmp_path / "runs"
    payload = build_level15_payload(_row("22010"))
    for n, wheelbase in enumerate(wheelbases):
        vehicle = runs / f"2026100{n}T000000Z-test" / "22010"
        vehicle.mkdir(parents=True)
        (vehicle / "input.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        (vehicle / "result.json").write_text(json.dumps({"output": {"fields": {
            "curb_weight_kg": {"value": 1935}, "wheelbase_mm": {"value": wheelbase}}}}), "utf-8")
    (runs / "_internal").mkdir()
    return runs


@pytest.fixture()
def eea_folder(tmp_path):
    folder = tmp_path / "open"
    ds.write_shards("eea_co2_cars", T.BMW_530E_2020, {}, status_used={2020: "F"}, folder=folder)
    yield folder


def test_the_shadow_report_gives_the_agreement_table_and_the_proposed_allowlist(tmp_path, eea_folder):
    from src.open_data.shadow import shadow_report

    report = shadow_report(_runs(tmp_path, [2975] * 5 + [2980]), folder=eea_folder)
    assert report["vehicles_total"] == 6 and report["vehicles_failed"] == 0
    table = {(t["source"], t["field"]): t for t in report["table"]}
    mass = table[("eea_co2_cars", "curb_weight_kg")]
    assert (mass["agree"], mass["disagree"], mass["offered_agree"], mass["route"]) == (6, 0, 6, "european_co2")
    wheelbase = table[("eea_co2_cars", "wheelbase_mm")]
    assert (wheelbase["agree"], wheelbase["disagree"]) == (5, 1)
    assert {(p["source"], p["field"]) for p in report["proposed"]} == {("eea_co2_cars", "curb_weight_kg")}
    assert report["disagreements"] == [{"run_id": "20261005T000000Z-test", "record_id": "22010",
                                        "source": "eea_co2_cars", "field": "wheelbase_mm", "route": "european_co2",
                                        "status": "offered", "offer_value": 2975, "run_value": 2980,
                                        "row_ids": report["disagreements"][0]["row_ids"]}]
    assert report["type_code"]["BMW"]["per_rule"] == {"exact_va": 6}
    assert all(v["level"] == "exact_technical_variant" for v in report["vehicles"])


def test_the_api_runs_the_report_stores_it_and_refuses_without_space(client, ctx, eea_folder, monkeypatch):
    from src.mcp_server.tools import Observer
    from src.storage import disk

    runs = ctx.paths.runs_dir
    for n in range(2):
        vehicle = runs / f"2026100{n}T000000Z-api" / "22010"
        vehicle.mkdir(parents=True)
        (vehicle / "input.json").write_text(json.dumps(build_level15_payload(_row("22010")), ensure_ascii=False),
                                            "utf-8")
        (vehicle / "result.json").write_text(json.dumps({"output": {"fields": {"curb_weight_kg": {"value": 1935}}}}))
    assert client.get("/api/data/open-data/shadow-report").json() == {"report": None}
    ds.set_snapshot_dir(eea_folder)
    try:
        report = client.post("/api/data/open-data/shadow-report").json()["report"]
    finally:
        ds.set_snapshot_dir(None)
    from src.open_data.shadow import vehicle_dirs

    expected = len(vehicle_dirs(runs))                      # the fixture context's own runs plus the two above
    assert report["vehicles_total"] == expected >= 2
    assert sum(1 for v in report["vehicles"] if v["record_id"] == "22010") == 2
    stored = ctx.paths.data_dir / "derived" / "open_data_shadow_report.json"
    assert json.loads(stored.read_text("utf-8"))["generated_at"] == report["generated_at"]
    assert client.get("/api/data/open-data/shadow-report").json()["report"]["vehicles_total"] == expected
    mcp = Observer(ctx.paths).open_data_shadow_report()
    assert mcp["report"]["vehicles_total"] == expected and mcp["path"].endswith("open_data_shadow_report.json")
    monkeypatch.setattr(disk, "free_bytes", lambda path: 50 * 1024 * 1024)
    refused = client.post("/api/data/open-data/shadow-report")
    assert refused.status_code == 507 and refused.json()["error"]["code"] == "insufficient_disk"


from test_api import ACCESS, client, ctx, data_root, gate  # noqa: E402,F401  (the API fixtures)

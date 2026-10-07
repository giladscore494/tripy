"""EEA 2010-2016 (the NEDC years) in the open-data build: Y1 the years (final rows only up to 2021, a year without them
reported), Y2 the per-year schema (a year without Ewltp groups by co2_nedc, its absent columns recorded), Y3 the match
and offers of a 2010-2016 target (no CO2 key; exact only through the type code; NEDC never compared with or offered as
WLTP), Y4 the schema audit, and the deterministic shards. No network: DISCODATA is the fake of
tests/test_open_data_builds.py; the targets are the MILO records 19931 (BMW 320I 2015, degem 3B11) and 59987 (SKODA
OCTAVIA 2015) of data/open_data_coverage_records.json; the EEA / ADEME rows are synthetic.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import pytest

from fixtures import eea_type_code as T
from fixtures import open_data_live_headers as H
from src.db import build_level15_payload
from src.gov_registry import identity_fingerprint
from src.open_data import audit
from src.open_data import build as B
from src.open_data import datasets as ds
from src.open_data.build import build_dataset, build_eea, build_eea_csv
from src.open_data.match import match
from src.open_data.offers import field_offers, row_cycle

from test_open_data_builds import _eea_fetch
from test_open_data_repo import _script

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 50 * 1024 * 1024
# a 2014 DISCODATA header: no WLTP column, no fuel consumption, no electric energy; "Enedc (g/km)" is present but empty
# and the NEDC CO2 is in "E (g/km)" (the live [latest] table, final rows 2010-2016: data/open_datasets.json
# column_sources_by_year)
NEDC_HEADER = [c for c in H.EEA_2018_HEADER if c not in ("Ewltp (g/km)", "Erwltp (g/km)", "Fc", "Z (Wh/km)")]


@pytest.fixture(autouse=True)
def _reset():
    yield
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def _record(record: str) -> dict:
    rows = json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    return build_level15_payload(next(r for r in rows if r["upstream_record_id"] == record))


def _nedc_rows(year: int = 2014) -> list[dict]:
    return [H.eea_row(NEDC_HEADER, Year=year, Ve="A1", **{"Enedc (g/km)": None, "E (g/km)": 172, "W (mm)": 2910,
                                                            "R": 3}),
            H.eea_row(NEDC_HEADER, Year=year, Ve="A1", **{"Enedc (g/km)": None, "E (g/km)": 165, "W (mm)": None,
                                                            "R": 1}),
            H.eea_row(NEDC_HEADER, Year=year, Ve="A2", **{"Enedc (g/km)": None, "E (g/km)": 172, "W (mm)": 2910,
                                                            "R": 4})]


# --- Y1 / Y2: the years and the per-year schema ---------------------------------------------------------------------------

def test_a_2014_header_without_ewltp_groups_by_co2_nedc_and_records_the_absent_columns():
    queries: list[str] = []
    built = build_eea(_eea_fetch([(2014, "F")], {(2014, "F"): _nedc_rows()}, queries, header=NEDC_HEADER))
    report = built["years"][0]
    assert report["status"] == "built" and report["status_used"] == "F"
    assert {"co2_wltp", "fuel_consumption_l_100km", "energy_wh_km", "electric_range_km"} <= set(
        report["absent_columns"]) and built["absent_columns"]["2014"] == report["absent_columns"]
    grouped = [q for q in queries if "GROUP BY" in q]
    assert grouped and all("[E (g/km)]" in q.split("GROUP BY", 1)[1] and "Ewltp" not in q and "Enedc" not in q
                           for q in grouped)
    assert report["mapping"]["co2_nedc"] == "E (g/km)" and "co2_wltp" not in report["mapping"]
    assert report["live_header"] == NEDC_HEADER
    # the two A1 configurations differ only by their NEDC CO2: two rows, never merged
    assert sorted((r["version"], r["co2_nedc"]) for r in built["rows"]) == [("A1", 165), ("A1", 172), ("A2", 172)]
    assert all(r.get("co2_wltp") is None and r["year"] == 2014 for r in built["rows"])
    assert report["audit"]["keys"]["co2_wltp"]["state"] == "absent"


def test_2010_2016_co2_nedc_resolves_to_e_not_ambiguous_and_2017_keeps_enedc():
    """column_sources_by_year REPLACES the spellings of co2_nedc for 2010-2016: both "Enedc (g/km)" (empty there) and
    "E (g/km)" are in those headers, and an added alias would match both and stop the year as ambiguous."""
    cfg = ds.datasets()["eea_co2_cars"]
    assert "Enedc (g/km)" in NEDC_HEADER and "E (g/km)" in NEDC_HEADER
    for year in range(2010, 2017):
        resolved = B.resolve_map(NEDC_HEADER, B.eea_year_columns(cfg, year))
        assert not resolved["ambiguous"] and not resolved["missing"]
        assert resolved["mapping"]["co2_nedc"] == "E (g/km)"
    for year in (2009, 2017, 2018):
        resolved = B.resolve_map(H.EEA_2018_HEADER, B.eea_year_columns(cfg, year))
        assert not resolved["ambiguous"] and resolved["mapping"]["co2_nedc"] == "Enedc (g/km)"
    assert B.eea_csv_columns(cfg, 2014)["co2_nedc"]["source"] == ["E (g/km)"]             # the datahub CSV too
    # an added alias instead of a replacement would be ambiguous (why it is a replacement)
    added = {**cfg, "column_sources_by_year": {}, "column_aliases_by_year": {"2014": {"co2_nedc": ["E (g/km)"]}}}
    assert B.resolve_map(NEDC_HEADER, B.eea_year_columns(added, 2014))["ambiguous"][0]["key"] == "co2_nedc"


def test_a_zero_nedc_co2_is_kept_as_a_value_not_missing(tmp_path):
    rows = [H.eea_row(NEDC_HEADER, Year=2015, Mk="TESLA", Cn="MODEL S", Ft="electric", Fm="E", Ve="P85",
                      **{"Ec (cm3)": 0, "Enedc (g/km)": None, "E (g/km)": 0, "R": 2})]
    built = build_eea(_eea_fetch([(2015, "F")], {(2015, "F"): rows}, [], header=NEDC_HEADER))
    (row,) = built["rows"]
    assert row["co2_nedc"] == 0 and built["years"][0]["audit"]["keys"]["co2_nedc"]["coverage_pct"] == 100.0
    assert "co2_nedc" not in built["years"][0]["empty_columns"]
    shard = ds.write_shards("eea_co2_cars", built["rows"], {}, folder=tmp_path)[0]["path"]
    (stored,), _ = ds.read_shard(shard)
    assert stored["co2_nedc"] == 0 and row_cycle(stored) == "nedc"
    entry = next(e for e in ds.config()["field_map"] if e["field"] == "co2_nedc_g_km")
    offers = field_offers({"route": "european", "level": "exact_technical_variant", "sources": {"eea_co2_cars": {
        "status": "unique", "survivors": [{**stored, "row_id": "eea-1"}], "configurations": ["x"]}}}, [entry])
    assert offers[0]["value"] == 0 and offers[0]["status"] == "offered" and offers[0]["n_null"] == 0


def test_a_year_with_both_co2_columns_groups_by_both():
    queries: list[str] = []
    rows = [H.eea_row(Year=2018, **{"Ewltp (g/km)": 190}), H.eea_row(Year=2018, **{"Ewltp (g/km)": 190,
                                                                                     "Enedc (g/km)": 160})]
    built = build_eea(_eea_fetch([(2018, "F")], {(2018, "F"): rows}, queries))
    grouped = next(q for q in queries if "GROUP BY" in q).split("GROUP BY", 1)[1]
    assert "[Ewltp (g/km)]" in grouped and "[Enedc (g/km)]" in grouped
    assert sorted(r["co2_nedc"] for r in built["rows"]) == [160, 172]


def test_a_2010_2016_year_with_only_provisional_rows_is_not_built_and_is_reported(tmp_path, monkeypatch):
    rows = {(2013, "P"): [H.eea_row(NEDC_HEADER, Year=2013, Status="P")], (2014, "F"): _nedc_rows()}
    queries: list[str] = []
    fetch = _eea_fetch([(2013, "P"), (2014, "F")], rows, queries, header=NEDC_HEADER)
    built = build_eea(fetch)
    reports = {r["year"]: r for r in built["years"]}
    assert reports[2013] == {"year": 2013, "status": "skipped", "reason": "no_final_rows", "statuses": ["P"]}
    assert not any("[Year] = 2013" in q for q in queries)                # never a provisional 2010-2016 row
    assert {r["year"] for r in built["rows"]} == {2014}
    # the build step: the 2013 failure is listed at the end of the summary; 2014 is written
    build = _script("build_open_data")
    monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None: build_dataset(name, fetch))
    results, manifest, _ = build.run(["eea_co2_cars"], tmp_path / "open", tmp_path / "work", {}, LIMIT, None)
    assert [s["year"] for s in manifest["datasets"]["eea_co2_cars"]["shards"]] == [2014]
    text = build.summary(results, manifest)
    failures = text.split("Per-year failures:", 1)[1]
    assert "eea_co2_cars 2013: skipped (no_final_rows)" in failures and "2014" not in failures


def test_the_datahub_csv_never_builds_a_2010_2021_year_from_a_provisional_record(monkeypatch):
    monkeypatch.setattr(B, "eea_csv_sources", lambda fetch, cfg: ({2013: {"uuid": "u13", "year": 2013, "status": "P",
                                                                          "links": ["https://x/2013.zip"]}}, []))
    downloads = []
    out = build_eea_csv(lambda url: b"", lacking_years={2013},
                        download=lambda url, target: downloads.append(url) or 0)
    assert out["years"] == [{"year": 2013, "status_used": "P", "source": "datahub_csv", "record": "u13",
                             "status": "skipped", "reason": "no_final_rows"}] and not downloads


def test_the_datahub_csv_reads_an_early_year_only_when_discodata_lacks_it(monkeypatch):
    seen = {}

    def fake_csv(fetch, *, skip_years=None, lacking_years=None, **_):
        seen.update(skip=set(skip_years or ()), lacking=set(lacking_years or ()))
        return {"rows": [], "years": [], "absent_columns": {}, "urls": [], "discovery": []}
    monkeypatch.setattr(B, "build_eea_csv", fake_csv)
    no_power = [c for c in NEDC_HEADER if c != "Ep (KW)"]
    rows = {(2014, "F"): _nedc_rows(), (2012, "F"): [H.eea_row(no_power, Year=2012)]}   # 2012 stops (schema)
    B.build_eea_all(_eea_fetch([(2011, "P"), (2012, "F"), (2014, "F")], rows, [], header=NEDC_HEADER))
    assert seen["skip"] == {2014}
    assert seen["lacking"] == {2010, 2011, 2013, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022}  # never 2012 / 2014


# --- Y3: match and offers of a 2010-2016 target ----------------------------------------------------------------------------

def _bmw_2015(variant: str = "3B11", **extra) -> dict:
    row = {"make": "BMW", "model": "320I", "type_approval": "e1*2001/116",
           "variant": variant, "version": "8A31", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 1997,
           "power_kw": 135, "co2_nedc": 124, "year": 2015, "wheelbase_mm": 2810, "wheelbase_mm_min": 2810,
           "wheelbase_mm_max": 2810, "mass_running_order_kg": 1495, "mass_running_order_kg_min": 1495,
           "mass_running_order_kg_max": 1495}
    row.update(extra)
    row["row_id"] = f"eea-2015-F-{row['variant']}-{row['version']}"
    return row


def test_a_bmw_2015_type_code_equal_to_va_is_exact_without_a_co2_key():
    payload = _record("19931")
    fp = identity_fingerprint(payload)
    assert fp["type_code"] == "3B11"
    rows = [_bmw_2015(), _bmw_2015("3D31", power_kw=110, displacement_cc=1598), _bmw_2015("3B31", power_kw=105)]
    result = match(fp, payload, rows_by_source={"eea_co2_cars": rows})
    eea = result["sources"]["eea_co2_cars"]
    assert result["co2_key"] == "unavailable" and result["keys"]["co2_wltp"] is None and result["nedc_target"]
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "exact_va"
    assert eea["candidates"] == 1 and eea["status"] == "unique" and "co2_wltp" not in eea["keys_matched"]
    assert "superseded_by_exact_key" not in eea                           # no co2 key: nothing superseded
    assert result["level"] == "exact_technical_variant" and result["level_basis"] == "european_type_code"
    offers = {o["field"]: o for o in field_offers(result) if o["source"] == "eea_co2_cars"}
    assert offers["wheelbase_mm"]["status"] == "offered" and offers["wheelbase_mm"]["value"] == 2810
    assert offers["curb_weight_kg"]["status"] == "offered" and offers["curb_weight_kg"]["value"] == 1495
    assert offers["co2_nedc_g_km"]["status"] == "offered" and offers["co2_nedc_g_km"]["value"] == 124
    assert offers["co2_nedc_g_km"]["companion"] == {"co2_standard": "NEDC"}


def test_two_type_code_rows_with_different_nedc_co2_disagree_and_the_null_states_nothing():
    payload = _record("19931")
    rows = [_bmw_2015(), _bmw_2015(version="8A32", co2_nedc=129), _bmw_2015(version="8A33", co2_nedc=None)]
    result = match(identity_fingerprint(payload), payload, rows_by_source={"eea_co2_cars": rows})
    assert result["level"] == "exact_technical_variant"
    offers = {o["field"]: o for o in field_offers(result) if o["source"] == "eea_co2_cars"}
    assert offers["co2_nedc_g_km"]["status"] == "survivors_disagree" and offers["co2_nedc_g_km"]["n_null"] == 1
    assert offers["wheelbase_mm"]["status"] == "offered"                  # M2: every row agrees on 2810


def test_a_make_without_a_type_code_rule_is_at_most_body_powertrain(monkeypatch):
    payload = _record("59987")
    fp = identity_fingerprint(payload)
    eea = [{"row_id": "eea-2015-F-1", "make": "SKODA", "model": "OCTAVIA", "variant": "AUCZEX", "version": "FMCZFB",
            "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 1395, "power_kw": 103, "co2_nedc": 119,
            "year": 2015, "wheelbase_mm": 2686, "mass_running_order_kg": 1320}]
    ademe = [{"row_id": "ademe-1", "make": "SKODA", "model": "OCTAVIA", "version": "OCTAVIA 1.4 TSI 140 AMBITION DSG",
              "fuel": "ES", "displacement_cc": 1395, "power_kw": 103, "mass_running_order_kg": 1320}]
    sources = {"eea_co2_cars": eea, "ademe_car_labelling": ademe}
    result = match(fp, payload, rows_by_source=sources)
    assert result["sources"]["eea_co2_cars"]["type_code"]["status"] == "no_rule"
    assert result["sources"]["eea_co2_cars"]["status"] == "unique"
    assert result["sources"]["ademe_car_labelling"]["status"] == "unique"
    assert result["level"] == "body_powertrain" and result["co2_key"] == "unavailable"
    offers = {o["field"]: o for o in field_offers(result) if o["source"] == "eea_co2_cars"}
    assert offers["co2_nedc_g_km"]["status"] == "below_level" and offers["wheelbase_mm"]["status"] == "below_level"
    # the same rows for a target outside the NEDC years: the dual-unique rule (unchanged) would make it exact; the
    # level is never raised for 2010-2016 because an older year has fewer candidates
    config = ds.config()
    eea_cfg = {**config["datasets"]["eea_co2_cars"], "nedc_years": {}}
    monkeypatch.setattr(ds, "config", lambda path=None: {**config, "datasets": {**config["datasets"],
                                                                                 "eea_co2_cars": eea_cfg}})
    assert match(fp, payload, rows_by_source=sources)["level"] == "exact_technical_variant"


def test_an_nedc_value_is_never_compared_with_a_wltp_target_nor_offered_as_wltp():
    payload = _record("22010")                                           # 530e 2020: co2_wltp 47
    fp = identity_fingerprint(payload)
    jp91 = [r for r in T.BMW_530E_2020 if r["variant"] == "JP91" and r["power_kw"] == 135]
    for nedc in (47, 99):                                                # equal or not: never compared
        rows = [{**{k: v for k, v in r.items() if k != "co2_wltp"}, "co2_nedc": nedc, "electric_range_km": 54,
                 "electric_range_km_min": 54, "electric_range_km_max": 54} for r in jp91]
        result = match(fp, payload, rows_by_source={"eea_co2_cars": rows})
        eea = result["sources"]["eea_co2_cars"]
        assert result["co2_key"] == "co2_wltp" and eea["status"] in ("unique", "ambiguous")
        assert "co2_wltp" not in eea["keys_matched"] and not eea.get("vetoed")
        assert eea["type_code"]["co2_selected"] is False and "superseded_by_exact_key" not in eea
        assert all(row_cycle(r) == "nedc" for r in eea["survivors"])
        offers = [o for o in field_offers(result) if o["source"] == "eea_co2_cars"]
        assert not [o for o in offers if o["field"] == "co2_wltp" or o["column"] == "co2_wltp"]
        nedc_offer = next(o for o in offers if o["field"] == "co2_nedc_g_km")
        assert nedc_offer["value"] == nedc and nedc_offer["definition"] == "nedc_combined"
        assert not [o for o in offers if o["field"] == "electric_range_km"]   # WLTP field: never from an NEDC row
        # fuel consumption needs the co2_wltp key: an NEDC row never supplies it either
        assert not [o for o in offers if o["field"] == "fuel_consumption_combined_l_100km"
                    and o.get("status") == "offered"]


def test_the_row_cycle():
    assert row_cycle({"co2_wltp": 120, "co2_nedc": 100, "year": 2018}) == "wltp"
    assert row_cycle({"co2_nedc": 100, "year": 2018}) == "nedc"
    assert row_cycle({"year": 2015}) == "nedc"                           # a BEV of the NEDC years
    assert row_cycle({"year": 2021}) is None


# --- Y4: the audit ---------------------------------------------------------------------------------------------------------

def test_year_stats_weight_the_coverage_by_registrations():
    rows = [{"make": "BMW", "wheelbase_mm": 2810, "co2_nedc": 124, "registrations": 3},
            {"make": "BMW", "wheelbase_mm": None, "co2_nedc": 120.5, "registrations": 1}]
    stats = audit.year_stats(rows, ["make", "wheelbase_mm", "co2_nedc", "co2_wltp"],
                             {"make": "Mk", "wheelbase_mm": "W (mm)", "co2_nedc": "Enedc (g/km)"})
    keys = stats["keys"]
    assert stats["registrations"] == 4 and stats["weighted"]
    assert keys["wheelbase_mm"] == {"state": "mapped", "column": "W (mm)", "type": "INTEGER", "null_pct": 50.0,
                                    "distinct": 1, "coverage_pct": 75.0}
    assert keys["co2_nedc"]["type"] == "REAL" and keys["co2_nedc"]["coverage_pct"] == 100.0
    assert keys["co2_wltp"]["state"] == "absent" and keys["co2_wltp"]["coverage_pct"] == 0.0
    assert keys["make"]["type"] == "TEXT" and keys["make"]["distinct"] == 1


def test_the_audit_writes_the_markdown_and_the_csv_from_the_built_snapshot(tmp_path, monkeypatch):
    rows = {(2014, "F"): _nedc_rows(), (2013, "P"): [H.eea_row(NEDC_HEADER, Year=2013, Status="P")]}
    fetch = _eea_fetch([(2013, "P"), (2014, "F")], rows, [], header=NEDC_HEADER)
    build = _script("build_open_data")
    monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None: build_dataset(name, fetch))
    out = tmp_path / "open"
    build.run(["eea_co2_cars"], out, tmp_path / "work", {}, LIMIT, "https://run/7")
    script = _script("audit_eea")
    md, table, summary = tmp_path / "audit.md", tmp_path / "audit.csv", tmp_path / "short.md"
    assert script.main(["--open", str(out), "--md", str(md), "--csv", str(table), "--summary", str(summary)]) == 0
    text = md.read_text("utf-8")
    assert "## EEA schema audit (per year)" in text and "Build: https://run/7" in text
    line = next(l for l in text.splitlines() if l.startswith("| 2014 |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    head = next(l for l in text.splitlines() if l.startswith("| year |"))
    named = dict(zip([c.strip() for c in head.strip("|").split("|")], cells))
    # registrations: A1/172 3, A1/165 1 (no wheelbase), A2/172 4 -> wheelbase 7 / 8
    assert named["status used"] == "F" and named["rows"] == "3" and named["registrations"] == "8"
    assert named["wheelbase_mm"] == "87.5" and named["co2_nedc"] == "100.0" and named["co2_wltp"] == "absent"
    assert "Live header: `ID, MS" in text and "| co2_nedc | E (g/km) | mapped | INTEGER |" in text
    assert "| 2014 | 3 | 0 | 0 |" in text                               # sanity: shard rows, other year, HTML
    assert text.rstrip().splitlines()[-1] == "- 2013: skipped (no_final_rows)"   # the summary ends with the failures
    parsed = list(csv.DictReader(io.StringIO(table.read_text("utf-8"))))
    wheelbase = next(r for r in parsed if r["year"] == "2014" and r["key"] == "wheelbase_mm")
    assert wheelbase["coverage_pct_weighted"] == "87.5" and wheelbase["live_column"] == "W (mm)"
    assert next(r for r in parsed if r["year"] == "2014" and r["key"] == "co2_wltp")["state"] == "absent"
    assert "Live header" not in summary.read_text("utf-8") and "| 2014 |" in summary.read_text("utf-8")


def test_the_audit_flags_rows_of_another_year_and_an_html_body(tmp_path):
    shard = ds.write_shards("eea_co2_cars", [
        {"row_id": "1", "make": "BMW", "model": "<html><body>Service Unavailable</body></html>", "year": 2015}],
        {}, folder=tmp_path)[0]["path"]
    check = audit.shard_checks(shard, 2014)
    assert check == {"rows": 1, "other_year_rows": 1, "html_rows": 1}
    problems = audit.failures([{"year": 2014, "status": "built", "status_used": "F", "rows": 1}], {2014: check}, 2021)
    assert {p["problem"] for p in problems} == {"other_year_rows", "html_or_error_body"}
    assert audit.failures([{"year": 2012, "status": "built", "status_used": "F", "rows": 0}], {}, 2021) == [
        {"year": 2012, "problem": "final_year_without_rows"}]


def test_an_error_body_is_never_parsed_as_rows():
    from src.open_data.build import EeaQueryError, eea_response

    for body in (b"<html><body>Service Unavailable</body></html>", b'{"errorMessage": "Incorrect syntax"}', b"{}"):
        with pytest.raises(EeaQueryError):
            eea_response(body)


# --- determinism -----------------------------------------------------------------------------------------------------------

def test_the_same_fixture_builds_byte_identical_shards(tmp_path, monkeypatch):
    rows = {(2014, "F"): _nedc_rows(), (2015, "F"): _nedc_rows(2015)}
    build = _script("build_open_data")
    shas = []
    for n in (1, 2):
        fetch = _eea_fetch([(2014, "F"), (2015, "F")], rows, [], header=NEDC_HEADER)
        monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None, f=fetch: build_dataset(name, f))
        _, manifest, _ = build.run(["eea_co2_cars"], tmp_path / f"open{n}", tmp_path / f"work{n}", {}, LIMIT, None)
        shas.append([(s["file"], s["sha256"], s["bytes"]) for s in manifest["datasets"]["eea_co2_cars"]["shards"]])
    assert shas[0] == shas[1] and [s[0] for s in shas[0]] == ["eea_co2_cars/2014.sqlite.gz",
                                                               "eea_co2_cars/2015.sqlite.gz"]
    for file, _, _ in shas[0]:
        assert (tmp_path / "open1" / file).read_bytes() == (tmp_path / "open2" / file).read_bytes()


def test_the_size_gate_warns_over_60_mb_and_writes_the_shards_anyway():
    build = _script("build_open_data")
    items = [{"year": y, "part": None, "status": "ok", "bytes": 9 * 1024 * 1024, "rows": 1} for y in range(2010, 2017)]
    lines = build.size_gate({"eea_co2_cars": {"shard_items": items + [{"year": 2018, "status": "ok", "bytes": 1}]}})
    assert "Size gate eea_co2_cars 2010-2016: 63.0 MB compressed in 7 shard(s)" in lines[1]
    assert lines[2].startswith("**Warning:**")
    assert len(build.size_gate({"eea_co2_cars": {"shard_items": items[:2]}})) == 2      # under: sizes, no warning

"""EEA identity by the government type code (K1: degem_nm <-> EEA Va / Ve, per-make rules), T kept in the shards (K2)
and the 2023+ years from the EEA datahub CSV (K3). No network: the K1 rows are verbatim from the 2020 shard
(tests/fixtures/eea_type_code.py); the K3 CSV uses the recorded DISCODATA 2018 header (tests/fixtures/
open_data_live_headers.py) — the 2023+ CSV header itself could not be read from this environment.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from fixtures import eea_type_code as T
from fixtures import open_data_live_headers as H
from src.db import build_level15_payload
from src.gov_registry import identity_fingerprint
from src.open_data import datasets as ds
from src.open_data.build import (build_dataset, build_eea, build_eea_csv, datahub_related, eea_aggregate,
                                 title_year_status)
from src.open_data.match import match, type_code_coverage, type_code_match, type_code_rule
from src.open_data.offers import field_offers

from test_open_data_builds import _eea_fetch

ROOT = Path(__file__).resolve().parent.parent


def _record(record: str) -> dict:
    rows = json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    rows += json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    return build_level15_payload(next(r for r in rows if r["upstream_record_id"] == record))


# --- K1: the type-code key ------------------------------------------------------------------------------------------------

def test_22010_jp91_is_the_candidate_set_co2_47_selects_and_the_level_is_exact():
    payload = _record("22010")
    fp = identity_fingerprint(payload)
    assert fp["type_code"] == "JP91"
    result = match(fp, payload, rows_by_source={"eea_co2_cars": T.BMW_530E_2020})
    eea = result["sources"]["eea_co2_cars"]
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "exact_va"
    jp91 = [r for r in T.BMW_530E_2020 if r["variant"] == "JP91"]
    assert eea["candidates"] == len(jp91)                                   # JA91 / JA92 / JP92 never candidates
    survivors = {r["row_id"]: r for r in eea["survivors"]}
    assert survivors and all(r["variant"] == "JP91" and r["co2_wltp"] == 47 and r["power_kw"] == 135
                             for r in survivors.values())                   # co2 47 selects, 120 kW vetoed (A4)
    assert eea["type_code"]["co2_selected"] is True
    assert {v["row_id"] for v in eea["vetoed"]} == {r["row_id"] for r in jp91 if r["power_kw"] == 120}
    assert all(v["vetoes"] == ["power"] for v in eea["vetoed"])
    assert {"type_code_match", "co2_wltp", "power", "displacement"} <= set(eea["keys_matched"])
    assert result["level"] == "exact_technical_variant" and result["level_basis"] == "european_type_code"
    assert result["designation"] == "530e xDrive iPerformance JP91"
    offers = {o["field"]: o for o in field_offers(result) if o["source"] == "eea_co2_cars"}
    assert offers["curb_weight_kg"]["status"] == "offered" and offers["curb_weight_kg"]["value"] == 1935
    assert offers["wheelbase_mm"]["status"] == "offered" and offers["wheelbase_mm"]["value"] == 2975


def test_a_ja91_row_is_never_a_type_code_candidate():
    ja91 = next(r for r in T.BMW_530E_2020 if r["variant"] == "JA91")
    assert not type_code_rule("exact_va", "JP91", ja91)
    found = type_code_match(T.BMW_530E_2020, "JP91")
    assert found["status"] == "match" and {r["variant"] for r in found["rows"]} == {"JP91"}
    assert type_code_match([ja91], "JP91")["status"] == "no_match"


def test_the_type_code_matches_without_a_co2_match_and_power_still_vetoes():
    payload = _record("22010")
    fp = identity_fingerprint(payload)
    rows = [r for r in T.BMW_530E_2020 if r["variant"] == "JP91" and r.get("co2_wltp") != 47]
    result = match(fp, payload, rows_by_source={"eea_co2_cars": rows})
    eea = result["sources"]["eea_co2_cars"]
    assert eea["type_code"]["co2_selected"] is False and eea["status"] in ("unique", "ambiguous")
    assert "co2_wltp" not in eea["keys_matched"]                            # no co2 match, no co2 veto either
    assert result["level"] == "exact_technical_variant"                     # the identity is the type code
    only_120 = [r for r in rows if r["power_kw"] == 120]
    vetoed = match(fp, payload, rows_by_source={"eea_co2_cars": only_120})
    assert vetoed["sources"]["eea_co2_cars"]["status"] == "none" and vetoed["level"] is None


@pytest.mark.parametrize("code, row, expected", [
    ("ZWE211L-DWXNBW", {"variant": "ZWE211(E)", "version": "ZWE211L-DWXNBW(1D)"}, True),
    ("ZWE211L DWXNBW", {"variant": "ZWE211(E)", "version": "ZWE211L-DWXNBW(1D)"}, True),   # a space for the dash
    ("ZWE211L-DWXNBW", {"variant": "MZEA11(E)", "version": "MZEA11L-DEFNBW(1D)"}, False),
    ("ZYX11", {"variant": "ZYX11(E)", "version": "AHXNBW(1E)"}, True),                       # Va without the "(...)"
    ("ZWE211L-DWXNBW", {"variant": "ZWE211L-DWXNBW-X", "version": ""}, False),
])
def test_the_toyota_rule(code, row, expected):
    assert type_code_rule("toyota_style", code, row) is expected
    assert type_code_match([{**row, "make": "TOYOTA"}], code)["status"] == ("match" if expected else "no_match")


def test_a_make_without_a_rule_keeps_the_match_unchanged(monkeypatch):
    from fixtures import pr48_open_data as F
    from src.open_data import datasets

    payload = _record("85095")                                            # CTS: Cadillac has no type-code rule
    fp = identity_fingerprint(payload)
    with_rules = match(fp, payload, rows_by_source={"eea_co2_cars": F.CTS_EEA})
    assert with_rules["sources"]["eea_co2_cars"]["type_code"]["status"] == "no_rule"
    config = datasets.config()
    monkeypatch.setattr(datasets, "config", lambda path=None: {k: v for k, v in config.items()
                                                               if k != "eea_type_code_rules"})
    without = match(fp, payload, rows_by_source={"eea_co2_cars": F.CTS_EEA})
    drop = {k: v for k, v in with_rules["sources"]["eea_co2_cars"].items() if k != "type_code"}
    assert drop == without["sources"]["eea_co2_cars"] and with_rules["level"] == without["level"]
    assert type_code_match([{"make": "HYUNDAI", "variant": "B5P31"}], "J3813")["status"] == "no_rule"
    assert type_code_match([{"make": "SKODA", "type_approval": "5E"}], "5E33ND")["status"] == "no_rule"


def test_the_shadow_report_counts_per_make_and_rule_and_lists_unmatched_codes():
    rows = {"BMW": T.BMW_530E_2020, "TOYOTA": [{"make": "TOYOTA", "variant": "ZWE211(E)",
                                                "version": "ZWE211L-DWXNBW(1D)"}], "SKODA": []}
    out = type_code_coverage({"BMW": ["JP91", "JA91", "21AG"], "TOYOTA": ["ZWE211L DWXNBW", "MZEA11L-DEXNBW"],
                              "SKODA": ["5E33ND"]}, rows)
    assert out["BMW"] == {"degem_nm": 3, "rules": ["exact_va"], "per_rule": {"exact_va": 2}, "matched": 2,
                          "unmatched_examples": ["21AG"]}
    assert out["TOYOTA"]["per_rule"] == {"toyota_style": 1} and out["TOYOTA"]["unmatched_examples"] == [
        "MZEA11L-DEXNBW"]
    assert out["SKODA"]["rules"] == [] and out["SKODA"]["matched"] == 0


def test_the_report_script_has_a_type_code_section_per_make(tmp_path):
    import importlib.util
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("open_data_report", ROOT / "scripts" / "open_data_report.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    analysed = [{"record": "22010", "make": "BMW", "type_code": "JP91",
                 "type_code_match": {"status": "match", "rule": "exact_va"}},
                {"record": "x", "make": "BMW", "type_code": "21AG", "type_code_match": {"status": "no_match"}}]
    lines = "\n".join(script.type_code_section(analysed, {"BMW": {"degem_nm": 3, "rules": ["exact_va"],
                                                                  "per_rule": {"exact_va": 2}, "matched": 2,
                                                                  "unmatched_examples": ["21AG"]}}))
    assert "| BMW | 2 | exact_va: 1 | 21AG (no_match) |" in lines
    assert "| BMW | 3 | exact_va | exact_va: 2 | 2 (67 %) | 21AG |" in lines


# --- MERCEDES-BENZ: dotless_va and second_token_t ----------------------------------------------------------------------

def _mercedes(code: str, hp: int, cc: int, year: int = 2023) -> tuple[dict, dict]:
    """(fingerprint, payload) of a Mercedes record (48743, C180) with another type code, power, cc and year."""
    rows = json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    rows += json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    row = next(r for r in rows if r["upstream_record_id"] == "48743")
    payload = build_level15_payload({**row, "degem_nm": code, "koah_sus": hp, "nefah_manoa": cc, "shnat_yitzur": year,
                                     "co2_wltp": None})
    return identity_fingerprint(payload), payload


def _benz(row_id: str, year: int, **values) -> dict:
    return {"row_id": f"eea-{year}-T-{row_id}", "make": "MERCEDES-BENZ", "year": year, "fuel": "PETROL",
            "fuel_mode": "M", **values}


@pytest.mark.parametrize("code, row, expected", [
    ("253.981", {"variant": "253981"}, True),
    ("253 981", {"variant": "253981"}, True),
    ("253.98", {"variant": "253981"}, False),
    ("253.981", {"variant": "253982", "version": "253981"}, True),                 # Ve, the fallback
    ("253.981", {"variant": "X253981"}, False),
])
def test_the_dotless_rule(code, row, expected):
    assert type_code_rule("dotless_va", code, row) is expected
    assert type_code_match([{**row, "make": "MERCEDES-BENZ"}], code)["status"] == ("match" if expected else "no_match")


def test_dotless_va_prefers_va_rows_over_ve_rows():
    va, ve = {"make": "MERCEDES-BENZ", "variant": "253981", "row_id": "a"}, {"make": "MERCEDES-BENZ",
                                                                            "variant": "253", "version": "253981",
                                                                            "row_id": "b"}
    found = type_code_match([ve, va], "253.981")
    assert found["status"] == "match" and found["rule"] == "dotless_va" and found["rows"] == [va]
    assert type_code_match([ve], "253.981")["rows"] == [ve]


@pytest.mark.parametrize("code, row, expected", [
    ("LF5E R2EW", {"type_approval": "R2EW"}, True),
    ("lf5e  r2ew", {"type_approval": "R2EW"}, True),
    ("LF5E R2EW", {"type_approval": "X", "variant": "R2EW"}, True),               # Va, the fallback
    ("LF5E R2EW", {"type_approval": "R2EWX"}, False),                              # equality only, never contains
    ("FB3D H1GLE", {"type_approval": "H1GL"}, False),
    ("LF5E R2E", {"type_approval": "R2E"}, False),                                 # the second token < 4 characters
    ("R2EW", {"type_approval": "R2EW"}, False),                                    # a single token: never this rule
    ("A LF5E R2EW", {"type_approval": "R2EW"}, False),                             # three tokens
])
def test_the_second_token_rule(code, row, expected):
    assert type_code_rule("second_token_t", code, row) is expected


def test_a_single_token_code_never_uses_the_second_token_rule():
    row = {"make": "MERCEDES-BENZ", "type_approval": "R2EW", "variant": "X1", "version": "X2"}
    assert type_code_match([row], "R2EW")["status"] == "no_match"
    found = type_code_match([row], "LF5E R2EW")
    assert found["status"] == "match" and found["rule"] == "second_token_t"


def test_dotless_va_is_tried_before_second_token_t():
    by_va = {"make": "MERCEDES-BENZ", "variant": "LF5ER2EW", "row_id": "a"}
    by_t = {"make": "MERCEDES-BENZ", "type_approval": "R2EW", "row_id": "b"}
    found = type_code_match([by_t, by_va], "LF5E R2EW")
    assert found["rule"] == "dotless_va" and found["rows"] == [by_va]


def test_253_981_matches_its_va_row_and_a_row_5_kw_off_is_vetoed():
    fp, payload = _mercedes("253.981", hp=197, cc=1991, year=2020)                 # 197 hp = 144.9 kW
    assert fp["type_code"] == "253.981"
    rows = [_benz("1", 2020, model="GLC 300 4MATIC", type_approval="E1*2007/46*1229", variant="253981",
                  version="GLC300", displacement_cc=1991, power_kw=145.0, co2_wltp=190.0),
            _benz("2", 2020, model="GLC 300 4MATIC", type_approval="E1*2007/46*1229", variant="253981",
                  version="GLC300X", displacement_cc=1991, power_kw=150.0, co2_wltp=191.0),
            _benz("3", 2020, model="GLC 200", type_approval="E1*2007/46*1229", variant="253942",
                  version="GLC200", displacement_cc=1991, power_kw=145.0, co2_wltp=180.0)]
    result = match(fp, payload, rows_by_source={"eea_co2_cars": rows})
    eea = result["sources"]["eea_co2_cars"]
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "dotless_va"
    assert eea["candidates"] == 2                                                 # 253942 is never a candidate
    assert [r["row_id"] for r in eea["survivors"]] == ["eea-2020-T-1"]
    assert [(v["row_id"], v["vetoes"]) for v in eea["vetoed"]] == [("eea-2020-T-2", ["power"])]
    assert result["level"] == "exact_technical_variant" and result["level_basis"] == "european_type_code"
    near = match(*_mercedes("253.98", hp=197, cc=1991, year=2020), rows_by_source={"eea_co2_cars": rows})
    assert near["sources"]["eea_co2_cars"]["type_code"]["status"] == "no_match"
    only_off = match(fp, payload, rows_by_source={"eea_co2_cars": rows[1:2]})
    assert only_off["sources"]["eea_co2_cars"]["status"] == "none" and only_off["level"] is None


def test_lf5e_r2ew_matches_t_r2ew():
    fp, payload = _mercedes("LF5E R2EW", hp=204, cc=1999, year=2024)              # 204 hp = 150 kW
    rows = [_benz("1", 2024, model="E 300 e", type_approval="R2EW", variant="214", version="E300E",
                  displacement_cc=1999, power_kw=150.0, co2_wltp=13.0),
            _benz("2", 2024, model="E 220 d", type_approval="R2EX", variant="214", version="E220D",
                  displacement_cc=1993, power_kw=145.0, co2_wltp=140.0)]
    eea = match(fp, payload, rows_by_source={"eea_co2_cars": rows})["sources"]["eea_co2_cars"]
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "second_token_t"
    assert eea["candidates"] == 1 and [r["row_id"] for r in eea["survivors"]] == ["eea-2024-T-1"]


def test_ff8g_h1gle_gls_580_against_a_gls_400_d_row_is_vetoed():
    fp, payload = _mercedes("FF8G H1GLE", hp=489, cc=3982, year=2023)              # GLS 580: 360 kW, 3982 cc
    rows = [_benz("1", 2023, model="GLE / GLS 400 d 4MATIC", type_approval="H1GLE", variant="167", version="GLS400D",
                  fuel="DIESEL", displacement_cc=2989, power_kw=243.0, co2_wltp=250.0)]
    result = match(fp, payload, rows_by_source={"eea_co2_cars": rows})
    eea = result["sources"]["eea_co2_cars"]
    assert eea["type_code"]["status"] == "match" and eea["type_code"]["rule"] == "second_token_t"
    assert eea["status"] == "none" and not eea.get("survivors")
    assert eea["vetoed"][0]["vetoes"] == ["displacement", "power"]                 # only the hard vetoes
    assert result["level"] is None


def test_the_shard_index_groups_by_t_for_second_token_t(tmp_path, monkeypatch):
    from src.facts.service import dumps
    from src.open_data import match as M
    from src.open_data import shard_index as SI

    rows = [_benz(f"{n}", 2024, model="E 300 e", type_approval=t, variant="214", version="E300E",
                  displacement_cc=1999, power_kw=150.0, co2_wltp=13.0) for n, t in enumerate(["R2EW", "R2EX", "R2EW"])]
    ds.write_shard("eea_co2_cars", rows, {}, year=2024, folder=tmp_path)
    fp, payload = _mercedes("LF5E R2EW", hp=204, cc=1999, year=2024)
    from src.open_data.match import target_keys

    keys = target_keys(fp, payload)
    monkeypatch.setenv("TRIPY_SHARD_INDEX", "0")
    scanned = M.match_source("eea_co2_cars", keys, folder=tmp_path)
    monkeypatch.setenv("TRIPY_SHARD_INDEX", "1")
    SI.clear()
    try:
        for path in ds.shard_files("eea_co2_cars", folder=tmp_path):
            assert SI.build_now(path) is not None
        monkeypatch.setattr(ds, "query_rows", lambda *a, **k: pytest.fail("the index path must not scan"))
        indexed = M.match_source("eea_co2_cars", keys, folder=tmp_path)
    finally:
        SI.clear()
    assert dumps(indexed) == dumps(scanned)
    assert scanned["type_code"]["rule"] == "second_token_t" and scanned["candidates"] == 2   # R2EX is not a candidate


def test_no_other_make_changes(monkeypatch):
    """BMW / VOLVO / TOYOTA / LEXUS (22010 included): the records match byte-identically without the Mercedes rule."""
    from src.facts.service import dumps
    from src.open_data import datasets
    from src.open_data.match import match_source, target_keys

    config = datasets.config()
    before = json.loads(json.dumps(config))
    del before["eea_type_code_rules"]["makes"]["MERCEDES-BENZ"]
    records = json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    records += json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    keys = [target_keys(identity_fingerprint(p), p) for p in (build_level15_payload(r) for r in records
                                                                if r["tozar"] in ("ב מ וו", "טויוטה"))]
    assert len(keys) >= 17 and "JP91" in {k["type_code"] for k in keys}
    now = [dumps(match_source("eea_co2_cars", k)) for k in keys]
    monkeypatch.setattr(datasets, "config", lambda path=None: before)
    assert [dumps(match_source("eea_co2_cars", k)) for k in keys] == now
    synthetic = {"VOLVO": ({"make": "VOLVO", "variant": "XZ40"}, "XZ40"),
                 "LEXUS": ({"make": "LEXUS", "variant": "AXZH11(E)", "version": "AXZH11L-AWTLZW(1E)"},
                           "AXZH11L AWTLZW")}
    for row, code in synthetic.values():
        assert type_code_match([row], code, before) == type_code_match([row], code, config)
        assert type_code_match([row], code, config)["status"] == "match"


# --- K2: T in the shards --------------------------------------------------------------------------------------------------

def test_t_is_kept_in_a_compacted_shard(tmp_path):
    rows = {(2018, "F"): [H.eea_row(Year=2018), H.eea_row(Year=2018, Ve="X")]}
    ds.set_snapshot_dir(tmp_path / "open")
    try:
        built = build_dataset("eea_co2_cars", _eea_fetch([(2018, "F")], rows, []))
    finally:
        ds.set_snapshot_dir(None)
    assert built["status"] == "built"
    with sqlite3.connect(tmp_path / "open" / "eea_co2_cars" / "2018.sqlite") as conn:
        values = {r[0] for r in conn.execute("SELECT type_approval FROM rows")}
    assert values == {"E4"}                                                   # D0: stored upper case


# --- K3: the 2023+ years from the datahub CSV ------------------------------------------------------------------------------

API = "https://sdi.eea.europa.eu/catalogue/datahub/api/records/"
SERIES = "fa8b1229-3db6-495d-b18e-9c9b3267c02b"


def _csv_zip(rows: list[dict], header: list[str]) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in header])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("data.csv", text.getvalue())
    return out.getvalue()


def _year_rows(year: int, status: str) -> list[dict]:
    """20 per-vehicle rows (the recorded 2018 header): 2 CTS configurations, BMW 530e at several masses, an unknown
    make that is never kept."""
    rows = [H.eea_row(Year=year, Status=status, R=1, **{"M (kg)": 1734 + (i % 3)}) for i in range(8)]
    rows += [H.eea_row(Year=year, Status=status, Ve="X", R=2) for _ in range(4)]
    rows += [H.eea_row(Year=year, Status=status, Mk="BMW", Cn="530E", Va="JP91", Ve="DAW50000", R=1,
                       **{"Ec (cm3)": 1998, "Ep (KW)": 135, "Ewltp (g/km)": 47, "M (kg)": m})
             for m in (1935, 1935, 1935, 1990, 1935, 1935)]
    rows += [H.eea_row(Year=year, Status=status, Mk="STUDEBAKER", Cn="LARK") for _ in range(2)]
    return rows


def _datahub(files: dict[str, bytes], children: list[dict], links: dict[str, list[str]], index: dict[str, str]):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.startswith(API):
            uuid = url[len(API):].split("/")[0]
            if uuid == SERIES:
                return json.dumps({"children": [{"id": c["uuid"], "title": {"eng": c["title"]}} for c in children]
                                   }).encode()
            return json.dumps({"onlines": [{"url": {"eng": u}, "function": "download"} for u in links.get(uuid, [])]
                               }).encode()
        if url in index:
            return index[url].encode()
        if url in files:
            return files[url]
        raise RuntimeError(f"unexpected {url}")
    return fetch, calls


def test_the_datahub_records_are_read_from_the_series_and_a_listed_record():
    assert title_year_status("Monitoring of CO2 emissions from passenger cars, 2023 - Final data",
                             ds.datasets()["eea_co2_cars"]["csv_years"]["title_pattern"]) == (2023, "F")
    assert title_year_status("Monitoring of CO2 emissions from passenger cars, 2025 - Provisional data",
                             ds.datasets()["eea_co2_cars"]["csv_years"]["title_pattern"]) == (2025, "P")
    related = datahub_related({"relations": {"children": [{"id": "a", "title": {"eng": "x, 2024 - Final data"}}],
                                             "onlines": [{"url": {"eng": "https://sdi.eea.europa.eu/data/a"}}]}})
    assert related == {"children": [{"uuid": "a", "title": "x, 2024 - Final data"}],
                       "onlines": [{"url": "https://sdi.eea.europa.eu/data/a", "title": "", "function": "",
                                    "protocol": ""}]}
    policy = __import__("src.source_authority", fromlist=["fetch_allowed"])
    assert policy.fetch_allowed(API + SERIES + "/related")


def test_a_csv_year_is_grouped_like_the_discodata_path_into_a_shard(tmp_path):
    header = H.EEA_2018_HEADER
    rows_2023 = _year_rows(2023, "F")
    url = "https://sdi.eea.europa.eu/data/u2023/CO2_passenger_cars_2023.zip"
    index = {"https://sdi.eea.europa.eu/data/u2023": f'<a href="CO2_passenger_cars_2023.zip">zip</a>'
                                                    f'<a href="CO2_passenger_cars_2023.accdb.zip">access</a>'}
    fetch, calls = _datahub({url: _csv_zip(rows_2023, header)},
                            [{"uuid": "u2023", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - "
                                                        "Final data"}],
                            {"u2023": ["https://sdi.eea.europa.eu/data/u2023"]}, index)
    out = build_eea_csv(fetch, skip_years=set(), work=tmp_path)
    year = next(y for y in out["years"] if y["year"] == 2023)
    assert year["status"] == "built" and year["source"] == "datahub_csv" and year["url"] == url
    assert year["status_used"] == "F" and year["rows_read"] == 20 and year["by_make"] == {"BMW": 1, "CADILLAC": 2}
    assert any(c.endswith(f"{SERIES}/related") for c in calls)
    # the same rows through the DISCODATA path (server GROUP BY + eea_aggregate) give the same configurations
    disco = build_eea(_eea_fetch([(2023, "F")], {(2023, "F"): rows_2023}, []))["rows"]

    def comparable(rows):
        return sorted((json.dumps({k: (float(v) if isinstance(v, (int, float)) or str(v).replace(".", "", 1).isdigit()
                                       else v) for k, v in r.items() if k not in ("row_id",)}, sort_keys=True)
                       for r in rows))
    assert comparable(out["rows"]) == comparable(disco)
    bmw = next(r for r in out["rows"] if r["make"] == "BMW")
    assert (bmw["mass_running_order_kg"], bmw["mass_running_order_kg_min"], bmw["mass_running_order_kg_max"],
            bmw["registrations"]) == (1935.0, 1935.0, 1990.0, 6)
    ds.set_snapshot_dir(tmp_path / "open")
    try:
        shards = ds.write_shards("eea_co2_cars", out["rows"], {}, status_used={2023: "F"})
    finally:
        ds.set_snapshot_dir(None)
    assert [(s["year"], s["rows"]) for s in shards] == [(2023, 3)]


def test_a_csv_year_without_a_required_key_stops_only_itself(tmp_path):
    header = H.EEA_2018_HEADER
    no_make = [c for c in header if c != "Ep (KW)"]
    files = {"https://sdi.eea.europa.eu/data/a/2023.zip": _csv_zip(_year_rows(2023, "F"), no_make),
             "https://sdi.eea.europa.eu/data/b/2024.zip": _csv_zip(_year_rows(2024, "P"), header)}
    fetch, _ = _datahub(files, [{"uuid": "a", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - Final"},
                                {"uuid": "b", "title": "Monitoring of CO2 emissions from passenger cars, 2024 - "
                                                       "Provisional data"}],
                        {"a": ["https://sdi.eea.europa.eu/data/a/2023.zip"],
                         "b": ["https://sdi.eea.europa.eu/data/b/2024.zip"]}, {})
    out = build_eea_csv(fetch, skip_years=set(), work=tmp_path)
    years = {y["year"]: y for y in out["years"]}
    assert years[2023]["status"] == "stopped" and years[2023]["reason"] == "schema_mismatch"
    assert years[2023]["missing"][0]["key"] == "power_kw" and "Ep (KW)" not in years[2023]["live_header"]
    assert years[2024]["status"] == "built" and years[2024]["status_used"] == "P"
    assert {r["year"] for r in out["rows"]} == {2024}


def test_two_csv_links_are_ambiguous_and_a_discodata_year_is_never_rebuilt(tmp_path):
    fetch, calls = _datahub({}, [{"uuid": "a", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - Final"},
                                 {"uuid": "c", "title": "Monitoring of CO2 emissions from passenger cars, 2022 - Final"}],
                            {"a": ["https://sdi.eea.europa.eu/data/a/x.zip", "https://sdi.eea.europa.eu/data/a/y.csv"],
                             "c": ["https://sdi.eea.europa.eu/data/c/z.zip"]}, {})
    out = build_eea_csv(fetch, skip_years={2022}, work=tmp_path)
    years = {y["year"]: y for y in out["years"]}
    assert years[2023]["status"] == "stopped" and years[2023]["reason"] == "ambiguous_download"
    assert years[2022]["status"] == "skipped" and not any("z.zip" in c for c in calls)
    assert not out["rows"]


def test_the_listed_2025_record_and_final_preferred_over_provisional(tmp_path):
    children = [{"uuid": "p23", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - Provisional data"},
                {"uuid": "f23", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - Final data"}]
    links = {"f23": ["https://sdi.eea.europa.eu/data/f23/f.zip"],
             "b4044b06-2e6b-4f8e-a6e6-66e0e98bb0dd": ["https://sdi.eea.europa.eu/data/p25/p.zip"]}
    files = {"https://sdi.eea.europa.eu/data/f23/f.zip": _csv_zip(_year_rows(2023, "F"), H.EEA_2018_HEADER),
             "https://sdi.eea.europa.eu/data/p25/p.zip": _csv_zip(_year_rows(2025, "P"), H.EEA_2018_HEADER)}
    fetch, calls = _datahub(files, children, links, {})
    out = build_eea_csv(fetch, skip_years=set(), work=tmp_path)
    years = {y["year"]: y for y in out["years"]}
    assert years[2023]["record"] == "f23" and years[2023]["status_used"] == "F"
    assert years[2025]["record"] == "b4044b06-2e6b-4f8e-a6e6-66e0e98bb0dd" and years[2025]["status"] == "built"
    assert not any("/p23/" in c for c in calls)


def test_build_dataset_writes_discodata_and_csv_years_as_shards(tmp_path):
    rows = {(2018, "F"): [H.eea_row(Year=2018)]}
    disco = _eea_fetch([(2018, "F")], rows, [])
    url = "https://sdi.eea.europa.eu/data/f23/f.zip"
    datahub, _ = _datahub({url: _csv_zip(_year_rows(2023, "F"), H.EEA_2018_HEADER)},
                          [{"uuid": "f23", "title": "Monitoring of CO2 emissions from passenger cars, 2023 - Final"}],
                          {"f23": [url]}, {})

    def fetch(u: str) -> bytes:
        return datahub(u) if "sdi.eea.europa.eu" in u else disco(u)
    ds.set_snapshot_dir(tmp_path / "open")
    try:
        built = build_dataset("eea_co2_cars", fetch)
    finally:
        ds.set_snapshot_dir(None)
    assert built["status"] == "built"
    assert sorted(p.name for p in (tmp_path / "open" / "eea_co2_cars").iterdir()) == ["2018.sqlite", "2023.sqlite"]
    years = {y["year"]: y for y in built["years"]}
    assert years[2018].get("mode") == "per_make" and years[2023]["source"] == "datahub_csv"
    assert built["csv_discovery"] and built["csv_discovery"][0]["kind"] == "series"

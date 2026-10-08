"""T1 / T2: the DISCODATA per-year tables named in the EEA datahub yearly records (2022 F, 2023, 2024, 2025). No
network: the datahub answers are recorded-shape record JSON (ISO 19139 converted to JSON, as tests/
test_open_data_datahub.py) whose lineage carries the reviewer's 2023 Final example query verbatim; DISCODATA is a fake
that answers per table ([latest] and the year tables) with the E1 grouping of tests/test_open_data_builds.py."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import datasets as ds
from src.open_data.build import (build_dataset, build_eea_all, eea_datahub_discover, eea_response,
                                 eea_year_columns, record_tables, resolve_map, validate_year_table)

from test_open_data_builds import eea_grouped
from test_open_data_datahub import P2025, SERIES, csv_zip, datahub, iso_record, related
from test_open_data_repo import _script

LATEST = "[CO2Emission].[latest].[co2cars]"
T2022F = "[CO2Emission].[latest].[co2cars_2022Fv20]"
T2023F = "[CO2Emission].[latest].[co2cars_2023Fv28]"
T2024F = "[CO2Emission].[latest].[co2cars_2024Fv30]"
T2025P = "[CO2Emission].[latest].[co2cars_2025Pv31]"
R2022F = "992616f8-158f-4ecc-b978-814b81629db6"
R2023F = "87fd2bce-6ad5-46d8-af41-f27cfd2e45a8"
R2024F = "5018ec17-2348-4c92-8761-6f2377bbd1c0"
# the reviewer's 2023 Final record text (2026-10-08), verbatim
LINEAGE_2023 = ("DISCODATA table name: [CO2Emission].[latest].[co2cars_2023Fv28]. Example query: Select top 1000 * from "
                "[CO2Emission].[latest].[co2cars_2023Fv28] where year = 2023 and status = 'F'")


@pytest.fixture(autouse=True)
def _reset():
    yield
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def yearly_record(title: str, lineage: str, resources: list[dict] = ()) -> dict:
    """A yearly datahub record: the identification title and the lineage statement (dataQualityInfo) that names the
    DISCODATA table."""
    record = iso_record(title, list(resources))
    record["gmd:MD_Metadata"]["gmd:dataQualityInfo"] = {"gmd:DQ_DataQuality": {"gmd:lineage": {"gmd:LI_Lineage": {
        "gmd:statement": {"gco:CharacterString": lineage}}}}}
    return record


def title(year: int, status: str) -> str:
    return f"Monitoring of CO2 emissions from passenger cars, {year} - {'Final' if status == 'F' else 'Provisional'}"


def lineage(table: str, year: int, status: str) -> str:
    return f"DISCODATA table name: {table}. Example query: Select top 1000 * from {table} where year = {year} and " \
           f"status = '{status}'"


def hub_for(records: dict[str, tuple[int, str, str]], files: dict[str, bytes] | None = None,
            downloads: dict[str, str] | None = None):
    """The datahub: the series lists the yearly records {uuid: (year, status, lineage text)} as children."""
    meta = {SERIES: iso_record("Monitoring of CO2 emissions from passenger cars Regulation (EU) 2019/631", [])}
    for uuid, (year, status, text) in records.items():
        links = [{"url": downloads[uuid], "protocol": "WWW:DOWNLOAD-1.0-http--download"}] \
            if downloads and uuid in downloads else []
        meta[uuid] = yearly_record(title(year, status), text, links)
    relations = {SERIES: related(children=[(u, title(y, s)) for u, (y, s, _) in records.items()])}
    return datahub(meta, relations, files=files)


def discodata(latest: dict[tuple[int, str], list[dict]], tables: dict[str, list[dict]], queries: list[str]):
    """A fake DISCODATA answering per table: [latest] (its DISTINCT year, status) and each year table (only the names
    given exist: any other is the server's 'Invalid object name' error)."""
    def fetch(url: str) -> bytes:
        query = parse_qs(urlparse(url).query)["query"][0]
        queries.append(query)
        assert len(url.split("query=", 1)[1].split("&", 1)[0]) <= 1800
        table = re.search(r" FROM (\[CO2Emission\]\.\[latest\]\.\[[^\]]+\])", query).group(1)
        if table == LATEST:
            by_pair = latest
        elif table in tables:
            by_pair = {}
            for row in tables[table]:
                by_pair.setdefault((int(row["Year"]), str(row["Status"])), []).append(row)
        else:
            return json.dumps({"errors": f"Invalid object name '{table}'."}).encode()
        if query.startswith("SELECT DISTINCT") and "WHERE" not in query:
            return json.dumps({"results": [{"Year": y, "Status": s} for y, s in by_pair]}).encode()
        if query.startswith("SELECT TOP 1") and "WHERE" not in query:
            first = next((rows[0] for rows in by_pair.values() if rows), None)
            return json.dumps({"results": [first or H.eea_row()] if table == LATEST else [first] if first
                               else []}).encode()
        year = int(query.split("[Year] = ")[1].split()[0])
        status = query.split("[Status] = '")[1][0]
        rows = by_pair.get((year, status)) or []
        if query.startswith("SELECT TOP 1"):
            return json.dumps({"results": rows[:1]}).encode()
        if query.startswith("SELECT DISTINCT [Mk]"):
            return json.dumps({"results": [{"Mk": m} for m in sorted({r["Mk"] for r in rows})]}).encode()
        return json.dumps({"results": eea_grouped(rows, query)}).encode()
    return fetch


def row(year: int, status: str, header=H.EEA_2018_HEADER, **values) -> dict:
    return H.eea_row(header, Year=year, Status=status, **{"Ewltp (g/km)": 150, **values})


def fetch_of(hub, disco):
    def fetch(url: str) -> bytes:
        return hub(url) if "sdi.eea.europa.eu" in url else disco(url)
    return fetch


# --- T1: the table names from the records ------------------------------------------------------------------------------

def test_the_2023_record_lineage_names_co2cars_2023fv28_and_year_and_status_agree():
    record = yearly_record(title(2023, "F"), LINEAGE_2023)
    found = record_tables(json.dumps(record), 2023, "F")
    assert found["found"] == [T2023F] and found["rejected"] == []
    assert found["accepted"] == [{"table": T2023F, "year": 2023, "status": "F", "version": 28}]
    hub, _ = hub_for({R2023F: (2023, "F", LINEAGE_2023)})
    choices, reports = eea_datahub_discover(hub, ds.datasets()["eea_co2_cars"]["csv_years"])
    block = choices[2023]["tables"]["F"]
    assert block["chosen"] == T2023F and block["record"] == R2023F and block["versions"] == [28]
    assert next(r for r in reports if r.get("uuid") == R2023F)["discodata_tables"]["found"] == [T2023F]
    summary = next(r for r in reports if r.get("kind") == "summary")
    assert summary["years"]["2023"]["tables"]["F"]["chosen"] == T2023F


def test_a_name_that_only_resembles_the_pattern_is_never_taken():
    text = ("[CO2Emission].[latest].[co2cars] [co2cars_2023Fv28] [CO2Emission].[latest].[co2cars_2023Fv] "
            "[CO2Emission].[latest].[co2cars_2023Xv28] [co2emission].[latest].[co2cars_2023Fv28]")
    assert record_tables(text, 2023, "F")["found"] == []


def test_a_table_whose_year_or_status_disagrees_with_the_record_is_rejected_with_the_reason():
    text = lineage(T2024F, 2024, "F") + " " + lineage("[CO2Emission].[latest].[co2cars_2023Pv27]", 2023, "P")
    found = record_tables(text, 2023, "F")
    assert found["accepted"] == []
    reasons = {r["table"]: r["reason"] for r in found["rejected"]}
    assert reasons == {T2024F: "table_record_mismatch: table 2024F vs record 2023F",
                       "[CO2Emission].[latest].[co2cars_2023Pv27]": "table_record_mismatch: table 2023P vs record 2023F"}
    hub, _ = hub_for({R2023F: (2023, "F", lineage(T2024F, 2024, "F"))})
    choices, _ = eea_datahub_discover(hub, ds.datasets()["eea_co2_cars"]["csv_years"])
    block = choices[2023]["tables"]["F"]
    assert block["chosen"] is None and block["rejected"][0]["reason"].startswith("table_record_mismatch")


def test_two_versions_of_one_year_and_status_choose_the_higher_v_and_record_both():
    text = lineage("[CO2Emission].[latest].[co2cars_2023Fv9]", 2023, "F") + " " + LINEAGE_2023
    hub, _ = hub_for({R2023F: (2023, "F", text)})
    choices, _ = eea_datahub_discover(hub, ds.datasets()["eea_co2_cars"]["csv_years"])
    block = choices[2023]["tables"]["F"]
    assert block["chosen"] == T2023F                                  # v28 over v9 (numeric, not text order)
    assert block["versions"] == [9, 28]
    assert set(block["found"]) == {T2023F, "[CO2Emission].[latest].[co2cars_2023Fv9]"}


# --- T1: validation before use -----------------------------------------------------------------------------------------

def test_the_2025_top_1_header_resolves_without_an_ambiguous_key_and_ech_rlfi_are_ignored():
    cfg = ds.datasets()["eea_co2_cars"]
    sample = row(2025, "P", H.EEA_2025P_HEADER, R=1, ID=12345, Ech="ECH", RLFI="RLFI")
    fetch = discodata({}, {T2025P: [sample]}, [])
    check = validate_year_table(lambda q: eea_response(fetch("https://x/sql?" + _encode(q))), T2025P, 2025, cfg)
    assert check["ok"] is True and check["reason"] is None and check["row_status"] == "P"
    assert check["year_column"] == "Year" and check["status_column"] == "Status"
    resolved = resolve_map(H.EEA_2025P_HEADER, eea_year_columns(cfg, 2025))
    assert resolved["missing"] == [] and resolved["ambiguous"] == []
    assert not {"Ech", "RLFI"} & set(resolved["mapping"].values())
    assert resolved["absent"] == ["electric_range_km"]                # no range column in any DISCODATA table


def _encode(query: str) -> str:
    from urllib.parse import quote
    return "query=" + quote(query)


def test_a_table_whose_row_year_differs_from_the_table_year_or_whose_header_misses_a_key_fails_validation():
    cfg = ds.datasets()["eea_co2_cars"]
    wrong = discodata({}, {T2023F: [row(2022, "F")]}, [])
    check = validate_year_table(lambda q: eea_response(wrong("https://x/sql?" + _encode(q))), T2023F, 2023, cfg)
    assert check["ok"] is False and check["reason"].startswith("year_mismatch")
    no_make = [c for c in H.EEA_2018_HEADER if c != "Mk"]
    bad = discodata({}, {T2023F: [row(2023, "F", no_make)]}, [])
    check = validate_year_table(lambda q: eea_response(bad("https://x/sql?" + _encode(q))), T2023F, 2023, cfg)
    assert check["reason"] == "schema_mismatch" and check["missing"][0]["key"] == "make"
    missing = discodata({}, {}, [])
    check = validate_year_table(lambda q: eea_response(missing("https://x/sql?" + _encode(q))), T2023F, 2023, cfg)
    assert check["ok"] is False and check["reason"].startswith("query_error")


# --- T2: which years come from where ------------------------------------------------------------------------------------

def _latest():
    return {(2021, "F"): [row(2021, "F")], (2022, "P"): [row(2022, "P", Ve="PROVISIONAL")]}


def test_2022_is_built_from_a_validated_final_table_instead_of_the_provisional_latest_rows(tmp_path):
    queries: list[str] = []
    hub, _ = hub_for({R2022F: (2022, "F", lineage(T2022F, 2022, "F"))})
    disco = discodata(_latest(), {T2022F: [row(2022, "F", Ve="FINAL"), row(2022, "F", Ve="FINAL2")]}, queries)
    ds.set_snapshot_dir(tmp_path / "open")
    built = build_dataset("eea_co2_cars", fetch_of(hub, disco))
    assert built["status"] == "built"
    report = next(y for y in built["years"] if y["year"] == 2022 and y.get("status") == "built")
    assert report["status_used"] == "F" and report["table"] == T2022F
    assert report["discodata_table"]["validation"] == [
        {"status": "F", "record": R2022F, "table": T2022F, "ok": True, "reason": None, "row_status": "F"}]
    assert report["discodata_table"]["latest_status"] == "P"
    shard = next(s for s in built["shards"] if s["year"] == 2022)
    assert shard["status_used"] == "F"
    assert not any("PROVISIONAL" in q for q in queries) and not any(
        f"FROM {LATEST} WHERE [Year] = 2022" in q for q in queries)       # [latest] P never read for 2022
    assert any(f"FROM {T2022F} WHERE [Year] = 2022 AND [Status] = 'F' AND [Mk]=" in q for q in queries)
    assert {r["version"] for r in _shard_rows(tmp_path, 2022)} == {"FINAL", "FINAL2"}
    assert next(s for s in built["shards"] if s["year"] == 2021)["status_used"] == "F"


def test_2022_stays_provisional_from_latest_without_a_final_table(tmp_path):
    hub, _ = hub_for({})
    ds.set_snapshot_dir(tmp_path / "open")
    built = build_dataset("eea_co2_cars", fetch_of(hub, discodata(_latest(), {}, [])))
    report = next(y for y in built["years"] if y["year"] == 2022 and y.get("status") == "built")
    assert report["status_used"] == "P" and "table" not in report and "discodata_table" not in report
    assert next(s for s in built["shards"] if s["year"] == 2022)["status_used"] == "P"
    assert {r["version"] for r in _shard_rows(tmp_path, 2022)} == {"PROVISIONAL"}


def test_2022_falls_back_to_latest_when_its_final_table_fails_validation(tmp_path):
    hub, _ = hub_for({R2022F: (2022, "F", lineage(T2022F, 2022, "F"))})
    disco = discodata(_latest(), {T2022F: [row(2021, "F", Ve="FINAL")]}, [])       # the row's year is not 2022
    ds.set_snapshot_dir(tmp_path / "work")
    built = build_eea_all(fetch_of(hub, disco))
    report = next(y for y in built["years"] if y["year"] == 2022 and y.get("status") == "built")
    assert report["status_used"] == "P" and report["table"] == LATEST
    assert report["discodata_table"]["validation"][0]["reason"].startswith("year_mismatch")


def test_2023_2024_from_their_final_tables_and_2025_from_the_provisional_one():
    queries: list[str] = []
    hub, _ = hub_for({R2023F: (2023, "F", LINEAGE_2023), R2024F: (2024, "F", lineage(T2024F, 2024, "F")),
                      P2025: (2025, "P", lineage(T2025P, 2025, "P"))})
    tables = {T2023F: [row(2023, "F", Mk="BMW", Cn="X1")], T2024F: [row(2024, "F")],
              T2025P: [row(2025, "P", H.EEA_2025P_HEADER, R=1, ID=1, Ech=1, RLFI=2)]}
    built = build_eea_all(fetch_of(hub, discodata(_latest(), tables, queries)))
    reports = {y["year"]: y for y in built["years"] if y.get("status") == "built"}
    assert {y: (r["status_used"], r.get("table")) for y, r in reports.items() if y >= 2023} == {
        2023: ("F", T2023F), 2024: ("F", T2024F), 2025: ("P", T2025P)}
    assert reports[2023]["discodata_table"]["latest_status"] is None
    assert {r["year"] for r in built["rows"]} >= {2023, 2024, 2025}
    assert "electric_range_km" in reports[2025]["absent_columns"]
    assert not any(y.get("source") == "datahub_csv" and y["year"] in (2023, 2024, 2025) for y in built["years"])


def test_2025_prefers_a_validated_final_table_over_the_provisional_one():
    t2025f = "[CO2Emission].[latest].[co2cars_2025Fv33]"
    r2025f = "aaaaaaaa-0000-4000-8000-000000002025"
    hub, _ = hub_for({r2025f: (2025, "F", lineage(t2025f, 2025, "F")), P2025: (2025, "P", lineage(T2025P, 2025, "P"))})
    tables = {t2025f: [row(2025, "F", Ve="FINAL")], T2025P: [row(2025, "P", Ve="PROV")]}
    built = build_eea_all(fetch_of(hub, discodata(_latest(), tables, [])))
    report = next(y for y in built["years"] if y["year"] == 2025 and y.get("status") == "built")
    assert report["status_used"] == "F" and report["table"] == t2025f
    assert {r["version"] for r in built["rows"] if r["year"] == 2025} == {"FINAL"}


def test_a_year_table_that_fails_validation_stops_only_that_year():
    hub, _ = hub_for({R2023F: (2023, "F", LINEAGE_2023), R2024F: (2024, "F", lineage(T2024F, 2024, "F"))})
    tables = {T2023F: [row(2023, "F", [c for c in H.EEA_2018_HEADER if c != "Ep (KW)"])], T2024F: [row(2024, "F")]}
    built = build_eea_all(fetch_of(hub, discodata(_latest(), tables, [])))
    stopped = next(y for y in built["years"] if y["year"] == 2023 and y.get("source") == "discodata_year_table")
    assert stopped["status"] == "stopped" and stopped["reason"] == "table_validation_failed"
    assert stopped["discodata_table"]["validation"][0]["reason"] == "schema_mismatch"
    assert any(y["year"] == 2024 and y.get("status") == "built" for y in built["years"])


def test_the_csv_path_is_not_called_for_a_year_with_a_validated_table(tmp_path):
    url = "https://sdi.eea.europa.eu/datashare/s/2023f/download"
    hub, calls = hub_for({R2023F: (2023, "F", LINEAGE_2023)}, files={url: csv_zip([], H.EEA_2018_HEADER)},
                         downloads={R2023F: url})
    downloads: list[str] = []

    def download(u, target):
        downloads.append(u)
        raise AssertionError("the datahub CSV must not be downloaded for a year with a validated table")
    ds.set_snapshot_dir(tmp_path / "work")
    built = build_eea_all(fetch_of(hub, discodata(_latest(), {T2023F: [row(2023, "F")]}, [])), download=download)
    assert downloads == [] and url not in calls
    assert not any(y.get("source") == "datahub_csv" and y["year"] == 2023 for y in built["years"])
    assert next(y for y in built["years"] if y["year"] == 2023)["table"] == T2023F


def test_range_join_years_are_empty_and_no_range_join_is_attempted(tmp_path):
    cfg = ds.datasets()["eea_co2_cars"]
    assert cfg["csv_years"]["range_join_years"] == []
    assert "no DISCODATA table has a range column" in cfg["csv_years"]["_range_join"]
    url = "https://sdi.eea.europa.eu/datashare/s/2021f/download"
    r2021 = "0172c621-9e03-4756-ac5f-47cc3e241201"
    hub, calls = hub_for({r2021: (2021, "F", "")}, downloads={r2021: url})
    ds.set_snapshot_dir(tmp_path / "work")
    built = build_eea_all(fetch_of(hub, discodata(_latest(), {}, [])), download=lambda u, t: (_ for _ in ()).throw(
        AssertionError("no CSV download: no range join year")))
    assert not any(y.get("mode") in ("range_join", "range_join_source") for y in built["years"])
    assert url not in calls
    assert all("electric_range_km" not in r for r in built["rows"])


def test_years_up_to_2021_never_read_a_year_table():
    t2021 = "[CO2Emission].[latest].[co2cars_2021Fv18]"
    hub, _ = hub_for({"0172c621-9e03-4756-ac5f-47cc3e241201": (2021, "F", lineage(t2021, 2021, "F"))})
    queries: list[str] = []
    built = build_eea_all(fetch_of(hub, discodata(_latest(), {t2021: [row(2021, "F", Ve="T")]}, queries)))
    assert not any(t2021 in q for q in queries)
    report = next(y for y in built["years"] if y["year"] == 2021 and y.get("status") == "built")
    assert "table" not in report and report["status_used"] == "F"


def test_the_build_summary_shows_the_table_chosen_and_the_validation_per_year():
    build = _script("build_open_data")
    hub, _ = hub_for({R2022F: (2022, "F", lineage(T2022F, 2022, "F")), R2023F: (2023, "F", LINEAGE_2023)})
    built = build_eea_all(fetch_of(hub, discodata(_latest(), {T2022F: [row(2022, "F")], T2023F: [row(2023, "F")]},
                                                  [])))
    lines = "\n".join(build.datahub_section({"eea_co2_cars": {"status": "built", "years": built["years"],
                                                                "csv_discovery": built["csv_discovery"]}}))
    assert "DISCODATA year tables (T1 / T2)" in lines
    assert f"| 2022 | F | {R2022F} | {T2022F} | {T2022F} | — | ok | {T2022F} (built) |" in lines
    assert f"| 2023 | F | {R2023F} | {T2023F} | {T2023F} | — | ok | {T2023F} (built) |" in lines


def _shard_rows(tmp_path, year: int) -> list[dict]:
    import sqlite3

    path = tmp_path / "open" / "eea_co2_cars" / f"{year}.sqlite"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM rows")]

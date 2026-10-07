"""The EEA datahub discovery through record metadata (D1), the electric range from the datahub CSV (D2), text identity
normalization (D0) and the determinism check (D3). No network: DISCODATA is the fake of tests/test_open_data_builds.py;
the datahub answers are recorded-shape fixtures (an ISO 19139 record converted to JSON, GeoNetwork `/related` answers)
built here, with the record uuids and link kinds of build run 37690665130 (automation PR #68)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import random
import zipfile
from pathlib import Path

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import build as B
from src.open_data import datasets as ds
from src.open_data.build import (build_dataset, build_eea, build_eea_all, classify_resource, csv_header_from_prefix,
                                 eea_csv_year, eea_datahub_discover, range_join, record_metadata)
from src.open_data.match import type_code_match, type_code_rule

from test_open_data_builds import _eea_fetch
from test_open_data_repo import _script

API = "https://sdi.eea.europa.eu/catalogue/datahub/api/records/"
SERIES = "fa8b1229-3db6-495d-b18e-9c9b3267c02b"
P2025 = "b4044b06-2e6b-4f8e-a6e6-66e0e98bb0dd"
CHILD_A = "3e1600f1-ef3f-4d52-9431-569a8920573d"
CHILD_B = "d3f32b6f-d06d-4a00-82d6-53e9a4cbcf13"
HOSTS = ["eea.europa.eu", "data.europa.eu"]
LIMIT = 50 * 1024 * 1024
RANGE_HEADER = H.EEA_2018_HEADER + ["Electric range (km)"]


@pytest.fixture(autouse=True)
def _reset():
    yield
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def iso_record(title: str | None, resources: list[dict], *, begin: str | None = None, end: str | None = None,
               contact: str = "https://www.eea.europa.eu") -> dict:
    """An ISO 19139 record as the GeoNetwork record API converts it to JSON (namespaced keys, CharacterString / URL
    wrappers); a contact's website is never a resource."""
    ident: dict = {"gmd:citation": {"gmd:CI_Citation": {"gmd:title": {"gco:CharacterString": title}}}} if title else {}
    if begin:
        ident["gmd:extent"] = {"gmd:EX_Extent": {"gmd:temporalElement": {"gmd:EX_TemporalExtent": {"gmd:extent": {
            "gml:TimePeriod": {"gml:beginPosition": begin, "gml:endPosition": end}}}}}}
    return {"gmd:MD_Metadata": {
        "gmd:contact": {"gmd:CI_ResponsibleParty": {"gmd:contactInfo": {"gmd:CI_Contact": {"gmd:onlineResource": {
            "gmd:CI_OnlineResource": {"gmd:linkage": {"gmd:URL": contact}}}}}}},
        "gmd:identificationInfo": {"gmd:MD_DataIdentification": ident},
        "gmd:distributionInfo": {"gmd:MD_Distribution": {"gmd:transferOptions": [{"gmd:MD_DigitalTransferOptions": {
            "gmd:onLine": [{"gmd:CI_OnlineResource": {
                "gmd:linkage": {"gmd:URL": r["url"]},
                **({"gmd:protocol": {"gco:CharacterString": r["protocol"]}} if r.get("protocol") else {}),
                **({"gmd:name": {"gco:CharacterString": r["name"]}} if r.get("name") else {})}}
                for r in resources]}}]}}}}


def related(children: list[tuple[str, str]] = (), links: list[str] = ()) -> dict:
    """A GeoNetwork `/related` answer: children (uuid, title) and online links."""
    return {"children": [{"id": u, "title": {"eng": t}} for u, t in children],
            "onlines": [{"url": {"eng": u}, "title": {"eng": ""}} for u in links]}


def datahub(records: dict[str, dict], relations: dict[str, dict], files: dict[str, bytes] | None = None,
            pages: dict[str, str] | None = None):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.startswith(API):
            uuid, _, rest = url[len(API):].partition("/")
            if rest == "related":
                return json.dumps(relations.get(uuid) or {}).encode()
            if uuid in records:
                return json.dumps(records[uuid]).encode()
            raise RuntimeError(f"404 {url}")
        if pages and url in pages:
            return pages[url].encode()
        if files and url in files:
            return files[url]
        raise RuntimeError(f"unexpected {url}")
    return fetch, calls


def csv_zip(rows: list[dict], header: list[str]) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in header])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CO2_passenger_cars_2021.csv", text.getvalue())
    return out.getvalue()


def _csv_cfg(**extra) -> dict:
    return {**ds.datasets()["eea_co2_cars"]["csv_years"], **extra}


# --- D0: text identity normalization -----------------------------------------------------------------------------------

def test_case_and_space_variants_of_one_configuration_are_one_row_with_summed_registrations(tmp_path):
    rows = [H.eea_row(Year=2021, Ft="PETROL", R=1, **{"M (kg)": 1500}),
            H.eea_row(Year=2021, Ft="Petrol", R=5, **{"M (kg)": 1600}),
            H.eea_row(Year=2021, Ft="PETROL  ", R=1, **{"M (kg)": 1700})]
    queries: list[str] = []
    built = build_eea(_eea_fetch([(2021, "F")], {(2021, "F"): rows}, queries))
    assert len(built["rows"]) == 1                                  # the fake GROUP BY returned three server rows
    (row,) = built["rows"]
    assert row["fuel"] == "PETROL" and row["registrations"] == 7
    # weighted median over the merged inputs (1500 x1, 1600 x5, 1700 x1), never an average of the variants' medians
    assert (row["mass_running_order_kg"], row["mass_running_order_kg_min"], row["mass_running_order_kg_max"]) == \
        (1600.0, 1500.0, 1700.0)
    shard = ds.write_shards("eea_co2_cars", built["rows"], {}, folder=tmp_path)[0]["path"]
    stored, _ = ds.read_shard(shard)
    assert [(r["fuel"], r["model"], r["variant"]) for r in stored] == [("PETROL", "CTS", "AL")]


def test_the_shard_writer_normalizes_text_identity_with_nfkc():
    assert ds.norm_text("  ２１８i  Gran\tTourer ") == "218I GRAN TOURER"
    assert ds.norm_text(None) is None and ds.norm_text("   ") is None and ds.norm_text(47.0) == 47.0


def test_a_target_type_code_matches_a_lower_case_va():
    row = {"make": "BMW", "variant": " wx31 "}
    assert type_code_rule("exact_va", "WX31", row) and type_code_rule("exact_va", "ＷＸ３１", row)
    assert type_code_match([row], "wx31")["status"] == "match"


def _randomized(rows: list[dict], seed: int) -> list[dict]:
    rnd = random.Random(seed)

    def vary(text: str) -> str:
        text = "".join(c.upper() if rnd.random() < 0.5 else c.lower() for c in text)
        return text + " " * rnd.randint(0, 3)
    return [{**r, **{k: vary(str(r[k])) for k in ("Cn", "Ft", "Va", "Ve", "T") if r.get(k)}} for r in rows]


def test_randomized_casing_of_the_same_input_gives_identical_shard_bytes(tmp_path, monkeypatch):
    base = [H.eea_row(Year=2021, Ve=f"V{n % 4}", R=1 + n, **{"M (kg)": 1500 + 10 * n, "W (mm)": 2800})
            for n in range(12)]
    build = _script("build_open_data")
    shas = []
    for seed in (1, 2):
        fetch = _eea_fetch([(2021, "F")], {(2021, "F"): _randomized(base, seed)}, [])
        monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None, f=fetch: build_dataset(name, f))
        _, manifest, _ = build.run(["eea_co2_cars"], tmp_path / f"open{seed}", tmp_path / f"work{seed}", {}, LIMIT,
                                   None)
        shas.append([(s["file"], s["sha256"], s["rows"]) for s in manifest["datasets"]["eea_co2_cars"]["shards"]])
    assert shas[0] == shas[1] and shas[0][0][2] == 4                  # four versions, whatever the spelling


def test_the_other_datasets_store_normalized_text_identity():
    from src.open_data.build import compact

    out = compact("epa_fueleconomy", {"rows": [{"make": "Ford", "model": " F150  Raptor", "transmission":
                                                "Automatic (S10)", "drive": "4-Wheel Drive", "year": 2020}]})
    (row,) = out["rows"]
    assert (row["make"], row["model"], row["transmission"], row["drive"]) == \
        ("FORD", "F150 RAPTOR", "AUTOMATIC (S10)", "4-WHEEL DRIVE")
    from src.open_data.match import transmission_of
    assert transmission_of(row["transmission"]) == {"class": "automatic", "gears": 10, "gearbox_type": "automatic"}


# --- D1: discovery through record metadata ------------------------------------------------------------------------------

def test_record_metadata_reads_the_title_the_distribution_and_never_a_contact():
    meta = record_metadata(iso_record("Monitoring of CO2 emissions from passenger cars, 2024 - Final data", [
        {"url": "https://sdi.eea.europa.eu/datashare/s/abc/download", "protocol": "WWW:DOWNLOAD-1.0-http--download",
         "name": "CO2 cars 2024 (zip)"}], begin="2024-01-01", end="2024-12-31"))
    assert meta["title"] == "Monitoring of CO2 emissions from passenger cars, 2024 - Final data"
    assert meta["temporal_year"] == 2024
    assert [r["url"] for r in meta["resources"]] == ["https://sdi.eea.europa.eu/datashare/s/abc/download"]
    assert meta["resources"][0]["protocol"] == "WWW:DOWNLOAD-1.0-http--download"


def test_a_download_in_a_related_records_metadata_is_discovered_with_its_title_year_and_status():
    """The PR #68 case: the 2025 record links the series and two records by their record-API URLs, landing pages, a
    PDF, an XLSX table definition, a PNG and a DOI; the data file is in a related record's own metadata."""
    zip_url = "https://sdi.eea.europa.eu/datashare/s/2025p/download"
    links_2025 = [f"{API}{SERIES}", f"{API}{CHILD_A}", f"{API}{CHILD_B}", "https://co2cars.apps.eea.europa.eu/",
                  "https://discodata.eea.europa.eu/",
                  "https://www.eea.europa.eu/en/analysis/indicators/co2-performance-of-new-passenger",
                  f"https://sdi.eea.europa.eu/catalogue/api/records/{P2025}/attachments/metadata_2025P.pdf",
                  f"https://sdi.eea.europa.eu/catalogue/api/records/{P2025}/attachments/Table-definition-cars-2025.xlsx",
                  f"https://doi.org/10.2909/{P2025}",
                  f"https://sdi.eea.europa.eu/catalogue/api/records/{P2025}/attachments/Cars.png"]
    records = {
        SERIES: iso_record("Monitoring of CO2 emissions from passenger cars (series)", []),
        P2025: iso_record(None, []),
        CHILD_A: iso_record("CO2 emissions from new passenger cars, 2025 - Provisional data", [
            {"url": zip_url, "protocol": "WWW:DOWNLOAD-1.0-http--download", "name": "zip"},
            {"url": "https://sdi.eea.europa.eu/data/Table-definition.xlsx", "protocol": "WWW:DOWNLOAD-1.0-http--download"}]),
        CHILD_B: iso_record("CO2 emissions from new passenger cars - provisional data",
                            [{"url": "https://www.eea.europa.eu/en/datahub", "protocol": "WWW:LINK"}],
                            begin="2025-01-01", end="2025-12-31")}
    relations = {SERIES: related(), P2025: related(links=links_2025)}
    fetch, calls = datahub(records, relations, pages={u: "<html>no files</html>" for u in links_2025})
    choices, reports = eea_datahub_discover(fetch, _csv_cfg())
    assert choices[2025]["url"] == zip_url and choices[2025]["status"] == "P"
    assert choices[2025]["record"] == CHILD_A
    assert sorted(choices[2025]["records"]) == sorted([P2025, CHILD_A, CHILD_B])
    by_uuid = {r["uuid"]: r for r in reports if r.get("uuid")}
    assert by_uuid[CHILD_A]["year_basis"] == "title" and by_uuid[CHILD_B]["year_basis"] == "temporal_extent"
    assert by_uuid[P2025]["year_basis"] == "configured"           # no title: the reviewer's listed year / status
    classes = {r["url"]: r["class"] for r in by_uuid[P2025]["resources"]}
    assert classes[f"{API}{CHILD_A}"] == "record" and classes[f"https://doi.org/10.2909/{P2025}"] == "excluded"
    assert all(classes[u] == "excluded" for u in links_2025 if u.endswith((".pdf", ".xlsx", ".png")))
    assert classes["https://co2cars.apps.eea.europa.eu/"] == "landing"
    assert f"{API}{CHILD_A}" in calls and f"{API}{CHILD_A}/related" in calls      # metadata + related, in JSON
    summary = reports[-1]
    assert summary["kind"] == "summary" and summary["metadata_endpoint"].endswith("(Accept: application/json)")
    assert not any(c.startswith("https://doi.org") for c in calls)            # never fetched


def test_classification_never_chooses_a_table_definition_a_document_an_image_or_a_doi():
    for url in ("https://sdi.eea.europa.eu/x/Table-definition-cars-2025-Provisional.xlsx",
                "https://sdi.eea.europa.eu/x/metadata.pdf", "https://sdi.eea.europa.eu/x/Cars.png",
                "https://doi.org/10.2909/b4044b06", "https://sdi.eea.europa.eu/x/cars.accdb.zip"):
        assert classify_resource({"url": url, "protocol": "WWW:DOWNLOAD-1.0-http--download"}, HOSTS) == "excluded"
    assert classify_resource({"url": "https://example.org/cars.zip"}, HOSTS) == "excluded"            # host
    assert classify_resource({"url": "https://data.europa.eu/x/cars.csv.gz"}, HOSTS) == "download"
    assert classify_resource({"url": "https://sdi.eea.europa.eu/s/x/download", "format": "text/csv"}, HOSTS) == \
        "download"
    assert classify_resource({"url": "https://www.eea.europa.eu/en/datahub"}, HOSTS) == "landing"
    assert classify_resource({"url": f"{API}{CHILD_A}"}, HOSTS) == "record"


def test_two_zips_for_one_year_without_file_pattern_stop_that_year_with_both_listed():
    zips = ["https://sdi.eea.europa.eu/data/2024/a.zip", "https://sdi.eea.europa.eu/data/2024/b.zip"]
    records = {SERIES: iso_record("series", []),
               CHILD_A: iso_record("Monitoring of CO2 emissions from passenger cars, 2024 - Final data",
                                   [{"url": u} for u in zips])}
    fetch, _ = datahub(records, {SERIES: related(children=[(CHILD_A, "x, 2024 - Final data")])})
    choices, _ = eea_datahub_discover(fetch, _csv_cfg(records=[]))
    assert choices[2024]["url"] is None and choices[2024]["reason"] == "ambiguous_download"
    assert choices[2024]["candidates"] == zips
    narrowed, _ = eea_datahub_discover(fetch, _csv_cfg(records=[], file_pattern=r"/b\.zip$"))
    assert narrowed[2024]["url"] == zips[1]


def test_the_recursion_has_a_depth_limit_a_record_limit_and_reads_a_cycle_once():
    chain = [f"r{n}" for n in range(5)]
    records = {u: iso_record(f"chain {u}", []) for u in [SERIES] + chain}
    relations = {SERIES: related(links=[f"{API}{chain[0]}"])}
    for n, uuid in enumerate(chain):
        relations[uuid] = related(links=[f"{API}{chain[n + 1]}"] if n + 1 < len(chain) else [] + [f"{API}{SERIES}"])
    relations[chain[1]] = related(links=[f"{API}{chain[2]}", f"{API}{chain[0]}", f"{API}{SERIES}"])      # a cycle
    fetch, calls = datahub(records, relations)
    _, reports = eea_datahub_discover(fetch, _csv_cfg(records=[]))
    read = [r["uuid"] for r in reports if r.get("kind") not in ("summary",) and "status_note" not in r]
    assert read == [SERIES, "r0", "r1"]                                   # depth 0, 1, 2; r2 (depth 3) never read
    assert calls.count(f"{API}r0") == 1 and calls.count(f"{API}{SERIES}") == 1
    star = {SERIES: iso_record("series", [])}
    star_rel = {SERIES: related(links=[f"{API}s{n}" for n in range(50)])}
    star.update({f"s{n}": iso_record(f"s{n}", []) for n in range(50)})
    fetch, calls = datahub(star, star_rel)
    _, reports = eea_datahub_discover(fetch, _csv_cfg(records=[]))
    assert sum(1 for c in calls if c.startswith(API) and not c.endswith("/related")) == 40
    assert sum(1 for r in reports if "record limit" in str(r.get("status_note"))) == 11


# --- D2: electric range from the datahub CSV -------------------------------------------------------------------------------

def _range_rows(year: int) -> list[dict]:
    return [H.eea_row(RANGE_HEADER, Year=year, Mk="BMW", Cn="530E", Va="JP91", Ve="DAW50000", Ft="PETROL/ELECTRIC",
                      Fm="P", R=2, **{"Ec (cm3)": 1998, "Ep (KW)": 135, "Ewltp (g/km)": 47, "Enedc (g/km)": None,
                                      "M (kg)": 1935, "Electric range (km)": 57}),
            H.eea_row(RANGE_HEADER, Year=year, R=3, **{"Enedc (g/km)": None})]


def test_a_csv_header_with_electric_range_is_mapped(tmp_path):
    path = tmp_path / "2021.zip"
    path.write_bytes(csv_zip(_range_rows(2021), RANGE_HEADER))
    rows, report = eea_csv_year(path, 2021, "F")
    assert report["status"] == "built" and report["mapping"]["electric_range_km"] == "Electric range (km)"
    bmw = next(r for r in rows if r["make"] == "BMW")
    assert bmw["electric_range_km"] == 57.0 and bmw["fuel"] == "PETROL/ELECTRIC"


def test_the_range_joins_on_the_normalized_identity_key_and_an_unmatched_key_gets_none():
    keys = ["make", "model", "variant", "version", "fuel", "displacement_cc", "power_kw", "co2_wltp", "year"]
    disco = [{"make": "BMW", "model": "530e", "variant": "jp91", "version": "DAW50000", "fuel": "Petrol/Electric",
              "displacement_cc": 1998, "power_kw": 135, "co2_wltp": 47, "year": 2021, "registrations": 3},
             {"make": "BMW", "model": "530e", "variant": "JP91", "version": "OTHER", "fuel": "PETROL/ELECTRIC",
              "displacement_cc": 1998, "power_kw": 135, "co2_wltp": 47, "year": 2021, "registrations": 1}]
    csv_rows = [{"make": "BMW", "model": "530E", "variant": "JP91", "version": "DAW50000", "fuel": "PETROL/ELECTRIC",
                 "displacement_cc": "1998", "power_kw": "135.0", "co2_wltp": "47", "year": 2021,
                 "electric_range_km": 57.0, "electric_range_km_min": 54.0, "electric_range_km_max": 57.0}]
    stats = range_join(disco, csv_rows, keys, ["electric_range_km"])
    assert disco[0]["electric_range_km"] == 57.0 and disco[0]["electric_range_km_min"] == 54.0
    assert "electric_range_km" not in disco[1]                                   # no exact join: no range
    assert stats["matched"] == 1 and stats["join_pct"] == 50.0 and stats["values_pct_registrations"] == 75.0


def test_discodata_stays_the_base_for_2021_and_the_range_comes_from_the_datahub_csv(tmp_path, monkeypatch):
    disco_rows = {(2021, "F"): [H.eea_row(Year=2021, Mk="BMW", Cn="530e", Va="jp91", Ve="DAW50000",
                                          Ft="Petrol/Electric", Fm="P", R=2, **{"Ec (cm3)": 1998, "Ep (KW)": 135,
                                                                               "Ewltp (g/km)": 47, "Enedc (g/km)": None,
                                                                               "M (kg)": 1935}),
                                {**H.eea_row(Year=2021, R=3), "Enedc (g/km)": None, "Ve": "NO-RANGE"}]}
    disco = _eea_fetch([(2021, "F")], disco_rows, [])
    zip_url = "https://sdi.eea.europa.eu/datashare/s/2021f/download"
    records = {SERIES: iso_record("series", []),
               CHILD_A: iso_record("Monitoring of CO2 emissions from passenger cars, 2021 - Final data",
                                   [{"url": zip_url, "protocol": "WWW:DOWNLOAD-1.0-http--download"}])}
    hub, calls = datahub(records, {SERIES: related(children=[(CHILD_A, "x, 2021 - Final data")])},
                         files={zip_url: csv_zip(_range_rows(2021), RANGE_HEADER)})

    def fetch(url: str) -> bytes:
        return hub(url) if "sdi.eea.europa.eu" in url else disco(url)
    ds.set_snapshot_dir(tmp_path / "work")
    built = build_eea_all(fetch)
    years = [y for y in built["years"] if y["year"] == 2021]
    base = next(y for y in years if y.get("mode") == "per_make")
    join = next(y for y in years if y.get("mode") == "range_join")
    assert join["status"] == "joined" and join["csv_status"] == "F" and join["url"] == zip_url
    assert join["matched"] == 1 and join["join_rows"] == 2 and join["join_pct"] == 50.0
    assert "Electric range (km)" in join["csv_live_header"]
    assert all(r.get("mode") != "csv" for r in years)                          # never rebuilt from the CSV
    bmw = next(r for r in built["rows"] if r["make"] == "BMW")
    other = next(r for r in built["rows"] if r["make"] == "CADILLAC")
    assert bmw["electric_range_km"] == 57.0 and "electric_range_km" not in other
    assert base["mapping"]["electric_range_km"].endswith("(datahub CSV join)")
    assert "electric_range_km" not in base["absent_columns"]
    assert base["audit"]["keys"]["electric_range_km"]["coverage_pct"] == 40.0     # 2 of 5 registrations
    assert len(built["rows"]) == 2


def test_the_probe_reads_a_csv_header_from_the_first_bytes():
    body = csv_zip([H.eea_row(RANGE_HEADER, Year=2023)] * 2000, RANGE_HEADER)
    head = csv_header_from_prefix(body[:4096])
    assert head["header"] == RANGE_HEADER and head["file"] == "CO2_passenger_cars_2021.csv"
    gz = gzip.compress(("﻿" + ";".join(RANGE_HEADER) + "\n1;2\n").encode("utf-8"))
    assert csv_header_from_prefix(gz[:200])["header"][-1] == "Electric range (km)"
    assert csv_header_from_prefix(b"Mk,Cn\nBMW,X")["header"] == ["Mk", "Cn"]


def test_the_probe_prints_the_datahub_records_and_each_csv_header():
    from src.open_data.probe import datahub_markdown, probe_datahub

    zip_url = "https://sdi.eea.europa.eu/datashare/s/2023f/download"
    records = {SERIES: iso_record("series", []),
               CHILD_A: iso_record("Monitoring of CO2 emissions from passenger cars, 2023 - Final data",
                                   [{"url": zip_url, "protocol": "WWW:DOWNLOAD-1.0-http--download"}])}
    fetch, _ = datahub(records, {SERIES: related(children=[(CHILD_A, "x, 2023 - Final data")])})
    body = csv_zip([H.eea_row(RANGE_HEADER, Year=2023)], RANGE_HEADER)
    fetch.prefix = lambda url, limit=4 << 20: body[:limit] if url == zip_url else b""
    out = probe_datahub(fetch, ds.datasets()["eea_co2_cars"])
    assert out["headers"]["2023"]["header"] == RANGE_HEADER
    assert out["headers"]["2023"]["range_columns"] == ["Electric range (km)"]
    text = "\n".join(datahub_markdown(out))
    assert f"chosen {zip_url}" in text and "range columns: ['Electric range (km)']" in text
    assert "download: " + zip_url in text


# --- D3: the determinism check ---------------------------------------------------------------------------------------------

def _shard_rows(year: int, makes: dict[str, int]) -> list[dict]:
    return [{"row_id": f"{m}-{n}", "make": m, "model": f"M{n}", "year": year} for m, k in makes.items()
            for n in range(k)]


def test_a_changed_make_count_in_a_final_year_with_the_same_source_rows_is_a_warning(tmp_path):
    build = _script("build_open_data")
    out = tmp_path / "open"
    old = ds.write_shards("eea_co2_cars", _shard_rows(2021, {"AUDI": 5, "BMW": 4}), {}, status_used={2021: "F"},
                          folder=tmp_path / "old")
    previous_result = {"status": "built", "built_at": "x", "shards": [{**s, "path": str(s["path"])} for s in old],
                       "years": [{"year": 2021, "status": "built", "status_used": "F", "source_rows": 30}]}
    previous, _ = build.write_shards("eea_co2_cars", previous_result, out, tmp_path / "w1", {}, LIMIT, None)
    previous["years"] = previous_result["years"]
    new = ds.write_shards("eea_co2_cars", _shard_rows(2021, {"AUDI": 6, "BMW": 4}), {}, status_used={2021: "F"},
                          folder=tmp_path / "new")
    result = {"shards": [{**s, "path": str(s["path"])} for s in new],
              "years": [{"year": 2021, "status": "built", "status_used": "F", "source_rows": 30}]}
    (item,) = build.determinism("eea_co2_cars", result, out, previous, tmp_path / "w2")
    assert item["deltas"] == {"AUDI": [5, 6]} and item["warning"] is True and item["rows"] == [9, 10]
    result["years"][0]["source_rows"] = 31                                        # the source changed: no warning
    assert build.determinism("eea_co2_cars", result, out, previous, tmp_path / "w3")[0]["warning"] is False
    same = ds.write_shards("eea_co2_cars", _shard_rows(2021, {"AUDI": 5, "BMW": 4}), {}, status_used={2021: "F"},
                           folder=tmp_path / "same")
    result = {"shards": [{**s, "path": str(s["path"])} for s in same], "years": result["years"]}
    assert build.determinism("eea_co2_cars", result, out, previous, tmp_path / "w4")[0]["deltas"] == {}
    table = "\n".join(build.determinism_table({"eea_co2_cars": {"determinism": [item]}}))
    assert "| 2021 | F | 30 -> 30 | 9 -> 10 | 1 | AUDI 5->6 | **yes** |" in table


def test_the_build_step_warns_about_a_d3_change(tmp_path, monkeypatch):
    build = _script("build_open_data")
    rows = {(2021, "F"): [H.eea_row(Year=2021, R=2), H.eea_row(Year=2021, Ve="X", R=1)]}
    out = tmp_path / "open"
    for n, fetch_rows in enumerate((rows, {(2021, "F"): rows[(2021, "F")] + [H.eea_row(Year=2021, Ve="Y")]})):
        fetch = _eea_fetch([(2021, "F")], fetch_rows, [])
        monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None, f=fetch: build_dataset(name, f))
        results, manifest, warnings = build.run(["eea_co2_cars"], out, tmp_path / f"work{n}", {}, LIMIT, None)
    item = results["eea_co2_cars"]["determinism"][0]
    assert item["deltas"] == {"CADILLAC": [2, 3]}
    assert item["source_rows"] == [2, 3] and item["warning"] is False             # the source grew: no warning
    assert manifest["datasets"]["eea_co2_cars"]["determinism"][0]["year"] == 2021

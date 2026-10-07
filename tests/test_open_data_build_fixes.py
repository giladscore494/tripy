"""Open-data build fixes (H1-H5, E1-E5): DISCODATA errors are errors, the short per-(year, make spelling) EEA queries
with their split fallback and the local weighted aggregation, the canonical make table and its normalized spellings, the
CVS unit overrides and file year, and the pull-request fallback. No network."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import datasets as ds
from src.open_data import makes as mk
from src.open_data.build import (BuildStopped, EeaQueryError, EeaQueryTooLong, build_eea, dictionary_check,
                                 eea_aggregate, eea_query_bytes, eea_query_plan, eea_response, resolve_map)

from test_open_data_builds import _eea_fetch, eea_grouped

ROOT = Path(__file__).resolve().parent.parent


# --- H1 -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"errorMessage": "Invalid column name 'R'."},
    {"error": "timeout", "results": []},
    {"message": "Bad request"},                                    # no `results` key at all
    ["not", "an", "object"],
])
def test_sql_raises_on_an_error_or_a_response_without_results(body):
    with pytest.raises(EeaQueryError) as error:
        eea_response(json.dumps(body).encode())
    assert len(str(error.value)) <= 500


def test_sql_returns_the_rows_and_an_empty_result_is_not_an_error():
    assert eea_response(b'{"results": [{"Mk": "BMW"}]}') == [{"Mk": "BMW"}]
    assert eea_response(b'{"results": []}') == []
    with pytest.raises(EeaQueryError):
        eea_response(b"<html>Service unavailable</html>")


def test_a_failed_grouped_year_is_failed_with_its_message_never_built_with_0_rows():
    error = json.dumps({"errorMessage": "Execution Timeout Expired"}).encode()
    rows = {(2018, "F"): [H.eea_row()], (2019, "F"): [H.eea_row(Year=2019)]}

    def grouped(year, status, query):
        return error if year == 2019 else json.dumps({"results": eea_grouped(rows[(year, status)], query)}).encode()
    built = build_eea(_eea_fetch([(2018, "F"), (2019, "F")], rows, [], grouped=grouped))
    reports = {r["year"]: r for r in built["years"]}
    assert reports[2018]["status"] == "built" and reports[2018]["rows"] == 1 and reports[2018]["mode"] == "per_make"
    assert reports[2019]["status"] == "failed" and "Execution Timeout Expired" in reports[2019]["error"]
    assert reports[2019]["make_errors"] == {"CADILLAC": json.dumps({"errorMessage": "Execution Timeout Expired"})}
    assert "rows" not in reports[2019]
    with pytest.raises(BuildStopped) as stop:                     # every year failed: no snapshot, never 0 rows
        build_eea(_eea_fetch([(2019, "F")], rows, [], grouped=grouped))
    assert stop.value.reason == "no_year_built"
    assert stop.value.report["years"][0]["status"] == "failed"


# --- E1: short queries ---------------------------------------------------------------------------------------------------

LONG_MAKES = ["AUDI", "B.M.W.", "CITROËN", "L'AUTOMOBILE",
              "KIA MOTORS SLOVAKIA S.R.O.. SV.JANA NEPOMUCKEHO 1282/1.TEPLICKA NAD VAHOM.013 01 SLOVAK REPUBLIC",
              "HYUNDAI ASSAN OTOMOTIV SANAYI VE TICARET A.S. 34742 KO ZYATAGI. ISTANBUL/TURKEY"]


def _eea_mapping(extra=("Electric range (km)",)):
    cfg = ds.datasets()["eea_co2_cars"]
    return cfg, resolve_map(H.EEA_2018_HEADER + list(extra), cfg["columns"])["mapping"]


def test_every_query_for_the_real_column_set_is_short_and_has_the_e1_shape():
    cfg, mapping = _eea_mapping()
    assert len([k for k in cfg["measure_keys"] if k in mapping]) == 5             # S2: no At1 / At2 / Mt
    for make in LONG_MAKES:
        for year, status in ((2017, "F"), (2022, "P")):
            plan = eea_query_plan(cfg["table"], mapping, cfg, "R", f"[Year] = {year} AND [Status] = '{status}'", make)
            assert len(plan) == 1                                      # the whole column set fits
            (measures, query), = plan
            assert eea_query_bytes(query) <= 1800 and measures == cfg["measure_keys"]
            assert query.count(") AS ") == 1 and query.count(" AS ") == 2 and "SUM(TRY_CAST([R] AS bigint)) AS r " in query
            assert not any(f in query for f in ("MIN(", "MAX(", "AVG(", "ORDER BY"))
            select = query[len("SELECT "):query.index(",SUM(")]
            assert query.endswith("GROUP BY " + select)               # GROUP BY the same columns
            assert select.startswith("[Mk],[Cn],[T],[Va],[Ve],[Ft],[Fm],[Ec (cm3)],[Ep (KW)],[Ewltp (g/km)],[Year],")
            for column in ("[M (kg)]", "[W (mm)]", "[Z (Wh/km)]", "[Fc]", "[Electric range (km)]"):
                assert column in select
            for column in ("[Mt]", "[At1 (mm)]", "[At2 (mm)]"):            # read by neither the match nor an offer
                assert column not in select
    assert "[Mk]=N'CITRO" in eea_query_plan(cfg["table"], mapping, cfg, "R", "[Year] = 2018", "CITROËN")[0][1]
    assert "[Mk]='L''AUTOMOBILE'" in eea_query_plan(cfg["table"], mapping, cfg, "R", "[Year] = 2018",
                                                     "L'AUTOMOBILE")[0][1]


def test_the_split_path_triggers_over_the_limit_and_query_too_long_names_the_length():
    cfg, mapping = _eea_mapping()
    where = "[Year] = 2018 AND [Status] = 'F'"
    whole = eea_query_bytes(eea_query_plan(cfg["table"], mapping, cfg, "R", where, "AUDI")[0][1])
    plan = eea_query_plan(cfg["table"], mapping, cfg, "R", where, "AUDI", limit=whole - 1)
    assert len(plan) == 2 and all(eea_query_bytes(q) <= whole - 1 for _, q in plan)
    assert plan[0][0] + plan[1][0] == cfg["measure_keys"] and plan[0][0] and plan[1][0]
    for _, query in plan:                                              # both parts carry the whole identity key
        assert "[Mk],[Cn],[T],[Va],[Ve],[Ft],[Fm],[Ec (cm3)],[Ep (KW)],[Ewltp (g/km)],[Year]" in query
    with pytest.raises(EeaQueryTooLong) as error:
        eea_query_plan(cfg["table"], mapping, cfg, "R", where, "AUDI", limit=300)
    assert error.value.reason == "query_too_long" and error.value.length > 300
    assert str(error.value).startswith("query_too_long: ") and isinstance(error.value, EeaQueryError)


def test_the_runner_computes_the_weighted_median_min_max_and_joins_split_parts():
    cfg, mapping = _eea_mapping(())
    ident = {"Mk": "BMW", "Cn": "530E", "T": "G30", "Va": "JA", "Ve": "1", "Ft": "petrol/electric", "Fm": "P",
             "Ec (cm3)": 1998, "Ep (KW)": 135, "Ewltp (g/km)": 46, "Year": 2021}
    part_a = [{**ident, "M (kg)": 1900, "W (mm)": 2975, "r": 10}, {**ident, "M (kg)": 1945, "W (mm)": 2975, "r": 30},
              {**ident, "M (kg)": 2010, "W (mm)": 2975, "r": 5}, {**ident, "Cn": "520D", "M (kg)": 1700, "r": None}]
    part_b = [{**ident, "Fc": 2.0, "r": 40}, {**ident, "Fc": 2.4, "r": 5}, {**ident, "Cn": "520D", "Fc": 5.1, "r": 7}]
    rows = eea_aggregate([(["mass_running_order_kg", "wheelbase_mm"], part_a), (["fuel_consumption_l_100km"], part_b)],
                         mapping, cfg, "F")
    bmw = next(r for r in rows if r["model"] == "530E")
    assert (bmw["mass_running_order_kg"], bmw["mass_running_order_kg_min"], bmw["mass_running_order_kg_max"]) == \
        (1945.0, 1900.0, 2010.0)                                       # the weighted median, not the average
    assert bmw["wheelbase_mm"] == bmw["wheelbase_mm_min"] == bmw["wheelbase_mm_max"] == 2975.0
    assert (bmw["fuel_consumption_l_100km"], bmw["fuel_consumption_l_100km_max"]) == (2.0, 2.4)
    assert bmw["registrations"] == 45 and bmw["status"] == "F" and bmw["make"] == "BMW"
    other = next(r for r in rows if r["model"] == "520D")
    assert other["mass_running_order_kg"] == 1700.0 and other["fuel_consumption_l_100km"] == 5.1
    assert other["registrations"] is None                             # R unreadable: no count, never 0


def test_a_build_over_the_limit_splits_and_a_failing_spelling_stops_only_itself(monkeypatch):
    from src.open_data import build

    monkeypatch.setattr(build, "known_makes", lambda: {"BMW", "CADILLAC", "TOYOTA"})
    rows = {(2018, "F"): [H.eea_row(), H.eea_row(Ve="X"),
                          H.eea_row(Mk="BMW", Cn="530E", **{"M (kg)": 1945, "Z (Wh/km)": 180}),
                          H.eea_row(Mk="BMW", Cn="530E", **{"M (kg)": 2010, "Z (Wh/km)": 180}, R=1),
                          H.eea_row(Mk="TOYOTA", Cn="YARIS"), H.eea_row(Mk="1OYOTA", Cn="YARIS")]}
    queries: list[str] = []

    def grouped(year, status, query):
        if "[Mk]='TOYOTA'" in query:
            return json.dumps({"errorMessage": "TOYOTA failed"}).encode()
        return json.dumps({"results": eea_grouped(rows[(year, status)], query)}).encode()
    whole = eea_query_bytes(eea_query_plan(ds.datasets()["eea_co2_cars"]["table"], _eea_mapping(())[1],
                                           ds.datasets()["eea_co2_cars"], "R", "[Year] = 2018 AND [Status] = 'F'",
                                           "CADILLAC")[0][1])
    monkeypatch.setattr(build, "EEA_MAX_QUERY_BYTES", whole - 1)
    built = build_eea(_eea_fetch([(2018, "F")], rows, queries, grouped=grouped))
    report = built["years"][0]
    assert report["mode"] == "per_make" and report["split"] is True and report["status"] == "partial"
    assert report["spelling_source"] == "year_distinct" and report["spellings"] == 3     # never 1OYOTA
    assert report["make_errors"] == {"TOYOTA": json.dumps({"errorMessage": "TOYOTA failed"})}
    assert report["by_make"] == {"BMW": 1, "CADILLAC": 2} and report["rows"] == 3
    assert report["max_query_bytes"] <= whole - 1 and all(eea_query_bytes(q) <= whole - 1 for q in queries)
    assert not any("1OYOTA" in q for q in queries)
    bmw = next(r for r in built["rows"] if r["make"] == "BMW")
    assert (bmw["mass_running_order_kg_min"], bmw["mass_running_order_kg_max"]) == (1945.0, 2010.0)
    assert bmw["mass_running_order_kg"] == 1945.0 and bmw["registrations"] == 4   # weighted median (3 vs 1)
    assert bmw["wheelbase_mm"] == 2910.0 and bmw["energy_wh_km"] == 180.0       # both halves joined on the identity key
    assert "track_rear_mm" not in bmw and "mass_wltp_test_kg" not in bmw        # S2: no longer queried
    assert "fuel_consumption_l_100km" not in bmw                       # no value stated: no measurement
    cts = next(r for r in built["rows"] if r["make"] == "CADILLAC")
    assert cts["mass_running_order_kg"] == cts["mass_running_order_kg_min"] == 1734.0


# --- E2 / E4: the canonical make table and the normalized spellings --------------------------------------------------------

@pytest.mark.parametrize("spelling, make", [
    ("B.M.W.", "BMW"), ("LANDROVER", "LAND ROVER"), ("Land Rover", "LAND ROVER"), ("ALFAROMEO", "ALFA ROMEO"),
    ("VOLVO CAR CORPORATION ASSAR GABRIELSSONS VAG 405 31 GOTHENBURG SWEDEN", "VOLVO"),
    ("AUDI AG DE-85045 INGOLSTADT", "AUDI"), ("AUDI AG", "AUDI"), ("A.U.D.I.", "AUDI"),
    ("TOYOTA MOTOR CORPORATION 1. TOYOTA-CHO.TOYOTA CITY", "TOYOTA"), ("MERCEDES-BENZ", "MERCEDES-BENZ"),
    ("MERCEDES BENZ AG DE-70372 STUTTGART GERMANY", "MERCEDES-BENZ"), ("CITROËN", "CITROEN"),
    ("ALFA ROMEO S.P.A. C.SO G. AGNELLI 200", "ALFA ROMEO"), ("SKODA AUTO S.A.", "SKODA"),
    ("HIUNDAI", None), ("JEPP", None), ("1OYOTA", None), ("AUDI HUNGARIA", None), ("JAGUAR LAND ROVER", None),
    ("BMW,MINI", None), ("3 BMW", None),
])
def test_a_spelling_belongs_to_a_make_only_when_it_normalizes_to_it(spelling, make):
    assert mk.canonical_of(spelling) == make


def test_the_canonical_table_maps_every_catalog_tozar_or_lists_it_for_review():
    table = mk.canonical()
    catalog = mk.catalog_manufacturers()
    unmapped = table["unmapped"]["tozar"]
    assert len(catalog) == 62 and sorted(unmapped) == sorted(["גמס", "דאבל יו אם איי", "דאיון"])
    assert all(t in table["tozar"] or t in unmapped for t in catalog) and len(table["tozar"]) == 59
    assert mk.tozar_makes("הונדה") == (["HONDA"], []) and mk.tozar_makes("ג'יפ") == (["JEEP"], [])
    assert mk.tozar_makes("ב מ וו") == (["BMW"], ["MINI"]) and mk.tozar_makes("סיאט") == (["SEAT"], ["CUPRA"])
    assert mk.tozar_makes("דיימלר קרייזלר") == ([], ["CHRYSLER", "DODGE", "JEEP"])
    assert mk.tozar_makes("רובר") == mk.tozar_makes("לנדרובר") == (["LAND ROVER"], [])


def test_a_mini_row_counts_for_bmw_only_when_its_model_is_a_catalog_model_of_that_tozar():
    keep = mk.row_filter()
    families = mk.model_gates()["MINI"]["ב מ וו"]
    assert "COOPER" in families and "COUNTRYMAN" in families and "MINI" not in families   # never the make itself
    assert keep("MINI", "COOPER S") and keep("MINI", "MINI ONE D COUNTRYMAN")
    assert not keep("MINI", "MINI") and not keep("MINI", "PACEMAN")
    assert keep("BMW", "X5") and keep("B.M.W.", "SERIE 3") and keep("BMW I", "I3")        # plain / reviewed
    assert not keep("CUPRA", "CUPRA") and keep("CUPRA", "FORMENTOR")
    assert not keep("RAM", "1500") and keep("JEEP", "WRANGLER") and keep("DODGE", "CHALLENGER")
    assert not keep("HIUNDAI", "I30") and not keep("JEPP", "WRANGLER")


def test_the_aliases_are_canonical_names_generated_spellings_and_reviewed_extras(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"aliases": {"קיה": ["Kia Motors"], "ב מ וו": ["B.M.W."]}}), "utf-8")
    ds.set_repo_dir(tmp_path / "no-open-data")              # without the committed manifest's make_spellings
    try:
        merged = mk.aliases(path)
    finally:
        ds.set_repo_dir(None)
    assert merged["קיה"] == ["KIA", "KIA MOTORS"]
    assert merged["ב מ וו"] == ["B.M.W.", "BMW", "BMW I", "MINI"]           # + the reviewed extra BMW I
    assert merged["טויוטה"] == ["TOYOTA"] and merged["מרצדס"] == ["MERCEDES-AMG", "MERCEDES-BENZ"]
    assert "גמס" not in merged


def test_the_alias_script_reports_spellings_per_dataset_and_rows_kept(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_make_aliases", ROOT / "scripts" / "build_make_aliases.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"datasets": {
        "eea_co2_cars": {"distinct_makes": ["SKODA", "KIA", "HIUNDAI", "AUDI AG DE-85045 INGOLSTADT", "1OYOTA",
                                            "MINI"],
                         "make_rows": {"SKODA": 10, "KIA": 5, "HIUNDAI": 2, "AUDI AG DE-85045 INGOLSTADT": 3,
                                       "1OYOTA": 1, "MINI": 4}},
        "epa_fueleconomy": {"distinct_makes": ["SUBARU", "LANDROVER"]}}}), "utf-8")
    out, review = tmp_path / "aliases.json", tmp_path / "review.md"
    assert script.main(["--probe", str(probe), "--open", str(tmp_path / "none"), "--out", str(out),
                        "--review", str(review)]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["aliases"]["סקודה"] == ["SKODA"] and data["aliases"]["סובארו"] == ["SUBARU"]
    assert data["aliases"]["אאודי"] == ["AUDI", "AUDI AG DE-85045 INGOLSTADT"]
    assert data["aliases"]["לנדרובר"] == ["LAND ROVER", "LANDROVER"]
    assert data["spellings"]["AUDI"] == {"eea_co2_cars": ["AUDI AG DE-85045 INGOLSTADT"]}
    assert not any("HIUNDAI" in v or "1OYOTA" in v for v in data["aliases"].values())
    assert data["rows"]["eea_co2_cars"] == {"kept": 22, "total": 25, "spellings_kept": 4, "spellings_total": 6}
    assert data["model_gated"]["ב מ וו"]["spellings"] == ["MINI"] and "COOPER" in data["model_gated"]["ב מ וו"]["models"]
    assert data["review"]["unmapped"] == sorted(["גמס", "דאבל יו אם איי", "דאיון"])
    assert data["review"]["not_in_canonical"] == [] and data["manufacturers"] == 62
    assert data["sources"] == {"eea_co2_cars": 6, "epa_fueleconomy": 2}
    text = review.read_text("utf-8")
    assert "| eea_co2_cars | 22 / 25 | 4 / 6 |" in text and "HIUNDAI (2)" in text and "Review: unmapped tozar (3)" in text


# --- H4 -------------------------------------------------------------------------------------------------------------------

def test_cvs_overrides_confirm_units_the_dictionary_does_not_state():
    cfg = ds.datasets()["tc_cvs"]
    unitless = [["Code", "Description"], ["MYR", "Model year"], ["OL", "Overall length"], ["OW", "Overall width"],
                ["OH", "Overall height"], ["WB", "Wheelbase"], ["CW", "Curb weight"]]
    assert dictionary_check(unitless, cfg["dictionary_check"], cfg["unit_overrides"]) == []
    without = {k: v for k, v in cfg["unit_overrides"].items() if k != "curb_weight_kg"}
    problems = dictionary_check(unitless, cfg["dictionary_check"], without)
    assert [(p["code"], p["reason"]) for p in problems] == [("CW", "unit_not_stated")]
    wrong = {**cfg["unit_overrides"], "curb_weight_kg": {**cfg["unit_overrides"]["curb_weight_kg"], "unit": "lb"}}
    assert [p["code"] for p in dictionary_check(unitless, cfg["dictionary_check"], wrong)] == ["CW"]
    for key in ("length_cm", "width_cm", "height_cm", "wheelbase_cm", "curb_weight_kg"):
        assert "CTS 2018" in cfg["unit_overrides"][key]["evidence"]


def test_cvs_headers_with_trailing_spaces_resolve():
    cfg = ds.datasets()["tc_cvs"]
    out = resolve_map(["MYR", "MAKE", "MODEL", "OL", "OW", "OH ", "WB", "CW ", "TWR "], cfg["columns"])
    assert not out["missing"] and out["mapping"]["height_cm"] == "OH " and out["mapping"]["curb_weight_kg"] == "CW "


def test_the_cvs_probe_prints_each_unit_columns_distribution(monkeypatch):
    from src.open_data import build
    from src.open_data.probe import markdown, probe_ckan_files

    from test_open_data_builds import CVS_RESOURCES, _cvs_fetch

    monkeypatch.setattr(build, "_xls_rows", lambda body: [["OL", "Overall length"], ["OW", "w"], ["OH", "h"],
                                                          ["WB", "wb"], ["CW", "cw"], ["MYR", "year"]])
    import src.open_data.probe as probe_mod

    monkeypatch.setattr(probe_mod, "_xls_rows", build._xls_rows)
    out = probe_ckan_files("tc_cvs", _cvs_fetch([]))
    assert out["status"] == "ok" and out["dictionary_problems"] == []
    dist = out["headers"]["2018_en.csv"]["distributions"]["wheelbase_cm"]
    assert dist == {"n": 1, "p5": 291.0, "p50": 291.0, "p95": 291.0}
    assert out["distinct_makes"] == ["CADILLAC"]
    assert "wheelbase_cm: p5 291.0" in markdown({"datasets": {"tc_cvs": {"dataset": "tc_cvs", **out}}})
    assert CVS_RESOURCES


def test_the_eea_probe_runs_exactly_the_e1_query_and_records_its_url_length():
    from src.open_data.probe import markdown, probe_eea

    rows = {(2018, "F"): [H.eea_row(), H.eea_row(Mk="AUDI", Cn="A4")]}
    sent: list[str] = []

    def fetch(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        if query.startswith("SELECT DISTINCT [Mk] FROM"):
            return json.dumps({"results": [{"Mk": "CADILLAC"}, {"Mk": "AUDI"}, {"Mk": "1OYOTA"}]}).encode()
        if query.startswith("SELECT [Mk],COUNT(*)"):
            return json.dumps({"results": [{"Mk": "CADILLAC", "n": 7}, {"Mk": "AUDI", "n": 3}]}).encode()
        if "GROUP BY" in query:
            sent.append(url)
            return json.dumps({"errorMessage": "Conversion failed when converting the varchar value " + "x" * 400}
                              ).encode()
        return _eea_fetch([(2018, "F")], rows, [])(url)
    out = probe_eea(fetch)
    assert out["distinct_makes"] == ["1OYOTA", "AUDI", "CADILLAC"] and out["make_rows"] == {"CADILLAC": 7, "AUDI": 3}
    test = out["grouped_test"]
    assert test["status"] == "failed" and "Conversion failed" in test["error"] and test["year"] == 2018
    assert test["make"] == "AUDI" and test["queries"] == 1 and len(sent) == 1
    cfg = ds.datasets()["eea_co2_cars"]
    expected = eea_query_plan(cfg["table"], resolve_map(H.EEA_2018_HEADER, cfg["columns"])["mapping"], cfg, "R",
                              "[Year] = 2018 AND [Status] = 'F'", "AUDI")[0][1]
    assert test["query"] == expected and parse_qs(urlparse(sent[0]).query)["query"][0] == expected
    assert test["url_length"] == len(sent[0]) and len(test["response_head"]) == 300
    text = markdown({"datasets": {"eea_co2_cars": {"dataset": "eea_co2_cars", **out}}})
    assert "response head" in text and f"URL length {len(sent[0])}" in text


# --- E5: the CVS file year --------------------------------------------------------------------------------------------------

def test_the_cvs_year_is_the_file_year_and_myr_is_the_measured_year(monkeypatch):
    from src.open_data import build
    from src.open_data.build import build_ckan_files, measured_year

    from test_open_data_builds import CVS_RESOURCES

    monkeypatch.setattr(build, "_xls_rows", lambda body: H.CVS_DICTIONARY_ROWS)

    def fetch(url):
        if "package_show" in url:
            return json.dumps({"result": {"resources": CVS_RESOURCES}}).encode()
        if url.endswith(".xls"):
            return b"xls"
        return H.csv_text(H.CVS_HEADER, [["14", "CADILLAC", "CTS", 497, 183, 145, 291, 1642],
                                         ["8", "CADILLAC", "CTS", 486, 185, 147, 288, 1700]])
    built = build_ckan_files("tc_cvs", fetch)
    cts_2018 = [r for r in built["rows"] if r["file_year"] == 2018]
    assert [(r["year"], r["measured_year"]) for r in cts_2018] == [(2018, 2014), (2018, 2008)]
    assert measured_year("2") == 2002 and measured_year(22) == 2022 and measured_year("2016") == 2016
    assert measured_year("") is None


def _cvs_offer(measured: int, target_year: int, max_age_ok=True) -> dict:
    from src.open_data.offers import field_offers

    entry = next(e for e in ds.config()["field_map"] if e["source"] == "tc_cvs" and e["field"] == "curb_weight_kg")
    row = {"row_id": "cvs-153", "year": 2018, "measured_year": measured, "curb_weight_kg": 1642}
    return field_offers({"route": "american", "level": "exact_technical_variant", "keys": {"year": target_year},
                         "sources": {"tc_cvs": {"status": "unique", "survivors": [row]}}}, [entry])[0]


def test_a_cvs_offer_records_its_measured_year_and_an_old_one_is_reference_only():
    assert ds.datasets()["tc_cvs"]["measured_year_max_age"] == 6
    recent = _cvs_offer(2014, 2018)
    assert recent["status"] == "offered" and recent["measured_year"] == 2014 and recent["value"] == 1642
    assert _cvs_offer(2012, 2018)["status"] == "offered"                      # exactly 6 years: still offered
    old = _cvs_offer(2011, 2018)
    assert old["status"] == "reference_only" and old["measured_year"] == 2011 and "older generation" in old["reason"]


# --- E4: rows kept and kept makes ------------------------------------------------------------------------------------------

def test_compaction_keeps_the_normalized_spellings_and_reports_the_kept_makes():
    from src.open_data.build import compact

    rows = [{"make": "Ford", "model": "F150", "year": 2018}, {"make": "HONDA", "model": "Civic", "year": 2018},
            {"make": "Land Rover", "model": "Defender", "year": 2020}, {"make": "MINI", "model": "Cooper S", "year": 2019},
            {"make": "MINI", "model": "Paceman", "year": 2015}, {"make": "Ram", "model": "1500", "year": 2020},
            {"make": "Jeep", "model": "Wrangler", "year": 2018}, {"make": "HIUNDAI", "model": "i30", "year": 2018},
            {"make": "Studebaker", "model": "Lark", "year": 2018}]
    out = compact("epa_fueleconomy", {"rows": rows})
    assert sorted(r["make"] for r in out["rows"]) == ["FORD", "HONDA", "JEEP", "LAND ROVER", "MINI"]   # D0
    c = out["compaction"]
    assert (c["rows_before"], c["rows_after"], c["kept_makes"]) == (9, 5, 5)
    assert out["make_spellings"] == {"FORD": ["FORD"], "HONDA": ["HONDA"], "JEEP": ["JEEP"],
                                     "LAND ROVER": ["LAND ROVER"], "MINI": ["MINI"]}


def test_the_action_summary_prints_rows_kept_and_kept_makes_per_dataset():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_open_data", ROOT / "scripts" / "build_open_data.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    results = {"epa_fueleconomy": {"status": "built", "rows": 5, "make_spellings": {"FORD": ["FORD"], "JEEP": ["JEEP"]},
                                   "compaction": {"rows_before": 9, "rows_after": 5, "kept_makes": 2}},
               "eea_co2_cars": {"status": "built", "rows": 3, "make_spellings": {"AUDI": ["AUDI", "AUDI AG"]},
                                "compaction": {"rows_before": 3, "rows_after": 3, "kept_makes": 1}},
               "tc_cvs": {"status": "failed", "error": "x"}}
    probe = {"datasets": {"eea_co2_cars": {"make_rows": {"AUDI": 10, "AUDI AG": 5, "HIUNDAI": 5}}}}
    text = script.summary(results, {"datasets": {}}, probe)
    assert "| epa_fueleconomy | 5 / 9 (rows) | 2 | >= 40 (**lower**) | FORD, JEEP |" in text
    assert "| eea_co2_cars | 15 / 20 (raw rows (probe)) | 1 | — | AUDI |" in text
    assert "| tc_cvs |" in text.split("Makes kept per dataset")[0] and "| tc_cvs | x" not in text
    assert "make_spellings" in script.ENTRY_KEYS


# --- H5 -------------------------------------------------------------------------------------------------------------------

def test_the_pull_request_step_may_fail_and_the_compare_url_follows_it():
    workflow = (ROOT / ".github" / "workflows" / "build-open-data.yml").read_text("utf-8")
    steps = workflow[workflow.index("steps:"):]
    pr = steps[steps.index("- name: Open a pull request"):steps.index("uses: peter-evans/create-pull-request")]
    assert "id: cpr" in pr and "continue-on-error: true" in pr
    fallback = steps[steps.index("uses: peter-evans/create-pull-request"):]
    assert "if: always() && steps.cpr.outcome == 'failure'" in fallback
    assert "compare/main...automation/open-data?expand=1" in fallback and "GITHUB_STEP_SUMMARY" in fallback
    assert steps.index("scripts/probe_open_data.py") < steps.index("scripts/build_make_aliases.py") \
        < steps.index("scripts/build_open_data.py")
    assert "data/open_data_make_aliases.json" in steps[steps.index("add-paths"):]

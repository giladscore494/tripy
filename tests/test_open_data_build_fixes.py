"""Open-data build fixes (H1-H5): DISCODATA errors are errors, the EEA server-side grouping with its make-split
fallback, make aliases for the whole catalog, the CVS unit overrides, and the pull-request fallback. No network."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import datasets as ds
from src.open_data import makes as mk
from src.open_data.build import (BuildStopped, EeaQueryError, build_eea, dictionary_check, eea_grouped_query,
                                 eea_response, resolve_map)

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
    assert reports[2018]["status"] == "built" and reports[2018]["rows"] == 1 and reports[2018]["mode"] == "year"
    assert reports[2019]["status"] == "failed" and "Execution Timeout Expired" in reports[2019]["error"]
    assert reports[2019]["mode"] == "make_split" and "rows" not in reports[2019]
    with pytest.raises(BuildStopped) as stop:                     # every year failed: no snapshot, never 0 rows
        build_eea(_eea_fetch([(2019, "F")], rows, [], grouped=grouped))
    assert stop.value.reason == "no_year_built"
    assert stop.value.report["years"][0]["status"] == "failed"


# --- H2 -------------------------------------------------------------------------------------------------------------------

def test_the_grouped_query_groups_by_the_identity_keys_only():
    cfg = ds.datasets()["eea_co2_cars"]
    mapping = resolve_map(H.EEA_2018_HEADER, cfg["columns"])["mapping"]
    query = eea_grouped_query(cfg["table"], mapping, cfg, "R", "[Year] = 2018 AND [Status] = 'F'", ["BMW"])
    group_by = query.split("GROUP BY ", 1)[1]
    assert group_by == ("[Mk], [Cn], [T], [Va], [Ve], [Ft], [Fm], [Ec (cm3)], [Ep (KW)], [Ewltp (g/km)], [Year]")
    assert "ORDER BY" not in query and "SUM(TRY_CAST([R] AS bigint)) AS [registrations]" in query
    for column in ("[W (mm)]", "[At1 (mm)]", "[At2 (mm)]", "[M (kg)]", "[Mt]", "[Z (Wh/km)]", "[Fc]"):
        assert f"MIN(TRY_CAST({column} AS float))" in query and f"MAX(TRY_CAST({column} AS float))" in query \
            and f"AVG(TRY_CAST({column} AS float))" in query


def test_a_year_that_fails_as_a_whole_is_split_by_make_and_the_summary_counts_rows_per_make(monkeypatch):
    from src.open_data import build

    monkeypatch.setattr(build, "known_makes", lambda: {"BMW", "CADILLAC", "TOYOTA"})
    rows = {(2018, "F"): [H.eea_row(), H.eea_row(Ve="X"), H.eea_row(Mk="BMW", Cn="530E", **{"M (kg)": 1945}),
                          H.eea_row(Mk="BMW", Cn="530E", **{"M (kg)": 2010})]}
    queries: list[str] = []

    def grouped(year, status, query):
        makes = query.split(" IN (", 1)[1]
        if makes.count("'") > 2:                                     # the whole year: refused server-side
            return json.dumps({"errorMessage": "query too expensive"}).encode()
        if "'TOYOTA'" in makes:
            return json.dumps({"errorMessage": "TOYOTA failed"}).encode()
        return json.dumps({"results": eea_grouped(rows[(year, status)], query)}).encode()
    built = build_eea(_eea_fetch([(2018, "F")], rows, queries, grouped=grouped))
    report = built["years"][0]
    assert report["mode"] == "make_split" and report["status"] == "partial" and "too expensive" in report["year_error"]
    assert report["by_make"] == {"BMW": 1, "CADILLAC": 2} and report["rows"] == 3
    assert report["make_errors"] == {"TOYOTA": json.dumps({"errorMessage": "TOYOTA failed"})}
    bmw = next(r for r in built["rows"] if r["make"] == "BMW")
    assert (bmw["mass_running_order_kg_min"], bmw["mass_running_order_kg_max"]) == (1945.0, 2010.0)
    assert bmw["mass_running_order_kg"] == 1977.5 and bmw["registrations"] == 6     # AVG when MIN != MAX
    cts = next(r for r in built["rows"] if r["make"] == "CADILLAC")
    assert cts["mass_running_order_kg"] == cts["mass_running_order_kg_min"] == 1734.0


# --- H3 -------------------------------------------------------------------------------------------------------------------

def test_a_tozeret_nm_without_its_country_is_the_bmw_aliases():
    tozar = mk.strip_country("ב מ וו גרמניה", mk.catalog_manufacturers())
    assert tozar == "ב מ וו" and "BMW" in mk.aliases()[tozar]
    assert mk.strip_country("פולקסווגן-ספרד") == "פולקסווגן" and mk.strip_country("קיה ד. קוריאה") == "קיה"


def test_aliases_are_proposed_only_from_source_spellings_and_unmatched_makes_are_listed_not_guessed():
    observed = {"eea_co2_cars": ["TOYOTA", "HONDA", "HYUNDAI", "RENAULT", "MAZDA", "MAZDA MOTOR", "LAND ROVER"],
                "epa_fueleconomy": ["TESLA", "Chevrolet"]}
    result = mk.propose(["הונדה", "יונדאי", "רנו", "מזדה", "שברולט", "טסלה", "לנדרובר", "פיג'ו", "ב מ וו"], observed,
                        {"ב מ וו": ["BMW"]})
    assert result["aliases"]["הונדה"] == ["HONDA"] and result["aliases"]["יונדאי"] == ["HYUNDAI"]
    assert result["aliases"]["מזדה"] == ["MAZDA", "MAZDA MOTOR"]                # both source spellings
    assert result["aliases"]["שברולט"] == ["CHEVROLET"] and result["aliases"]["טסלה"] == ["TESLA"]
    assert result["aliases"]["לנדרובר"] == ["LAND ROVER"]
    assert "רנו" in result["unmatched"] and "פיג'ו" in result["unmatched"]       # RENAULT / PEUGEOT: silent letters
    assert "ב מ וו" not in result["aliases"] and result["reviewed"] == {"ב מ וו": ["BMW"]}
    flat = {m for makes in result["aliases"].values() for m in makes}
    assert flat <= {" ".join(m.split()).upper() for ms in observed.values() for m in ms}


def test_two_manufacturers_matching_one_make_are_ambiguous():
    result = mk.propose(["דאצ'יה", "דאציה"], {"eea_co2_cars": ["DACIA"]})
    assert result["aliases"] == {} and set(result["ambiguous"]) == {"דאצ'יה", "דאציה"}


def test_a_short_first_word_never_matches_alone_and_dge_spells_j():
    result = mk.propose(["אודי", "דה טומאסו", "דודג'"], {"eea_co2_cars": ["AUDI", "DE TOMASO", "DODGE"]})
    assert result["aliases"] == {"אודי": ["AUDI"], "דה טומאסו": ["DE TOMASO"], "דודג'": ["DODGE"]}


def test_one_skeleton_naming_makes_with_different_first_words_is_ambiguous():
    result = mk.propose(["גמס"], {"eea_co2_cars": ["GMC", "GMC TRUCK"]})
    assert result["aliases"] == {"גמס": ["GMC", "GMC TRUCK"]}
    result = mk.propose(["גמס"], {"eea_co2_cars": ["GMC", "GEMEC"]})
    assert result["aliases"] == {} and result["ambiguous"] == {"גמס": {"gmk": ["GEMEC", "GMC"]}}


def test_the_generated_aliases_merge_with_the_reviewed_vocabulary(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"aliases": {"קיה": ["Kia"], "ב מ וו": ["BMW ALPINA"]}}), "utf-8")
    merged = mk.aliases(path)
    assert merged["קיה"] == ["KIA"]
    assert merged["ב מ וו"] == ["BMW", "BMW I", "MINI"]                        # the reviewed entry wins
    assert merged["טויוטה"] == ["TOYOTA"]


def test_the_alias_script_writes_the_proposals_and_the_review_list(tmp_path):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("build_make_aliases", ROOT / "scripts" / "build_make_aliases.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"datasets": {"eea_co2_cars": {"distinct_makes": ["SKODA", "KIA", "RENAULT"]},
                                              "epa_fueleconomy": {"distinct_makes": ["SUBARU"]}}}), "utf-8")
    out, review = tmp_path / "aliases.json", tmp_path / "review.md"
    assert script.main(["--probe", str(probe), "--open", str(tmp_path / "none"), "--out", str(out),
                        "--review", str(review)]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["aliases"]["סקודה"] == ["SKODA"] and data["aliases"]["סובארו"] == ["SUBARU"]
    assert data["aliases"]["קיה"] == ["KIA"] and "רנו" in data["review"]["unmatched"]
    assert data["manufacturers"] >= 62 and data["sources"] == {"eea_co2_cars": 3, "epa_fueleconomy": 1}
    text = review.read_text("utf-8")
    assert "Review: unmatched" in text and "- רנו" in text


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


def test_the_eea_probe_runs_a_grouped_test_query_and_records_the_distinct_makes():
    from src.open_data.probe import markdown, probe_eea

    rows = {(2018, "F"): [H.eea_row()]}

    def fetch(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        if query.startswith("SELECT DISTINCT [Mk]"):
            return json.dumps({"results": [{"Mk": "CADILLAC"}, {"Mk": "BMW"}]}).encode()
        if "GROUP BY" in query:
            return json.dumps({"errorMessage": "Conversion failed when converting the varchar value"}).encode()
        return _eea_fetch([(2018, "F")], rows, [])(url)
    out = probe_eea(fetch)
    assert out["distinct_makes"] == ["BMW", "CADILLAC"]
    test = out["grouped_test"]
    assert test["status"] == "failed" and "Conversion failed" in test["error"] and test["year"] == 2018
    assert "Conversion failed" in test["response_head"] and "GROUP BY" in test["query"]
    assert "response head" in markdown({"datasets": {"eea_co2_cars": {"dataset": "eea_co2_cars", **out}}})


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

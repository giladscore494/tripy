"""Government datasets, the G3 / G1 / G2 fixes: dates and codes from the datastore (F1, F2: never a silent zero), the
price selected by model name (F3) and the multi-model recall DEGEM (F4). No network: the fake CKAN of
test_gov_datasets."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from test_gov_datasets import (ACTIVE, CANCELLED, CHASSIS, ENGINE, PLATE, PRICE_HEADER, PRICES, RECALL_HEADER,
                               B, F, FakeCkan, _csv, active_body, bodies, build, cancelled_body, gov, row,
                               serve)  # noqa: F401 - gov is a fixture
from src.gov_data import prices as G1
from src.gov_data import recalls as G2
from src.gov_data import survival as G3
from src.gov_data.names import date_pattern, to_int, ym_of


# --- F1: dates -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2019-03-12T00:00:00", (2019, 3)), ("2019-03-12T00:00:00.123Z", (2019, 3)), ("201903", (2019, 3)),
    (201903, (2019, 3)), ("2019-03", (2019, 3)), ("2019-3", (2019, 3)), ("2019-03-12", (2019, 3)),
    ("12/03/2019", (2019, 3)), ("20190312", (2019, 3)), ("garbage", None), ("2019-13", None), ("", None),
])
def test_ym_of_accepts_the_datastore_and_file_formats(value, expected):
    assert ym_of(value) == expected


def test_a_date_shape_keeps_no_digit_or_letter():
    assert date_pattern("2019-03-12T00:00:00") == "9999-99-99a99:99:99"
    assert date_pattern(None) == "(empty)" and date_pattern("12 מרץ 19") == "99 aaa 99"


def _cohort(cancellations: list[tuple[str, str]], active: int = 300) -> dict:
    agg = G3.Survival()
    for _ in range(active):
        agg.add({"_role": "active", "_resource_id": "a", "tozeret_cd": "1", "degem_cd": "2", "shnat_yitzur": "2010",
                 "moed_aliya_lakvish": "2010-01"})
    for road, bitul in cancellations:
        agg.add({"_role": "cancelled", "_resource_id": "c", "tozeret_cd": "1", "degem_cd": "2",
                 "shnat_yitzur": "2010", "moed_aliya_lakvish": road, "bitul_dt": bitul})
    rows, stats = G3.cohorts(agg, G3.BASIS_MODEL_YEAR, 2026 * 12 + 9, 200, (3, 20))
    return {**rows[0], "_stats": stats, "_agg": agg}


def test_ten_dated_cancellations_give_shares_above_zero():
    cohort = _cohort([("2010-01-15T00:00:00", "2016-06-01T00:00:00")] * 10)
    assert cohort["dated_cancelled"] == 10 and cohort["withheld"] is None
    shares = json.loads(json.dumps(cohort["shares"]))
    assert shares["7"] == round(10 / 310, 4) and shares["10"] == round(10 / 310, 4) and shares["6"] == 0.0
    assert cohort["age_median"] == 6.4


def test_a_cohort_with_undated_cancellations_gets_no_survival_fields():
    cohort = _cohort([("2010-01", "2016-06")] * 10 + [("2010-01", "")] * 5)       # 10 of 15 dated: 67 %
    assert cohort["shares"] is None and cohort["age_median"] is None
    assert cohort["withheld"] == "undated_cancellations" and cohort["_stats"]["undated_cohorts"] == 1


def _undated(body: bytes, column: str, value: str) -> bytes:
    """The fixture CSV body with every value of `column` replaced."""
    lines = body.decode("utf-8-sig").splitlines()
    header = lines[0].split(",")
    index = header.index(column)
    out = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        cells[index] = value
        out.append(",".join(cells))
    return ("﻿" + "\r\n".join(out) + "\r\n").encode("utf-8")


def test_a_resource_with_no_parsed_date_fails_date_unparsed(gov):
    out, work = gov
    bad = _undated(cancelled_body(0), "bitul_dt", "12 March 22")
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: bad})), names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["reason"] == "date_unparsed"
    assert status["resource_id"] == CANCELLED[0]
    assert f"[{CANCELLED[0]}] bitul_dt parsed 0.00% of 40 rows" in status["detail"]
    assert "('99 aaaaa 99', 40)" in status["detail"] and "'12 March 22'" in status["detail"]
    checks = {r["resource_id"]: r.get("checks") for r in status["resources"]}
    assert checks[CANCELLED[2]] is None                                           # no rows: nothing to check
    assert checks[CANCELLED[0]]["dates"]["bitul_dt"]["parsed_share"] == 0.0
    assert checks[CANCELLED[0]]["dates"]["moed_aliya_lakvish"]["parsed_share"] == 1.0
    assert checks[ACTIVE]["dates"]["moed_aliya_lakvish"]["parsed_share"] == 1.0
    assert not (out / "road_survival.sqlite.gz").exists()
    report = B.report(outcome)
    assert "| bitul_dt parsed" in report and "0.00 %" in report and "99 aaaaa 99" in report
    for token in (PLATE, CHASSIS, ENGINE):
        assert token not in report and token not in json.dumps(outcome["status"])


def test_the_report_lists_date_shares_examples_and_ages(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()), names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]["resources"]
    first = stats[CANCELLED[0]]
    assert first["dates"]["bitul_dt"] == {"parsed_share": 1.0, "examples": ["2022-05-10"], "unparsed_examples": [],
                                          "unparsed_patterns": []}
    assert first["age_years"] == {"n": 40, "p10": 5.2, "p50": 5.2, "p90": 5.2}
    assert stats[ACTIVE]["age_years"]["n"] == 0 and stats[ACTIVE]["unkeyed"] == 0
    text = "\n".join(outcome["sections"]["road_survival"])
    assert f"| `{CANCELLED[0]}` | cancelled | 40 | 0 (0.00 %) | 100.00 % | 100.00 % | 5.2 / 5.2 / 5.2 (40) |" in text


# --- F2: codes -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (588, 588), ("588", 588), (588.0, 588), ("588.0", 588), (" 588 ", 588), ("abc", None), ("588.5", None),
    ("1,588", None), ("5.88e2", None), (True, None), (None, None), ("", None),
])
def test_to_int_accepts_whole_numbers_only(value, expected):
    assert to_int(value) == expected


def test_two_percent_unkeyed_cancellations_fail_unkeyed_rows(gov):
    out, work = gov
    lines = active_body().decode("utf-8-sig").splitlines()
    for i in range(1, 7):                                                         # 6 of 300 rows: 2 %
        cells = lines[i].split(",")
        cells[1] = "abc"
        lines[i] = ",".join(cells)
    body = ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
    status = build(out, work, FakeCkan(bodies(**{ACTIVE: body})), names=["road_survival"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "unkeyed_rows" and status["resource_id"] == ACTIVE
    assert f"[{ACTIVE}] unkeyed 6 of 300 rows (2.00%, allowed 1%)" in status["detail"]
    assert "{'tozeret_cd': 'abc', 'degem_cd': '100'}" in status["detail"]
    assert PLATE not in status["detail"] and CHASSIS not in status["detail"]


def test_an_unkeyed_price_row_over_one_percent_fails_unkeyed_rows(gov):
    from test_gov_datasets import price_body
    out, work = gov
    status = build(out, work, FakeCkan(bodies(**{PRICES: price_body(broken=True)})),
                   names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "unkeyed_rows"
    assert "unkeyed 1 of 7 rows" in status["detail"] and "'tozeret_cd': None" in status["detail"]


# --- F3: the price per model name ------------------------------------------------------------------------------------

def mixed_price_body() -> bytes:
    return _csv(PRICE_HEADER, [
        [19, 10, 2007, 175450, "A3", 3, "יבואן ב", "P", "אאודי", "8P", "x"],
        [19, 10, 2007, 670850, "Q7", 3, "יבואן ב", "P", "אאודי", "4L", "x"],
        [413, 100, 2022, 119900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"],
        [413, 100, 2022, 129900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"]])


def test_the_catalogue_a3_gets_only_the_a3_price(gov, tmp_path):
    out, work = gov
    build(out, work, FakeCkan(bodies(**{PRICES: mixed_price_body()})), names=["new_car_prices"])
    serve(out, tmp_path)
    facts, _ = F.price_facts(row(tozeret_cd=19, degem_cd=10, shnat_yitzur=2007, kinuy_mishari="A3"))
    price = facts["original_new_price_ils"]
    assert price["value"] == 175450 and "range" not in price
    assert price["basis"] == "tozeret_cd+degem_cd+shnat_yitzur+kinuy_mishari"
    assert F.price_facts(row(tozeret_cd=19, degem_cd=10, shnat_yitzur=2007, kinuy_mishari="q-7"))[0] == {}  # no Q 7


def test_a_name_absent_from_a_two_name_key_is_withheld(gov, tmp_path):
    out, work = gov
    build(out, work, FakeCkan(bodies(**{PRICES: mixed_price_body()})), names=["new_car_prices"])
    serve(out, tmp_path)
    facts, withheld = F.price_facts(row(tozeret_cd=19, degem_cd=10, shnat_yitzur=2007, kinuy_mishari="TT"))
    assert facts == {} and withheld[0]["reason"] == "price_model_ambiguous"
    assert withheld[0]["field"] == "original_new_price_ils"


def test_a_single_name_key_serves_a_catalogue_name_it_does_not_carry(gov, tmp_path):
    out, work = gov
    build(out, work, FakeCkan(bodies(**{PRICES: mixed_price_body()})), names=["new_car_prices"])
    serve(out, tmp_path)
    facts, _ = F.price_facts(row(kinuy_mishari="COROLLA HYBRID"))
    price = facts["original_new_price_ils"]
    assert price["range"] == [119900, 129900] and price["basis"].endswith("+single_name_key")


def test_the_price_report_counts_multi_name_keys_and_the_model_resolution(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()), names=["road_survival", "new_car_prices"])
    stats = outcome["manifest"]["datasets"]["new_car_prices"]["stats"]
    assert stats["multi_name_keys"] == 0
    # the registry's (key, name) pairs: 413/100/2017 COROLLA and 413/101/2017 YARIS; the price list has 2022 / 2017
    resolution = stats["model_resolution"]
    assert resolution["pairs"] == {"kinuy_mishari": 0, "single_name_key": 0, "price_model_ambiguous": 0}
    assert "price per model name" in "\n".join(outcome["sections"]["new_car_prices"])
    names = {(413, 100, 2022): Counter({"COROLLA": 5, "COROLLA HYBRID": 2}), (19, 10, 2007): Counter({"TT": 1}),
             (9, 9, 2000): Counter({"X": 1})}
    agg = G1.Prices()
    for r in [("19", "10", "2007", "175450", "A3"), ("19", "10", "2007", "670850", "Q7"),
              ("413", "100", "2022", "119900", "COROLLA")]:
        agg.add(dict(zip(("tozeret_cd", "degem_cd", "shnat_yitzur", "mehir", "kinuy_mishari"), r)))
    lines, stats = G1.report(agg, None, names)
    assert stats["multi_name_keys"] == 1
    assert stats["model_resolution"]["pairs"] == {"kinuy_mishari": 1, "single_name_key": 1,
                                                  "price_model_ambiguous": 1}
    assert stats["model_resolution"]["vehicles"] == {"kinuy_mishari": 5, "single_name_key": 2,
                                                     "price_model_ambiguous": 1}


# --- F4: multi-model DEGEM -------------------------------------------------------------------------------------------

MODELS = {"MERCEDES-BENZ": {"VITO": {"VITO"}, "VIANO": {"VIANO"}},
          "TOYOTA": {"COROLLA": {"COROLLA"}, "YARIS": {"YARIS"}, "C HR": {"C-HR"}},
          "BMW": {"X5": {"X5"}, "X6": {"X6"}, "X1": {"X1"}},
          "JEEP": {"GRAND CHEROKEE": {"GRAND CHEROKEE"}, "GRAND CHEROKEE L": {"GRAND CHEROKEE L"}}}


def _map(degems: list[tuple[str, str]]) -> dict:
    agg = G2.Recalls()
    for i, (make, degem) in enumerate(degems):
        agg.add({"RECALL_ID": f"R{i}", "TOZAR_TEUR": make, "DEGEM": degem})
    return G2.propose_map(agg, {}, MODELS)


@pytest.mark.parametrize("make,degem,tokens", [
    ("MERCEDES BENZ", "VITO,VIANO", ["VIANO", "VITO"]),
    ("TOYOTA", "COROLLA YARIS", ["COROLLA", "YARIS"]),
    ("BMW", "X5 X6", ["X5", "X6"]),
    ("TOYOTA", "YARIS C-HR", ["C HR", "YARIS"]),
    ("JEEP", "GRAND CHEROKEE GRAND CHEROKEE L", ["GRAND CHEROKEE", "GRAND CHEROKEE L"]),
])
def test_a_multi_model_degem_gives_one_entry_per_model(make, degem, tokens):
    result = _map([(make, degem)])
    entries = result["entries"]
    assert sorted(e["token"] for e in entries) == tokens
    assert all(e["origin"] == "exact_normalized_equality_split" for e in entries)
    assert {e["degem"] for e in entries} == {" ".join(degem.replace(",", " ").replace("-", " ").split())}
    assert result["unresolved"] == [] and result["split_degems"] == 1


@pytest.mark.parametrize("make,degem", [("BMW", "3"), ("BMW", "X5 Z9"), ("TOYOTA", "COROLLA CROSS BZ4X")])
def test_a_degem_with_an_unknown_part_stays_unresolved(make, degem):
    result = _map([(make, degem)])
    assert result["entries"] == [] and result["unresolved"][0]["reason"] == "no_exact_catalogue_model"


def test_split_entries_merge_under_the_notice_degem():
    pairs = G2.map_pairs(_map([("BMW", "X5 X6")]))
    assert pairs == {("BMW", "X5 X6"): ["X5", "X6"]}


def test_a_split_degem_serves_its_recall_to_each_model(gov, tmp_path):
    out, work = gov
    body = _csv(RECALL_HEADER, [["R-7", 413, "טויוטה", "COROLLA YARIS", "2016-01-01", "2018-12-31", "בלמים", "תקלה",
                                 2019, "רגיל", "החלפה", "כן", "יבואן א", "", ""]])
    outcome = build(out, work, FakeCkan(bodies(**{RECALLS_ID: body})))
    stats = outcome["manifest"]["datasets"]["recall_notices"]["stats"]
    assert stats["split_entries"] == 2 and stats["map_entries_before_split"] == 0 and stats["map_entries"] == 2
    assert stats["unresolved_before_split"] == 1 and stats["unresolved_models"] == 0
    serve(out, tmp_path)
    for model, degem in (("YARIS", 101), ("COROLLA", 100)):
        facts, _ = F.recall_facts(row(kinuy_mishari=model, degem_cd=degem, shnat_yitzur=2017))
        assert [r["recall_id"] for r in facts["recalls"]["value"]] == ["R-7"]
    assert "multi-model DEGEM split: before 0 entries / 1 unresolved; after 2 entries / 0 unresolved" in \
        "\n".join(outcome["sections"]["recall_notices"])


RECALLS_ID = "2c33523f-87aa-44ec-a736-edbb0a82975e"

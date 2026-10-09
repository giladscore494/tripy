"""Government datasets, the #83 review: dates and codes as the datastore returns them, the loud fail-safes that keep a
silent 0 % from ever being built (date_unparsed, unkeyed_rows, the per-cohort undated guard), the exact datastore
fields per role, the price per model name (G1) and the multi-model recall DEGEM (G2). No network."""

from __future__ import annotations

from collections import Counter

import pytest

from test_gov_datasets import (ACTIVE, CANCELLED, CHASSIS, ENGINE, PLATE, PRICE_HEADER, SURV_COLUMNS, FakeCkan, B,
                               _csv, bodies, build, gov, row, serve)  # noqa: F401 - gov is a fixture
from test_gov_datasets_fallback import FORBIDDEN, FallbackCkan, store_of
from src.gov_data import facts as F
from src.gov_data import prices as G1
from src.gov_data import recalls as G2
from src.gov_data import survival as G3
from src.gov_data.names import shape, to_int, ym_of

SURVIVAL = (*CANCELLED, ACTIVE)


# --- F1: dates ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2019-03", "2019-3", "201903", 201903, "20190312", "2019-03-12",
                                   "2019-03-12T00:00:00", "2019-03-12T00:00:00.000", "2019-03-12T00:00:00Z",
                                   "2019-03-12T00:00:00.123Z", "2019-03-12 00:00:00", "12/03/2019"])
def test_every_stated_date_format_parses(value):
    assert ym_of(value) == (2019, 3)


def test_a_cell_never_parses_as_a_date_by_accident():
    assert ym_of("1552348800000") is None and ym_of("2019-13-01") is None and ym_of("abc") is None
    assert ym_of(None) is None and ym_of("") is None


def test_a_cohort_with_ten_dated_cancellations_has_shares_and_ages():
    agg = G3.Survival()
    for _ in range(290):
        agg.add({"_role": "active", "_resource_id": "a", "tozeret_cd": "588", "degem_cd": "292",
                 "shnat_yitzur": "2008", "moed_aliya_lakvish": "2008-03"})
    for _ in range(10):
        agg.add({"_role": "cancelled", "_resource_id": "c", "tozeret_cd": "588.0", "degem_cd": 292.0,
                 "shnat_yitzur": "2008", "moed_aliya_lakvish": "2008-03-01T00:00:00", "bitul_dt": "2015-03-12T00:00:00"})
    rows, stats = G3.cohorts(agg, G3.BASIS_MODEL_YEAR, 2026 * 12 + 9, 200, (3, 20))
    cohort = rows[0]
    assert (cohort["cohort_size"], cohort["cancelled_count"], cohort["dated_cancelled"]) == (300, 10, 10)
    assert cohort["age_median"] == 7.0 and cohort["shares"]["10"] == round(10 / 300, 4) > 0
    assert cohort["shares"]["3"] == 0.0 and stats["undated_cohorts"] == 0
    assert G3.checks(agg) == []
    view = agg.resources["c"].view()
    assert view["cancelled_age_years"] == {"p10": 7.0, "p50": 7.0, "p90": 7.0, "n": 10}
    assert view["dates"]["bitul_dt"] == {"parsed": 10, "parsed_share": 1.0}


def test_a_cohort_whose_cancellations_are_undated_gets_no_share_never_a_zero():
    agg = G3.Survival()
    for _ in range(250):
        agg.add({"_role": "active", "_resource_id": "a", "tozeret_cd": 1, "degem_cd": 2, "shnat_yitzur": 2008,
                 "moed_aliya_lakvish": "2008-03"})
    for n in range(50):                                        # 10 dated; 40 cancelled before their first road month
        agg.add({"_role": "cancelled", "_resource_id": "c", "tozeret_cd": 1, "degem_cd": 2, "shnat_yitzur": 2008,
                 "moed_aliya_lakvish": "2008-03", "bitul_dt": "2015-01-01" if n < 10 else "2007-01-01"})
    rows, stats = G3.cohorts(agg, G3.BASIS_MODEL_YEAR, 2026 * 12, 200, (3, 20))
    assert rows[0]["shares"] is None and rows[0]["age_median"] is None and rows[0]["dated_cancelled"] == 10
    assert stats["undated_cohorts"] == 1 and stats["bad_dates"] == 40


# --- F2: codes ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [588, "588", 588.0, "588.0", " 588 ", "588.00"])
def test_a_code_parses_as_stated(value):
    assert to_int(value) == 588


@pytest.mark.parametrize("value", ["abc", "588.5", 588.5, "5,88", "5e2", "-1", -1, True, None, "", "58 8", float("nan")])
def test_anything_else_is_not_a_code(value):
    assert to_int(value) is None


def test_the_format_pattern_never_carries_a_value():
    assert shape("2019-03-12T00:00:00") == "9999-99-99a99:99:99"
    assert shape("588.0") == "999.9" and shape("טויוטה 12") == "aaaaaa 99" and shape("1552348800000") == "9" * 13
    assert shape("x" * 40).endswith("…") and len(shape("x" * 40)) == 33


# --- the loud fail-safes on a build ---------------------------------------------------------------------------------------

def cancelled_rows(n: int, *, bitul="2022-05-10", road="2017-3", tozeret_cd=413, unkeyed: int = 0,
                   unkeyed_code="X1") -> bytes:
    rows = [[f"{PLATE}C{i}", tozeret_cd, "P", "טויוטה יפן", 100, "ZWE", "", "", f"{CHASSIS}C{i}", f"{ENGINE}C{i}",
             road, "COROLLA", "בנזין", bitul] for i in range(n)]
    rows += [[f"{PLATE}U{i}", unkeyed_code, "P", "טויוטה יפן", 100, "ZWE", "", "", f"{CHASSIS}U{i}",
              f"{ENGINE}U{i}", road, "COROLLA", "בנזין", bitul] for i in range(unkeyed)]
    return _csv(SURV_COLUMNS + ["bitul_dt"], rows)


def test_a_resource_with_no_parsed_date_fails_date_unparsed_with_the_patterns(gov):
    out, work = gov
    first = build(out, work, FakeCkan(bodies()), names=["road_survival"])
    assert first["status"][0]["status"] == "built"
    before = (out / "road_survival.sqlite.gz").read_bytes()
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: cancelled_rows(40, bitul="1652140800000")})),
                    names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["reason"] == "date_unparsed"
    assert status["resource_id"] == CANCELLED[0] and "bitul_dt: 0 of 40 stated values parse" in status["detail"]
    assert status["findings"] == [{"reason": "date_unparsed", "resource_id": CANCELLED[0], "column": "bitul_dt",
                                   "patterns": [("9999999999999", 40)]}]
    assert (out / "road_survival.sqlite.gz").read_bytes() == before          # the previous snapshot is kept
    text = B.report(outcome)
    assert "failed: date_unparsed" in text and "`9999999999999` (40)" in text
    assert "| `" + CANCELLED[0] + "` | cancelled | 40 | 0 / 40 | 0 / 40 | 0 / 40 | 0 / 40 | 40 / 0 |" in text
    assert "1652140800000" not in text                                          # a pattern, never the value
    for token in (PLATE, CHASSIS, ENGINE):
        assert token not in text


def test_two_percent_unkeyed_rows_fail_unkeyed_rows(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: cancelled_rows(49, unkeyed=1)})),
                    names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["reason"] == "unkeyed_rows"
    assert "1 of 50 rows unkeyed (2.00 %" in status["detail"]
    assert status["findings"][0]["patterns"] == [("a9", 1)]
    # 1 % exactly is still built
    ok = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: cancelled_rows(99, unkeyed=1)})), names=["road_survival"])
    assert ok["status"][0]["status"] == "built"


def test_a_built_survival_reports_the_per_resource_tables(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()), names=["road_survival"])
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    first = stats["resources"][CANCELLED[0]]
    assert first["dates"]["bitul_dt"] == {"parsed": 40, "parsed_share": 1.0}
    assert first["null"]["shnat_yitzur"] == 40 and first["non_null"]["tozeret_cd"] == 40
    assert first["cancelled_age_years"] == {"p10": 5.2, "p50": 5.2, "p90": 5.2, "n": 40}
    assert stats["resources"][ACTIVE]["cancelled_age_years"] is None
    text = B.report(outcome)
    assert "moed_aliya_lakvish parsed" in text and "- none: every stated date and code parsed" in text


def test_the_datastore_fields_per_role_are_exactly_the_projection(gov):
    out, work = gov
    files = bodies()
    http = FallbackCkan(files, file_answers={rid: [FORBIDDEN] for rid in SURVIVAL},
                        datastore={rid: store_of(files[rid]) for rid in SURVIVAL})
    outcome = build(out, work, http, names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    sent = {r["resource_id"]: r["fields"].split(",") for r in http.datastore_requests if "sort" in r}
    kept = ["tozeret_nm", "degem_nm", "shnat_yitzur", "ramat_gimur", "kinuy_mishari", "sug_delek_nm"]
    for rid in CANCELLED[:2]:            # the third fixture resource is empty: one page, same fields
        assert sent[rid] == ["tozeret_cd", "degem_cd", "moed_aliya_lakvish", "bitul_dt", *kept]
    assert sent[CANCELLED[2]] == ["tozeret_cd", "degem_cd", "moed_aliya_lakvish", "bitul_dt", *kept]
    assert sent[ACTIVE] == ["tozeret_cd", "degem_cd", "moed_aliya_lakvish", *kept]


# --- F3: the price per model name ---------------------------------------------------------------------------------------

def price_rows(*rows) -> bytes:
    return _csv(PRICE_HEADER, [list(r) for r in rows])


AUDI_KEY = [(19, 10, 2007, 175450, "A3", 3, "יבואן ג", "P", "אאודי גרמניה", "8P", "x"),
            (19, 10, 2007, 670850, "Q7", 3, "יבואן ג", "P", "אאודי גרמניה", "4L", "x"),
            (413, 100, 2022, 119900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"),
            (413, 100, 2022, 129900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x")]


def test_the_catalogue_a3_gets_only_the_a3_price(gov, tmp_path):
    from test_gov_datasets import PRICES

    out, work = gov
    build(out, work, FakeCkan(bodies(**{PRICES: price_rows(*AUDI_KEY)})), names=["new_car_prices"])
    serve(out, tmp_path)
    audi = dict(tozar="אאודי", tozeret_cd=19, degem_cd=10, shnat_yitzur=2007)
    facts, _ = F.price_facts(row(**audi, kinuy_mishari="A3"))
    price = facts["original_new_price_ils"]
    assert price["value"] == 175450 and "range" not in price
    assert price["basis"] == "tozeret_cd+degem_cd+shnat_yitzur+kinuy_mishari"
    assert F.price_facts(row(**audi, kinuy_mishari=" q7 "))[0]["original_new_price_ils"]["value"] == 670850
    facts, withheld = F.price_facts(row(**audi, kinuy_mishari="TT"))
    assert facts == {} and withheld == [{"source": "gov_new_car_prices", "field": "original_new_price_ils",
                                         "reason": "price_model_ambiguous", "values": ["A3", "Q7"],
                                         "detail": "the key's rows name several models and none is the catalogue "
                                                   "model"}]
    # a single-name key serves its rows to a catalogue name it does not carry; the range rule still applies
    facts, _ = F.price_facts(row(kinuy_mishari="COROLLA HYBRID"))
    price = facts["original_new_price_ils"]
    assert price["range"] == [119900, 129900] and price["n_prices"] == 2
    assert price["basis"] == "tozeret_cd+degem_cd+shnat_yitzur (one kinuy_mishari)"


def test_the_price_name_resolution_report():
    agg = G1.Prices()
    for r in AUDI_KEY:
        agg.add(dict(zip(PRICE_HEADER, r)))
    names = Counter({(19, 10, 2007, "A3"): 5, (19, 10, 2007, "TT"): 2, (413, 100, 2022, "COROLLA HYBRID"): 7,
                     (1, 1, 2000, "NOT IN THE LIST"): 9})
    lines, stats = G1.name_resolution(agg, names)
    assert stats == {"keys_multi_kinuy": 1, "registry_pairs": {"name": 1, "single_name_key": 1,
                                                                "price_model_ambiguous": 1},
                     "registry_vehicles": {"name": 5, "single_name_key": 7, "price_model_ambiguous": 2}}
    assert "| price_model_ambiguous | 1 | 33.3 % | 2 | 14.3 % |" in lines


def test_a_price_with_thousands_separators_still_parses():
    agg = G1.Prices()
    agg.add({"tozeret_cd": "413", "degem_cd": "100", "shnat_yitzur": "2022", "mehir": " 145,900 ",
             "kinuy_mishari": "COROLLA"})
    assert agg.unkeyed == 0 and next(iter(agg.groups))[3] == 145900


# --- F4: multi-model DEGEM ----------------------------------------------------------------------------------------------

MODELS = {"MERCEDES-BENZ": {"VITO": {"VITO"}, "VIANO": {"VIANO"}},
          "TOYOTA": {"COROLLA": {"COROLLA"}, "YARIS": {"YARIS"}, "C HR": {"C-HR"}},
          "BMW": {"X5": {"X5"}, "X6": {"X6"}, "X1": {"X1"}, "3 SERIES": {"3 SERIES"}},
          "JEEP": {"GRAND CHEROKEE": {"GRAND CHEROKEE"}, "GRAND CHEROKEE L": {"GRAND CHEROKEE L"},
                   "WRANGLER": {"WRANGLER"}},
          "CHRYSLER": {"TOWN AND COUNTRY": {"TOWN & COUNTRY"}}}


def recalls(*pairs) -> G2.Recalls:
    agg = G2.Recalls()
    for n, (teur, degem) in enumerate(pairs):
        agg.add({"RECALL_ID": f"R-{n}", "TOZAR_TEUR": teur, "DEGEM": degem, "BUILD_BEGIN_A": "2010-01-01",
                 "BUILD_END_A": "2012-12-31"})
    return agg


def entries_of(new_map: dict, make: str, degem: str) -> list[dict]:
    return [e for e in new_map["entries"] if e["make"] == make and e["degem"] == degem]


def test_vito_viano_and_corolla_yaris_split_into_two_entries():
    new_map = G2.propose_map(recalls(("MERCEDES BENZ", "VITO,VIANO"), ("TOYOTA", "COROLLA YARIS")), {}, MODELS)
    vito = entries_of(new_map, "MERCEDES-BENZ", "VITO VIANO")
    assert [(e["split_token"], e["kinuy_mishari"], e["origin"]) for e in vito] == [
        ("VIANO", ["VIANO"], "exact_normalized_equality"), ("VITO", ["VITO"], "exact_normalized_equality")]
    assert [e["split_token"] for e in entries_of(new_map, "TOYOTA", "COROLLA YARIS")] == ["COROLLA", "YARIS"]
    pairs = G2.map_pairs(new_map)
    assert pairs[("MERCEDES-BENZ", "VITO VIANO")] == ["VIANO", "VITO"]          # one pair, both models
    assert new_map["unresolved"] == [] and new_map["entries_before"] == 0


def test_a_degem_with_an_unknown_word_stays_unresolved_no_partial_match():
    new_map = G2.propose_map(recalls(("BMW", "BMW 3"), ("BMW", "X5 X6"), ("BMW", "X5 X7"), ("BMW", "3")), {}, MODELS)
    assert [e["split_token"] for e in entries_of(new_map, "BMW", "X5 X6")] == ["X5", "X6"]
    unresolved = {u["degem"]: u["reason"] for u in new_map["unresolved"]}
    assert unresolved == {"BMW 3": "no_exact_catalogue_model", "X5 X7": "no_exact_catalogue_model",
                          "3": "no_exact_catalogue_model"}
    assert not entries_of(new_map, "BMW", "X5 X7")                              # X5 alone is never proposed


def test_multi_word_models_split_whole():
    assert G2.split_models("GRAND CHEROKEE GRAND CHEROKEE L", MODELS["JEEP"]) == ["GRAND CHEROKEE", "GRAND CHEROKEE L"]
    assert G2.split_models("WRANGLER GRAND CHEROKEE", MODELS["JEEP"]) == ["WRANGLER", "GRAND CHEROKEE"]
    assert G2.split_models("YARIS C-HR", MODELS["TOYOTA"]) == ["YARIS", "C HR"]
    assert G2.split_models("TOWN AND COUNTRY", MODELS["CHRYSLER"]) is None            # one model: the exact path
    assert G2.split_models("X5 X5", MODELS["BMW"]) is None                              # one model, named twice
    assert G2.split_models("X5;X6/X1", MODELS["BMW"]) == ["X5", "X6", "X1"]


def test_a_split_degem_resolves_its_recall_for_each_model_and_the_report_counts_it():
    new_map = G2.propose_map(recalls(("TOYOTA", "COROLLA YARIS"), ("BMW", "X5 X7")),
                             {"entries": [{"make": "TOYOTA", "degem": "AVENSIS", "kinuy_mishari": ["AVENSIS"],
                                           "origin": "reviewer"}]}, MODELS)
    lines, stats = G2.report(recalls(("TOYOTA", "COROLLA YARIS")), {"codes": 0, "agreeing": 0, "rate": 0.0}, False,
                             new_map, 0.98)
    assert (stats["map_entries_before"], stats["map_entries"], stats["split_entries"], stats["split_degem"]) == \
        (1, 3, 2, 1)
    assert "model map: 3 entries (before this build: 1; 2 of them from 1 multi-model DEGEM" in "\n".join(lines)
    pairs = G2.map_pairs(new_map)
    assert {k for k, v in pairs.items() if "YARIS" in v} == {("TOYOTA", "COROLLA YARIS")}

"""road_survival after automation PR #87: ages in whole years from the model year (S1), the degem_nm fallback for
cancellations without degem_cd (S2) and the per-cohort unkeyed guard; reports in counts and shapes only. No network:
the fake CKAN of test_gov_datasets."""

from __future__ import annotations

import json

from test_gov_datasets import (ACTIVE, CANCELLED, CHASSIS, ENGINE, PLATE, SURV_COLUMNS, B, F, FakeCkan, _csv,
                               bodies, build, gov, row, serve)  # noqa: F401 - gov is a fixture
from src.gov_data import survival as G3
from src.gov_data.names import norm_code


def active(n: int, *, tc=1, dc=2, year=2010, name="ABC 12") -> list[dict]:
    return [{"_role": "active", "_resource_id": "a", "tozeret_cd": tc, "degem_cd": dc, "degem_nm": name,
             "shnat_yitzur": year, "moed_aliya_lakvish": f"{year}-3"} for _ in range(n)]


def cancelled(n: int, bitul: str, *, tc=1, dc=2, year=2010, name="ABC 12", rid="c") -> list[dict]:
    return [{"_role": "cancelled", "_resource_id": rid, "tozeret_cd": tc, "degem_cd": dc, "degem_nm": name,
             "shnat_yitzur": year, "moed_aliya_lakvish": "", "bitul_dt": bitul} for _ in range(n)]


def survival(*groups: list[dict]) -> G3.Survival:
    agg = G3.Survival()
    for group in groups:
        for item in group:
            agg.add(item)
    agg.resolve()
    return agg


def cohort(agg: G3.Survival, ref_year: int = 2026, **key) -> tuple[dict, dict]:
    rows, stats = G3.cohorts(agg, ref_year, 200, (3, 20))
    want = {"tozeret_cd": 1, "degem_cd": 2, "cohort_year": 2010, **key}
    return next(r for r in rows if all(r[k] == v for k, v in want.items())), stats


# --- S1: whole years from the model year ----------------------------------------------------------------------------------

def test_a_2010_cohort_cancelled_in_2015_and_2018_has_its_shares_at_5_and_8():
    agg = survival(active(290), cancelled(4, "2015-03-12"), cancelled(6, "2018-11-30"))
    row_, _ = cohort(agg)
    assert row_["cohort_size"] == 300 and row_["dated_cancelled"] == 10 and row_["withheld"] is None
    shares = row_["shares"]
    assert shares["4"] == 0.0 and shares["5"] == round(4 / 300, 4) and shares["7"] == round(4 / 300, 4)
    assert shares["8"] == round(10 / 300, 4) and shares["15"] == round(10 / 300, 4)
    assert "16" not in shares and "20" not in shares                       # 2026 - 2010 = 16 >= a + 1: up to 15
    assert row_["age_median"] == 8 and row_["age_p25"] == 5 and isinstance(row_["age_median"], int)


def test_the_reference_year_decides_the_last_age():
    agg = survival(active(300))
    assert "16" in cohort(agg, ref_year=2027)[0]["shares"] and "16" not in cohort(agg, ref_year=2026)[0]["shares"]


def test_a_cancellation_before_its_model_year_is_a_bad_date_without_age():
    agg = survival(active(290), cancelled(10, "2012-01-01"), cancelled(1, "2009-12-31"))
    row_, stats = cohort(agg)
    assert stats["bad_dates"] == 1 and row_["dated_cancelled"] == 10 and row_["cancelled_count"] == 11


def _year_only(body: bytes, value: str) -> bytes:
    lines = body.decode("utf-8-sig").splitlines()
    index = lines[0].split(",").index("moed_aliya_lakvish")
    out = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        cells[index] = value
        out.append(",".join(cells))
    return ("﻿" + "\r\n".join(out) + "\r\n").encode("utf-8")


def test_an_empty_moed_aliya_lakvish_does_not_fail_the_build(gov, tmp_path):
    from test_gov_datasets import cancelled_body

    out, work = gov
    files = {CANCELLED[0]: _year_only(cancelled_body(0), ""), CANCELLED[1]: _year_only(cancelled_body(1), "")}
    outcome = build(out, work, FakeCkan(bodies(**files)), names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    assert stats["resources"][CANCELLED[0]]["moed_aliya_lakvish"]["filled_share"] == 0.0
    serve(out, tmp_path)
    facts, _ = F.survival_facts(row(shnat_yitzur=2017))
    value = facts["road_survival"]["value"]
    assert value["median_age_at_final_cancellation"] == 5
    assert "הגיל מחושב בשנים שלמות: שנת הביטול פחות שנת הייצור." in value["definition_he"]


# --- S2: the degem_nm fallback ----------------------------------------------------------------------------------------------

def test_a_cancellation_without_degem_cd_takes_the_one_degem_cd_of_its_name():
    assert norm_code(" abc-1.2 ") == norm_code("ABC12") == "ABC12"
    agg = survival(active(290), cancelled(5, "2015-01-01"),
                   cancelled(5, "2016-01-01", tc="0001", dc=None, name="abc-12"))       # zero-padded, no degem_cd
    row_, _ = cohort(agg)
    assert row_["cancelled_count"] == 10 and row_["via_degem_nm"] == 5
    stats = agg.resources["c"]
    assert (stats.direct, stats.via_name, stats.unkeyed_total()) == (5, 5, 0)


def test_a_name_with_two_degem_cd_stays_unkeyed_ambiguous():
    agg = survival(active(200), active(100, dc=3), cancelled(5, "2016-01-01", dc=None),
                   cancelled(2, "2016-01-01", dc=None, name="NEVER SEEN"))
    stats = agg.resources["c"]
    assert stats.via_name == 0 and stats.unkeyed["degem_nm_ambiguous"] == 5
    assert stats.unkeyed["degem_nm_unknown"] == 2 and agg.unkeyed_by_make_year[(1, 2010)] == 7


def _cancel_rows(keyed: int, unknown: int, *, filler: int = 0) -> bytes:
    rows = [[f"{PLATE}K{i}", 413, "P", "טויוטה יפן", 100, "ZWE", "", 2017, f"{CHASSIS}K{i}", f"{ENGINE}K{i}", "",
             "COROLLA", "בנזין", "2022-05-10"] for i in range(keyed)]
    rows += [[f"{PLATE}U{i}", "0413", "P", "טויוטה יפן", "", "NO SUCH CODE", "", 2017, f"{CHASSIS}U{i}",
              f"{ENGINE}U{i}", "2017", "COROLLA", "בנזין", "2022-05-10"] for i in range(unknown)]
    rows += [[f"{PLATE}F{i}", 413, "P", "טויוטה יפן", 102, "NGX", "", 2015, f"{CHASSIS}F{i}", f"{ENGINE}F{i}", "",
              "C-HR", "בנזין", "2021-01-01"] for i in range(filler)]
    return _csv(SURV_COLUMNS + ["bitul_dt"], rows)


def test_six_percent_still_unkeyed_fails_unkeyed_rows_with_shapes_only(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: _cancel_rows(47, 3)})), names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["reason"] == "unkeyed_rows"
    assert f"[{CANCELLED[0]}] unkeyed 3 of 50 rows (6.00%, allowed 5%) after the degem_nm fallback" in status["detail"]
    assert "'degem_nm_unknown': 3" in status["detail"] and "NO SUCH CODE" not in status["detail"]
    text = B.report(outcome)
    assert "NO SUCH CODE" not in text and PLATE not in text and CHASSIS not in text


def test_a_cancellation_keyed_via_degem_nm_in_a_build(gov):
    out, work = gov
    body = _cancel_rows(40, 0)
    lines = body.decode("utf-8-sig").splitlines()
    for i in range(1, 6):                                       # 5 rows without degem_cd, named as the registry names
        cells = lines[i].split(",")
        cells[4] = ""
        lines[i] = ",".join(cells)
    files = {CANCELLED[0]: ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")}
    outcome = build(out, work, FakeCkan(bodies(**files)), names=["road_survival"])
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    first = stats["resources"][CANCELLED[0]]
    assert (first["keyed_directly"], first["keyed_via_degem_nm"], first["unkeyed"]) == (35, 5, 0)
    assert stats["keyed_via_degem_nm"] == 5
    text = "\n".join(outcome["sections"]["road_survival"])
    assert f"| `{CANCELLED[0]}` | cancelled | 40 | 100.00 % | 100.00 % | 0.00 % (0.00 %) | 35 | 5 |" in text


# --- the per-cohort unkeyed guard ------------------------------------------------------------------------------------------

def test_fifteen_percent_unknown_degem_cancellations_withhold_the_cohort():
    agg = survival(active(280), cancelled(20, "2016-01-01"), cancelled(3, "2016-01-01", dc=None, name="OTHER"))
    row_, stats = cohort(agg)
    assert row_["unkeyed_same_make_year"] == 3 and row_["withheld"] == "unkeyed_cancellations"
    assert row_["shares"] is None and stats["unkeyed_cohorts"] == 1
    ok = survival(active(280), cancelled(20, "2016-01-01"), cancelled(2, "2016-01-01", dc=None, name="OTHER"))
    assert cohort(ok)[0]["withheld"] is None                                     # 2 of 20: 10 %, not over


def test_the_guard_reaches_the_facts_and_the_report(gov, tmp_path):
    out, work = gov
    files = {CANCELLED[0]: _cancel_rows(40, 6, filler=100)}                      # 6 of 146 unkeyed: 4.1 %
    outcome = build(out, work, FakeCkan(bodies(**files)), names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    assert stats["withheld"]["unkeyed_cancellations"] == 1                       # 6 > 10 % of its 50 cancellations
    text = "\n".join(outcome["sections"]["road_survival"])
    assert "unkeyed_cancellations (the unkeyed cancellations of its (tozeret_cd, shnat_yitzur) over 10%" in text
    assert "- sanity: share by age 10 over the cohorts of model years 2005–2014" in text
    assert "20 largest cohorts:" in text and "| withheld |" in text
    serve(out, tmp_path)
    facts, withheld = F.survival_facts(row(shnat_yitzur=2017))
    assert facts == {} and withheld[0]["reason"] == "unkeyed_cancellations"
    assert "6 unkeyed cancellations of the same tozeret_cd and year" in withheld[0]["detail"]


def test_the_report_carries_the_via_share_and_the_sanity_distribution():
    agg = survival(active(290, year=2008), cancelled(5, "2015-01-01", year=2008),
                   cancelled(5, "2019-01-01", year=2008, dc=None))
    rows, stats = G3.cohorts(agg, 2026, 200, (3, 20))
    lines, summary = G3.report(agg, agg.year_coverage(), rows, stats, G3.model_year_map(agg), 200, 2026)
    assert summary["via_degem_nm_share_per_cohort"] == {"n": 1, "p10": 0.5, "p50": 0.5, "p90": 0.5}
    assert summary["share_by_10_model_years_2005_2014"] == {"n": 1, "p10": round(5 / 300, 4),
                                                            "p50": round(5 / 300, 4), "p90": round(5 / 300, 4)}
    assert summary["withheld"] == {"small_cohort": 0, "unkeyed_cancellations": 0, "undated_cancellations": 0}
    assert json.dumps(summary, default=str).find("ABC 12") == -1                  # no degem_nm value in the summary

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


def anchor(n: int = 10) -> list[dict]:
    """Cancellations dated 1999 of another key and resource: coverage_start 1999, before every test cohort."""
    return cancelled(n, "1999-06-01", tc=99, dc=9, year=1995, name="OLD 1", rid="anchor")


def survival(*groups: list[dict], coverage: bool = True) -> G3.Survival:
    agg = G3.Survival()
    for group in groups + ((anchor(),) if coverage else ()):
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


def test_twenty_two_percent_still_unkeyed_fails_unkeyed_rows_with_shapes_only(gov):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: _cancel_rows(39, 11)})), names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["reason"] == "unkeyed_rows"
    assert f"[{CANCELLED[0]}] unkeyed 11 of 50 rows (22.00%, allowed 20%) after the degem_nm fallback" in \
        status["detail"]
    assert "'degem_nm_unknown': 11" in status["detail"] and "NO SUCH CODE" not in status["detail"]
    text = B.report(outcome)
    assert "NO SUCH CODE" not in text and PLATE not in text and CHASSIS not in text


def test_twelve_percent_unkeyed_builds_and_the_cohort_guard_withholds_the_cohort(gov, tmp_path):
    out, work = gov
    # 6 of 50 cancellation rows unkeyed (12 %, under the 20 % resource limit); all of (413, 2017): the cohort
    # 413/100/2017 has 44 cancellations, so 6 > 10 % of 44 -> withheld unkeyed_cancellations
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: _cancel_rows(44, 6)})), names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    assert stats["withheld"]["unkeyed_cancellations"] == 1
    assert stats["active_in_withheld"]["unkeyed_cancellations"] == 250
    assert stats["active_share_in_withheld"]["unkeyed_cancellations"] == round(250 / stats["active_in_cohorts"], 4)
    text = "\n".join(outcome["sections"]["road_survival"])
    assert "- active vehicles in withheld cohorts (of 300 in all cohorts): " in text
    assert "unkeyed_cancellations 250 (83.33 %)" in text
    serve(out, tmp_path)
    facts, withheld = F.survival_facts(row(shnat_yitzur=2017))
    assert facts == {} and withheld[0]["reason"] == "unkeyed_cancellations"


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
    assert "unkeyed_cancellations (the unkeyed and placeholder cancellations of its (tozeret_cd, shnat_yitzur) " \
           "over 10%" in text
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
    # the anchor cohort (1995, cancellations dated 1999, none via degem_nm) is the second cohort with a cancellation
    assert summary["via_degem_nm_share_per_cohort"] == {"n": 2, "p10": 0.0, "p50": 0.0, "p90": 0.5}
    assert summary["share_by_10_model_years_2005_2014"] == {"n": 1, "p10": round(5 / 300, 4),
                                                            "p50": round(5 / 300, 4), "p90": round(5 / 300, 4)}
    assert summary["withheld"] == {"placeholder_model_code": 0, "small_cohort": 1, "left_truncated": 0,
                                   "unkeyed_cancellations": 0, "undated_cancellations": 0}
    assert json.dumps(summary, default=str).find("ABC 12") == -1                  # no degem_nm value in the summary


# --- P1: degem_cd 0 is a placeholder ----------------------------------------------------------------------------------

def test_a_degem_cd_0_cohort_gets_no_survival_fields():
    agg = survival(active(250, dc=0), cancelled(50, "2016-01-01", dc=0))
    row_, stats = cohort(agg, degem_cd=0)
    assert row_["withheld"] == "placeholder_model_code" and row_["shares"] is None and row_["age_median"] is None


def test_the_fallback_never_maps_to_0_and_a_0_only_name_is_a_placeholder_row():
    # (1, "ZERO", 2010) is known only with degem_cd 0; (1, "XYZ 3", 2010) with {0, 5}
    agg = survival(active(300), cancelled(1, "2016-01-01", dc=0, name="ZERO"),
                   cancelled(1, "2016-01-01", dc=0, name="XYZ 3"), active(250, dc=5, name="XYZ 3"),
                   cancelled(8, "2016-01-01", dc=None, name="ZERO"), cancelled(4, "2016-01-01", dc=None, name="XYZ 3"),
                   cancelled(3, "2016-01-01", dc=None, year=1998, name="OLD NAME"),
                   cancelled(1, "1999-01-01", dc=0, year=1998, name="OLD NAME"))
    stats = agg.resources["c"]
    assert stats.placeholder == {"from_2000": 8, "before_2000": 3}
    assert stats.unkeyed_total() == 0 and stats.unkeyed_share() == 0.0           # not toward the file limit
    assert stats.via_name == 4                                                    # {0, 5} -> 5
    assert agg.placeholder_by_make_year[(1, 2010)] == 8 and agg.unkeyed_by_make_year[(1, 2010)] == 0
    five, _ = cohort(agg, degem_cd=5)
    assert five["via_degem_nm"] == 4 and five["cancelled_count"] == 4
    summary = G3.report(agg, agg.year_coverage(), *G3.cohorts(agg, 2026, 200, (3, 20)), G3.model_year_map(agg),
                        200, 2026)[1]
    assert summary["degem_nm_placeholder"] == {"before_2000": 3, "from_2000": 8}


def test_placeholder_rows_count_in_the_cohort_guard():
    # 300 active + 50 cancellations of 1/2/2010; 6 placeholder rows of (1, 2010): 6 > 10 % of 50
    agg = survival(active(300), cancelled(50, "2016-01-01"), active(10, dc=0, name="ZERO"),
                   cancelled(6, "2016-01-01", dc=None, name="ZERO"))
    row_, stats = cohort(agg)
    assert row_["withheld"] == "unkeyed_cancellations" and row_["placeholder_same_make_year"] == 6
    assert row_["unkeyed_same_make_year"] == 0 and stats["guard_due_to_placeholder_from_2000"] == 1


# --- left truncation ----------------------------------------------------------------------------------------------

def test_coverage_start_is_the_earliest_year_with_one_percent_of_the_cancellations():
    agg = survival(active(300), cancelled(1, "1995-01-01"), cancelled(150, "2000-05-01"), cancelled(49, "2010-01-01"),
                   coverage=False)
    assert agg.coverage_start() == 2000                       # 1995 holds 1 of 200 rows: 0.5 %, under 1 %
    assert survival(active(10), coverage=False).coverage_start() is None


def test_a_model_year_before_coverage_start_is_left_truncated(gov, tmp_path):
    agg = survival(active(300, year=1996), cancelled(60, "2004-01-01", year=1996), active(300, year=2001),
                   cancelled(60, "2009-01-01", year=2001), coverage=False)
    old, stats = cohort(agg, cohort_year=1996)
    new, _ = cohort(agg, cohort_year=2001)
    assert stats["coverage_start"] == 2004
    assert old["withheld"] == "left_truncated" and old["shares"] is None and old["age_median"] is None
    assert new["withheld"] == "left_truncated"                 # 2001 < 2004 too
    agg = survival(active(300, year=2005), cancelled(60, "2009-01-01", year=2005), coverage=False)
    served, stats = cohort(agg, cohort_year=2005)
    assert stats["coverage_start"] == 2009 and served["withheld"] == "left_truncated"
    agg = survival(active(300, year=2010), cancelled(60, "2009-01-01", year=2008), cancelled(60, "2015-01-01"),
                   coverage=False)
    served, stats = cohort(agg)
    assert stats["coverage_start"] == 2009 and served["withheld"] is None and served["age_median"] == 5
    summary = G3.report(agg, agg.year_coverage(), *G3.cohorts(agg, 2026, 200, (3, 20)), G3.model_year_map(agg),
                        200, 2026)[1]
    assert summary["coverage_start"] == 2009


def test_a_fully_cancelled_cohort_inside_the_coverage_is_served():
    agg = survival(cancelled(250, "2016-01-01", year=2008))
    row_, stats = cohort(agg, cohort_year=2008)
    assert stats["coverage_start"] == 1999 and row_["active_count"] == 0
    assert row_["withheld"] is None and row_["shares"]["10"] == 1.0 and row_["age_median"] == 8
    summary = G3.report(agg, agg.year_coverage(), *G3.cohorts(agg, 2026, 200, (3, 20)), G3.model_year_map(agg),
                        200, 2026)[1]
    assert summary["zero_active_cohorts_served"] == 1


def test_left_truncated_and_placeholder_reach_the_facts_side(gov, tmp_path):
    out, work = gov
    lines = cancelled_body_with(year=1996)
    outcome = build(out, work, FakeCkan(bodies(**{CANCELLED[0]: lines})), names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    assert stats["coverage_start"] == 1999
    serve(out, tmp_path)
    facts, withheld = F.survival_facts(row(tozeret_cd=413, degem_cd=100, shnat_yitzur=1996))
    assert facts == {} and withheld[0]["reason"] == "left_truncated"
    assert "coverage_start 1999" in withheld[0]["detail"]


def cancelled_body_with(year: int) -> bytes:
    """250 cancellations of 413/100 of model `year`, dated 2005."""
    rows = [[f"{PLATE}Y{i}", 413, "P", "טויוטה יפן", 100, "ZWE", "", year, f"{CHASSIS}Y{i}", f"{ENGINE}Y{i}", "",
             "COROLLA", "בנזין", "2005-01-01"] for i in range(250)]
    return _csv(SURV_COLUMNS + ["bitul_dt"], rows)

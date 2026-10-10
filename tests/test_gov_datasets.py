"""Government datasets (src/gov_data, scripts/build_gov_datasets.py): the CKAN ingestion with its fail-safes, the three
aggregations and their facts API fields. No network: a fake CKAN serves fixture CSV bodies."""

from __future__ import annotations

import copy
import gzip
import io
import json
import logging
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_gov_datasets as B  # noqa: E402

from src.gov_data import facts as F  # noqa: E402
from src.gov_data import recalls as G2  # noqa: E402
from src.gov_data import snapshot as SN  # noqa: E402
from src.gov_data import survival as G3  # noqa: E402
from src.gov_data.names import makes_of_name, norm_model, ym_of  # noqa: E402

BASE = "https://ckan.test/api/3/action"
PRICES, RECALLS = "39f455bf-6db0-4926-859d-017f34eacbcb", "2c33523f-87aa-44ec-a736-edbb0a82975e"
CANCELLED = ("851ecab1-0622-4dbe-a6c7-f950cf82abf9", "4e6b9724-4c1e-43f0-909a-154d4cc4e046",
             "ec8cbc34-72e1-4b69-9c48-22821ba0bd6c")
ACTIVE = "053cea08-09bc-40ec-8f7a-156f0677aff3"
PLATE, CHASSIS, ENGINE = "PLATE7777777", "CHASSISXYZ123", "ENGINE998877"


def _csv(header: list[str], rows: list[list]) -> bytes:
    out = io.StringIO()
    out.write(",".join(header) + "\r\n")
    for row in rows:
        out.write(",".join("" if v is None else str(v) for v in row) + "\r\n")
    return ("﻿" + out.getvalue()).encode("utf-8")


PRICE_HEADER = ["tozeret_cd", "degem_cd", "shnat_yitzur", "mehir", "kinuy_mishari", "semel_yevuan", "shem_yevuan",
                "sug_degem", "tozeret_nm", "degem_nm", "extra_column"]


def price_body(extra_rows: int = 0, broken: bool = False) -> bytes:
    rows = [[413, 100, 2022, 119900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"],
            [413, 100, 2022, 129900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"],
            [413, 100, 2022, 139900, "COROLLA", 7, "יבואן א", "P", "טויוטה יפן", "ZWE211L", "x"],
            [413, 101, 2022, 99900, "YARIS", 7, "יבואן א", "P", "טויוטה יפן", "KSP210", "x"],
            [413, 101, 2022, 99900, "YARIS", 7, "יבואן א", "P", "טויוטה יפן", "KSP210", "x"],    # duplicate: kept
            [413, 102, 2017, 150000, "C-HR", 7, "יבואן א", "P", "טויוטה יפן", "NGX10", "x"]]
    if broken:                                                                    # no code key: unkeyed
        rows.append(["", 5, 2020, 1, "BROKEN", "", "", "", "", "", ""])
    rows += [[413, 900 + n, 2021, 100000 + n, "FILL", 7, "יבואן א", "P", "טויוטה יפן", "F", "x"]
             for n in range(extra_rows)]
    return _csv(PRICE_HEADER, rows)


RECALL_HEADER = ["RECALL_ID", "TOZAR_CD", "TOZAR_TEUR", "DEGEM", "BUILD_BEGIN_A", "BUILD_END_A", "SUG_TAKALA",
                 "TEUR_TAKALA", "SHNAT_RECALL", "SUG_RECALL", "OFEN_TIKUN", "TKINA_EU", "YEVUAN_TEUR", "TELEPHONE",
                 "WEBSITE"]


def recall_body() -> bytes:
    return _csv(RECALL_HEADER, [
        ["R-1", 413, "טויוטה", "COROLLA", "2018-01-01", "2020-12-31", "בלמים", "תקלה בבלם", 2021, "רגיל",
         "החלפת רכיב", "כן", "יבואן א", "03-5551234", "https://example.test"],
        ["R-2", 413, "טויוטה", "C HR", "2016-01-01", "2018-12-31", "כריות אוויר", "תקלה בכרית", 2019, "רגיל",
         "החלפה", "כן", "יבואן א", "03-5551234", "https://example.test"],
        ["R-3", 999, "יצרן לא ידוע", "MODEL X1", "2015-01-01", "2016-12-31", "מנוע", "תקלה", 2017, "רגיל",
         "בדיקה", "לא", "יבואן ב", "", ""]])


SURV_COLUMNS = ["mispar_rechev", "tozeret_cd", "sug_degem", "tozeret_nm", "degem_cd", "degem_nm", "ramat_gimur",
                "shnat_yitzur", "misgeret", "mispar_manoa", "moed_aliya_lakvish", "kinuy_mishari", "sug_delek_nm"]


def active_body(n_big: int = 250) -> bytes:
    rows = []
    for i in range(n_big):
        rows.append([f"{PLATE}{i}", 413, "P", "טויוטה יפן", 100, "ZWE", "GLI", 2017, f"{CHASSIS}{i}", f"{ENGINE}{i}",
                     "2017-3", "COROLLA", "בנזין"])
    for i in range(50):
        rows.append([f"{PLATE}S{i}", 413, "P", "טויוטה יפן", 101, "KSP", "", 2017, f"{CHASSIS}S{i}",
                     f"{ENGINE}S{i}", "2017-6", "YARIS", "בנזין"])
    return _csv(SURV_COLUMNS, rows)


def cancelled_body(part: int) -> bytes:
    rows = []
    if part == 0:
        for i in range(40):        # 40 cancellations of model year 2017 in 2022: age 5 (part 1: 10 more in 2019, age 2;
                                   # moed_aliya_lakvish as the datastore states it: a year, or empty)
            rows.append([f"{PLATE}C{i}", 413, "P", "טויוטה יפן", 100, "ZWE", "", 2017, f"{CHASSIS}C{i}",
                         f"{ENGINE}C{i}", "2017", "COROLLA", "בנזין", "2022-05-10"])
    if part == 1:
        for i in range(10):
            rows.append([f"{PLATE}D{i}", 413, "P", "טויוטה יפן", 100, "ZWE", "", 2017, f"{CHASSIS}D{i}",
                         f"{ENGINE}D{i}", "", "COROLLA", "בנזין", "2019-09-01"])
    if part == 2:
        for i in range(5):         # the oldest file: 5 cancellations dated 1999 of another key (coverage_start 1999)
            rows.append([f"{PLATE}E{i}", 999, "P", "יצרן ישן", 1, "OLD", "", 1990, f"{CHASSIS}E{i}",
                         f"{ENGINE}E{i}", "", "OLDIE", "בנזין", "1999-03-01"])
    return _csv(SURV_COLUMNS + ["bitul_dt"], rows)


class FakeCkan:
    def __init__(self, bodies: dict[str, bytes], licences: dict[str, str] | None = None,
                 formats: dict[str, str] | None = None, missing: set[str] | None = None):
        self.bodies, self.licences, self.formats = bodies, licences or {}, formats or {}
        self.missing = missing or set()
        self.opened: list[str] = []
        self.calls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        action, params = parsed.path.rsplit("/", 1)[-1], dict(urllib.parse.parse_qsl(parsed.query))
        if action == "resource_show":
            rid = params["id"]
            if rid in self.missing:
                return {"success": False, "error": {"message": "Not found"}}
            return {"success": True, "result": {"url": f"https://files.test/{rid}/served-{rid[:4]}.csv",
                                                "format": self.formats.get(rid, "CSV"), "package_id": f"pkg-{rid}",
                                                "last_modified": "2026-10-01T05:00:00"}}
        if action == "package_show":
            rid = params["id"].removeprefix("pkg-")
            return {"success": True, "result": {"license_title": self.licences.get(rid, "Other (Open)"),
                                                "title": f"title {rid[:4]}",
                                                "organization": {"title": "משרד התחבורה"}}}
        if action == "datastore_search":
            return {"success": True, "result": {"records": [{"x": 1}]}}
        raise AssertionError(url)

    def open(self, url: str):
        self.opened.append(url)
        rid = url.split("/")[3]
        return io.BytesIO(self.bodies[rid])


def bodies(**changes) -> dict[str, bytes]:
    out = {PRICES: price_body(), RECALLS: recall_body(), ACTIVE: active_body(),
           CANCELLED[0]: cancelled_body(0), CANCELLED[1]: cancelled_body(1), CANCELLED[2]: cancelled_body(2)}
    out.update(changes)
    return out


@pytest.fixture
def gov(tmp_path):
    out, work = tmp_path / "gov", tmp_path / "work"
    yield out, work
    SN.set_gov_dir(None)


def build(out: Path, work: Path, http: FakeCkan, names=None, registry_index: Path | None = None) -> dict:
    def now() -> str:
        return "2026-10-03T04:41:00+00:00"
    return B.run(list(names or B.ORDER), out, work, http=http, now=now,
                 registry_index=registry_index or ROOT / "data" / "gov_registry_index.json")


def serve(out: Path, tmp_path: Path) -> None:
    SN.set_gov_dir(out, tmp_path / "decompressed")


CATALOGUE = {"tozar": "טויוטה", "tozeret_cd": 413, "degem_cd": 100, "shnat_yitzur": 2022, "kinuy_mishari": "COROLLA"}


def row(**changes) -> dict:
    return {**copy.deepcopy(CATALOGUE), **changes}


# --- G0: CKAN, download, fail-safes -----------------------------------------------------------------------------------

def test_the_file_url_comes_from_resource_show(gov):
    out, work = gov
    http = FakeCkan(bodies())
    outcome = build(out, work, http)
    assert [s["status"] for s in outcome["status"]] == ["built", "built", "built"]
    assert sorted(http.opened) == sorted(f"https://files.test/{rid}/served-{rid[:4]}.csv"
                                         for rid in (PRICES, RECALLS, ACTIVE, *CANCELLED))
    entry = outcome["manifest"]["datasets"]["new_car_prices"]
    res = entry["resources"][0]
    assert res["source_url"] == f"https://files.test/{PRICES}/served-{PRICES[:4]}.csv"
    for key in ("resource_id", "source_url", "downloaded_at", "source_last_modified", "license", "row_count",
                "file_size", "sha256", "schema_hash", "ingestion_version"):
        assert res.get(key) not in (None, ""), key
    assert res["license"] == "Other (Open)" and res["row_count"] == 6
    assert (out / "new_car_prices.sqlite.gz").is_file() and not list(out.glob("*.raw"))
    assert not list((work / "raw").glob("*")) if (work / "raw").exists() else True


def test_an_html_body_fails_the_dataset_and_keeps_the_previous_snapshot(gov):
    out, work = gov
    build(out, work, FakeCkan(bodies()))
    before = (out / "new_car_prices.sqlite.gz").read_bytes()
    entry_before = SN.manifest(out)["datasets"]["new_car_prices"]["sha256"]
    html = b"<!DOCTYPE html><html><body>Service unavailable</body></html>"
    outcome = build(out, work, FakeCkan(bodies(**{PRICES: html})), names=["new_car_prices", "recall_notices"])
    failed = next(s for s in outcome["status"] if s["dataset"] == "new_car_prices")
    assert failed == {"dataset": "new_car_prices", "status": "failed", "reason": "html_body",
                      "previous_snapshot_preserved": True,
                      "detail": "file: an HTML page instead of the data from files.test: '<!DOCTYPE html><html>"
                                "<body>Service unavailable</body></html>'; datastore: not used (datastore_active is "
                                "not true)",
                      "resource_id": PRICES,
                      "resources": [{"resource_id": PRICES, "role": "prices", "status": "failed",
                                     "access_method": "file_download", "file_http_status": None,
                                     "file_error": failed["resources"][0]["file_error"], "datastore_active": False,
                                     "rows": None, "reason": "html_body", "detail": failed["detail"]}]}
    assert failed["resources"][0]["file_error"].startswith("an HTML page instead of the data from files.test")
    assert (out / "new_car_prices.sqlite.gz").read_bytes() == before
    assert SN.manifest(out)["datasets"]["new_car_prices"]["sha256"] == entry_before
    assert SN.manifest(out)["datasets"]["new_car_prices"]["build_status"] == "failed"
    assert next(s for s in outcome["status"] if s["dataset"] == "recall_notices")["status"] == "built"
    assert json.loads((out / "build_status.json").read_text("utf-8"))[0]["previous_snapshot_preserved"] is True


def test_a_licence_text_change_stops_only_that_dataset(gov):
    out, work = gov
    build(out, work, FakeCkan(bodies()))
    before = (out / "recall_notices.sqlite.gz").read_bytes()
    http = FakeCkan(bodies(), licences={RECALLS: "Creative Commons Non-Commercial"})
    outcome = build(out, work, http)
    status = {s["dataset"]: s for s in outcome["status"]}
    assert status["recall_notices"]["status"] == "failed" and status["recall_notices"]["reason"] == "licence_changed"
    assert status["new_car_prices"]["status"] == "built" and status["road_survival"]["status"] == "built"
    assert (out / "recall_notices.sqlite.gz").read_bytes() == before


@pytest.mark.parametrize("change,reason", [
    ({"missing": {PRICES}}, "resource_unavailable"),
    ({"formats": {PRICES: "XLSX"}}, "format_changed"),
    ({"bodies": {PRICES: _csv(["tozeret_cd", "degem_cd", "shnat_yitzur", "kinuy_mishari"], [[1, 2, 2020, "A"]])}},
     "required_column_missing"),
    ({"bodies": {PRICES: b"just one line without any delimiter at all\n"}}, "format_changed"),
    ({"bodies": {PRICES: b'{"success": false, "error": "boom"}'}}, "html_body"),
])
def test_each_fail_safe_stops_the_dataset(gov, change, reason):
    out, work = gov
    http = FakeCkan(bodies(**change.get("bodies", {})), formats=change.get("formats"), missing=change.get("missing"))
    outcome = build(out, work, http, names=["new_car_prices"])
    assert outcome["status"][0]["status"] == "failed" and outcome["status"][0]["reason"] == reason
    assert not (out / "new_car_prices.sqlite.gz").exists()


def test_a_row_count_below_70_percent_of_the_previous_build_stops(gov):
    out, work = gov
    build(out, work, FakeCkan(bodies(**{PRICES: price_body(extra_rows=20)})), names=["new_car_prices"])
    outcome = build(out, work, FakeCkan(bodies()), names=["new_car_prices"])          # 7 of 27 rows
    assert outcome["status"][0]["reason"] == "row_count_drop"


def test_two_builds_of_the_same_data_give_the_same_snapshot_bytes(gov, tmp_path):
    out, work = gov
    build(out, work, FakeCkan(bodies()))
    first = {p.name: p.read_bytes() for p in out.glob("*.sqlite.gz")}
    other = tmp_path / "again"
    build(other, tmp_path / "work2", FakeCkan(bodies()))
    assert first == {p.name: p.read_bytes() for p in other.glob("*.sqlite.gz")}


# --- G1 ----------------------------------------------------------------------------------------------------------------

def test_one_key_with_three_prices_is_a_range_never_a_value(gov, tmp_path):
    out, work = gov
    build(out, work, FakeCkan(bodies()), names=["new_car_prices"])
    serve(out, tmp_path)
    facts, _ = F.price_facts(row())
    price = facts["original_new_price_ils"]
    assert price["range"] == [119900, 139900] and price["n_prices"] == 3 and "value" not in price
    assert price["source"] == "gov_new_car_prices" and price["source_level"] == "government_dataset"
    assert price["label_he"] == "מחיר מחירון חדש מקורי" and price["resource_id"] == PRICES
    assert price["licence"] == "Other (Open)" and price["attribution"]
    assert facts["original_importer"]["value"] == "יבואן א"


def test_a_single_price_is_a_value_and_duplicates_are_counted(gov, tmp_path):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()), names=["new_car_prices"])
    serve(out, tmp_path)
    price = F.price_facts(row(degem_cd=101, kinuy_mishari="YARIS"))[0]["original_new_price_ils"]
    assert price["value"] == 99900 and "range" not in price
    path = SN.materialize("new_car_prices")
    assert SN.query(path, "SELECT rows FROM prices WHERE degem_cd = 101") == [{"rows": 2}]
    stats = outcome["manifest"]["datasets"]["new_car_prices"]["stats"]
    assert stats["distinct_price_buckets"] == {"1": 2, "2-3": 1, ">3": 0} and stats["unkeyed_rows"] == 0
    assert stats["multi_price_examples"][0]["kinuy_mishari"] == ["COROLLA"]


def test_the_price_report_joins_the_registry_index(gov, tmp_path):
    out, work = gov
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"model_years": {"413|100|2022": {}, "1|1|2000": {}}}), "utf-8")
    outcome = build(out, work, FakeCkan(bodies()), names=["new_car_prices"], registry_index=index)
    join = outcome["manifest"]["datasets"]["new_car_prices"]["stats"]["registry_join"]
    assert join == {"matched": 1, "unmatched": 2, "pct": 33.3}


# --- G2 ----------------------------------------------------------------------------------------------------------------

def test_recall_matching_unresolved_resolved_and_zero(gov, tmp_path):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()))
    serve(out, tmp_path)
    model_map = json.loads((out / "recall_model_map.json").read_text("utf-8"))
    assert {(e["make"], e["degem"]) for e in model_map["entries"]} == {("TOYOTA", "COROLLA"), ("TOYOTA", "C HR")}
    assert [u["reason"] for u in model_map["unresolved"]] == ["make_unresolved"]
    # a resolved model with a notice in its production range
    facts, _ = F.recall_facts(row(shnat_yitzur=2019))
    assert facts["recall_count"]["value"] == 1
    item = facts["recalls"]["value"][0]
    assert item == {"recall_id": "R-1", "recall_year": 2021, "affected_system": "בלמים",
                    "fault_description": "תקלה בבלם", "repair_method": "החלפת רכיב",
                    "production_range": {"from": "2018-01-01", "to": "2020-12-31"}}
    assert facts["recalls"]["licence"] == "Other (Open)" and facts["recalls"]["resource_id"] == RECALLS
    # resolved, but no notice in 2022: 0 recalls, an empty list
    facts, _ = F.recall_facts(row(shnat_yitzur=2022))
    assert facts["recall_count"]["value"] == 0 and facts["recalls"]["value"] == []
    # an unresolved model: no recalls field at all, never []
    facts, withheld = F.recall_facts(row(kinuy_mishari="YARIS", degem_cd=101))
    assert facts == {} and withheld[0]["reason"] == "recall_model_unresolved"
    assert "TELEPHONE" not in json.dumps(outcome["manifest"]["datasets"]["recall_notices"]["stats"])
    raw = gzip.decompress((out / "recall_notices.sqlite.gz").read_bytes())
    assert b"03-5551234" not in raw and b"example.test" not in raw


def test_a_matched_notice_without_a_production_range_unresolves_the_variant(gov, tmp_path):
    out, work = gov
    body = _csv(RECALL_HEADER, [["R-9", 413, "טויוטה", "COROLLA", "", "2020", "בלמים", "x", 2021, "", "", "", "",
                                 "", ""]])
    build(out, work, FakeCkan(bodies(**{RECALLS: body})))
    serve(out, tmp_path)
    facts, withheld = F.recall_facts(row(shnat_yitzur=2019))
    assert facts == {} and withheld[0]["reason"] == "recall_range_unresolved"


def test_the_code_key_needs_98_percent_agreement():
    agg = G2.Recalls()
    names: dict[int, Counter] = {}
    for code in range(10):
        agg.add({"RECALL_ID": f"R{code}", "TOZAR_CD": str(code), "TOZAR_TEUR": "טויוטה", "DEGEM": "X"})
        names[code] = Counter({"טויוטה יפן" if code < 9 else "פורד ארה\"ב": 1})
    agreement = G2.code_agreement(agg, names)
    assert agreement["codes"] == 10 and agreement["agreeing"] == 9 and agreement["rate"] == 0.9
    assert not (agreement["rate"] >= 0.98)


def test_the_build_records_the_agreement_and_does_not_use_a_disagreeing_code(gov):
    out, work = gov
    body = recall_body().replace(b",413,", b",414,", 1)        # R-1's code is not the registry's 413
    outcome = build(out, work, FakeCkan(bodies(**{RECALLS: body})))
    stats = outcome["manifest"]["datasets"]["recall_notices"]["stats"]
    assert stats["tozar_cd_agreement"]["rate"] < 0.98 and stats["tozar_cd_used_as_key"] is False


def test_make_resolution_is_exact():
    assert makes_of_name("טויוטה יפן") == ["TOYOTA"] and makes_of_name("טויוטה") == ["TOYOTA"]
    assert makes_of_name("TOYOTA MOTOR CORPORATION") == ["TOYOTA"]
    assert makes_of_name("טויוטהה") == [] and makes_of_name("TOYOTTA") == []
    assert norm_model("C-HR") == "C HR" and norm_model("CHR") != norm_model("C-HR")


# --- G3 ----------------------------------------------------------------------------------------------------------------

def test_no_plate_chassis_or_engine_number_in_any_output_or_log(gov, tmp_path, caplog, capsys):
    out, work = gov
    caplog.set_level(logging.DEBUG)
    outcome = build(out, work, FakeCkan(bodies()))
    text = B.report(outcome)
    (out / "report.md").write_text(text, "utf-8")
    print(text)
    blobs = []
    for path in list(out.rglob("*")) + (list(work.rglob("*")) if work.exists() else []):
        if path.is_file():
            data = path.read_bytes()
            blobs.append(gzip.decompress(data) if path.suffix == ".gz" else data)
    captured = capsys.readouterr()
    blobs += [caplog.text.encode("utf-8"), captured.out.encode("utf-8"), captured.err.encode("utf-8")]
    for blob in blobs:
        for token in (PLATE, CHASSIS, ENGINE):
            assert token.encode("utf-8") not in blob


def test_survival_cohorts_small_cohorts_and_ages_not_reached(gov, tmp_path):
    out, work = gov
    outcome = build(out, work, FakeCkan(bodies()), names=["road_survival"])
    stats = outcome["manifest"]["datasets"]["road_survival"]["stats"]
    assert stats["cohort_basis"] == "shnat_yitzur" and stats["shnat_yitzur_coverage"] == 1.0
    assert stats["reference_year"] == 2026 and "Inactive vehicles" in stats["exclusion"]
    serve(out, tmp_path)
    facts, _ = F.survival_facts(row(shnat_yitzur=2017))
    value = facts["road_survival"]["value"]
    assert value["cohort_size"] == 300 and value["cohort_year"] == 2017                 # 250 active + 50 cancelled
    shares = value["cancelled_share_by_age"]
    assert sorted(int(a) for a in shares) == list(range(3, 9))       # 2026 - 2017 = 9 >= a + 1: no 9..20
    assert "12" not in shares
    # whole years: 10 cancelled in 2019 (age 2), 40 in 2022 (age 5)
    assert shares["3"] == shares["4"] == round(10 / 300, 4) and shares["5"] == shares["8"] == round(50 / 300, 4)
    assert value["median_age_at_final_cancellation"] == 5
    assert value["final_cancellation_rate"] == round(50 / 300, 4)
    assert "סיבת הביטול אינה ידועה" in value["definition_he"]
    assert facts["road_survival"]["source_level"] == "government_dataset"
    assert facts["road_survival"]["resource_id"] == [*CANCELLED, ACTIVE]
    assert "reliability" not in json.dumps(facts).lower()
    # the small cohort (50 vehicles): no survival fields
    facts, withheld = F.survival_facts(row(degem_cd=101, shnat_yitzur=2017))
    assert facts == {} and withheld[0]["reason"] == "small_cohort"
    path = SN.materialize("road_survival")
    small = SN.query(path, "SELECT * FROM cohorts WHERE degem_cd = 101")[0]
    assert small["cohort_size"] == 50 and small["shares"] is None and small["age_median"] is None


def test_share_by_age_is_absent_for_an_age_the_cohort_has_not_reached():
    agg = G3.Survival()
    for i in range(300):
        agg.add({"_role": "active", "tozeret_cd": "1", "degem_cd": "2", "shnat_yitzur": "2018"})
    rows, _ = G3.cohorts(agg, 2026, 200, (3, 20))                     # 2026 - 2018 = 8 >= a + 1: up to age 7
    assert "7" in rows[0]["shares"] and "8" not in rows[0]["shares"] and "12" not in rows[0]["shares"]


def test_dates_parse_exactly():
    assert ym_of("2017-3") == (2017, 3) and ym_of("2022-05-10") == (2022, 5) and ym_of("10/05/2022") == (2022, 5)
    assert ym_of("201703") == (2017, 3) and ym_of("garbage") is None and ym_of("2017-13") is None


# --- facts API ---------------------------------------------------------------------------------------------------------

def test_the_record_carries_the_gov_facts_and_versions(gov, tmp_path):
    from src.facts import versions as V
    from src.facts.record import build_record
    from src.facts.service import FactsService

    out, work = gov
    build(out, work, FakeCkan(bodies()))
    serve(out, tmp_path)
    part = F.gov_part(row(shnat_yitzur=2017, degem_cd=102, kinuy_mishari="C-HR"))
    assert set(part["facts"]) == {"original_new_price_ils", "original_importer", "recalls", "recall_count"}
    assert part["facts"]["recall_count"]["value"] == 1
    record = build_record("k", {"tozar": "טויוטה"}, {"facts": {}, "gov_facts": part["facts"],
                                                       "gov_withheld": part["withheld"]}, government=({}, []))
    assert record["facts"]["original_new_price_ils"]["value"] == 150000
    versions = V.versions()
    assert versions["gov_datasets"] == SN.file_sha(out / "manifest.json")
    assert versions["gov_recall_model_map"] == SN.file_sha(out / "recall_model_map.json")
    key_before = FactsService.cache_key("k", {"content_sha256": "x"})
    manifest = out / "manifest.json"
    manifest.write_text(manifest.read_text("utf-8").replace('"version"', '"version" ', 1), "utf-8")
    import os
    os.utime(manifest, ns=(manifest.stat().st_mtime_ns + 10**9,) * 2)
    assert FactsService.cache_key("k", {"content_sha256": "x"}) != key_before


def test_without_gov_snapshots_no_gov_field_appears(tmp_path):
    SN.set_gov_dir(tmp_path / "empty")
    try:
        part = F.gov_part(row())
    finally:
        SN.set_gov_dir(None)
    assert part["facts"] == {}
    assert {w["reason"] for w in part["withheld"]} == {"gov_snapshot_missing"}


def test_the_contract_allows_a_range_only_for_the_price():
    contract = json.loads((ROOT / "data/facts_contract.json").read_text("utf-8"))
    assert contract["version"] == "vehicle-facts/1.2"
    facts = contract["$defs"]["record"]["oneOf"][0]["properties"]["facts"]["properties"]
    assert set(facts) == {"original_new_price_ils", "original_importer", "recalls", "recall_count", "road_survival"}
    assert "range" in contract["$defs"]["gov_price_fact"]["properties"]
    assert "range" not in contract["$defs"]["gov_fact"]["properties"]
    assert "gov_datasets" in contract["$defs"]["versions"]["required"]


def test_the_contract_validates_built_gov_facts(gov, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    out, work = gov
    build(out, work, FakeCkan(bodies()))
    serve(out, tmp_path)
    contract = json.loads((ROOT / "data/facts_contract.json").read_text("utf-8"))
    facts = {}
    for r in (row(), row(shnat_yitzur=2019), row(shnat_yitzur=2017)):
        facts.update(F.gov_part(r)["facts"])
    assert set(facts) == {"original_new_price_ils", "original_importer", "recalls", "recall_count", "road_survival"}
    schema = {**contract, "$ref": "#/$defs/record"}
    schema.pop("required"), schema.pop("properties")
    record = {"variant_identity_key": "k", "status": "ok", "identity": {}, "facts": facts, "open_data_match": {},
              "versions": {"contract": "vehicle-facts/1", "admission": "a", "zero_semantics": "z", "snapshots_sha": "s",
                           "matcher": "m", "gov_datasets": "g"}}
    jsonschema.validate(record, schema)
    bad = copy.deepcopy(record)
    bad["facts"]["original_importer"]["range"] = [1, 2]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)

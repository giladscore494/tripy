"""The facts coverage probe (MCP facts_coverage) and the CO2-selection diagnostics of the debug / MCP withheld detail.
No network: a fake read-only query stands in for MILO and the snapshot rows are the committed fixture rows
(tests/fixtures/facts_rows.json). The public record is pinned against tests/fixtures/facts_public_golden.json, generated
on main before the diagnostics existed (byte-identical, sorted keys; `versions` compared with the live versions)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.facts import coverage as C
from src.facts import versions as V
from src.facts.service import FactsService, dumps
from test_facts_api import KEY, ROWS, FakeMilo, snapshot_rows

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests/fixtures/facts_public_golden.json").read_text("utf-8"))
TEMPLATE = "eea-2020-F-47216"          # one of 22010's type_code+co2 rows (JP91)
EEA = "eea_co2_cars"


def co2_rows(values):
    """22010's EEA rows replaced by copies of one JP91 row with (co2_wltp, mass, consumption) each."""
    def edit(rows):
        base = next(r for r in rows[EEA] if r["row_id"] == TEMPLATE)
        rows[EEA] = [{**copy.deepcopy(base), "row_id": f"eea-2020-F-9000{n}", "co2_wltp": co2,
                      "mass_running_order_kg": mass, "mass_running_order_kg_min": mass,
                      "mass_running_order_kg_max": mass, "fuel_consumption_l_100km": cons}
                     for n, (co2, mass, cons) in enumerate(values)]
        return rows
    return edit


CASES = {"co2_133_selects_2": [(127, 1500, 4.8), (133, 1395, 5.8), (133, 1395, 5.8)],
         "co2_no_equal": [(127, 1365, 4.8), (128, 1395, 5.8), (129, 1395, 5.9)]}


def co2_service(values) -> FactsService:
    row = {**ROWS["22010"], "co2_wltp": 133}
    return FactsService(FakeMilo([row]), rows_by_source=lambda r: snapshot_rows(r, co2_rows(values)))


def public(svc: FactsService, key: str) -> str:
    record = svc.records([key])[0][0]
    assert record.pop("versions") == V.versions()
    return dumps(record)


# --- P2: co2_selection --------------------------------------------------------------------------------------------------

def test_the_co2_step_selects_the_two_equal_rows():
    record = co2_service(CASES["co2_133_selects_2"]).records([KEY["22010"]], debug=True)[0][0]
    selection = record["co2_selection"][EEA]
    assert selection["gov_co2_wltp"] == 133 and selection["survivors"] == 3 and selection["selected"] == 2
    assert selection["survivor_co2_values"] == [127, 133]
    assert (selection["rule"], selection["tolerance"], selection["fallback"]) == ("equal", None, None)
    assert selection["selected_values"]["curb_weight_kg"] == [1395]
    assert selection["selected_values"]["fuel_consumption_combined_l_100km"] == [5.8]
    assert record["facts"]["curb_weight_kg"]["value"] == 1395            # the two 133 rows agree: admitted


def test_a_disagreement_after_the_co2_step_carries_the_selection():
    values = [(127, 1500, 4.8), (133, 1395, 5.8), (133, 1395, 6.0)]        # 0.2 l apart: wider than the 0.1 precision
    record = co2_service(values).records([KEY["22010"]], debug=True)[0][0]
    held = next(w for w in record["withheld"] if w["source"] == EEA and w["field"] == "fuel_consumption_combined_l_100km")
    assert held["reason"] == "offer_survivors_disagree" and held["values"] == ["5.8", "6"]
    assert held["co2_selection"]["selected"] == 2 and held["co2_selection"]["survivors"] == 3
    assert held["co2_selection"]["selected_values"]["fuel_consumption_combined_l_100km"] == [5.8, 6]


def test_no_equal_co2_is_the_fallback_and_nothing_is_selected_out():
    record = co2_service(CASES["co2_no_equal"]).records([KEY["22010"]], debug=True)[0][0]
    held = {w["field"]: w for w in record["withheld"] if w["source"] == EEA and w["reason"] == "offer_survivors_disagree"}
    assert set(held) == {"fuel_consumption_combined_l_100km"}
    # E2: the masses {1365, 1395} agree within precision (30 kg, 2.2 %), but no CO2 selected them: requirement_unmet
    mass = next(w for w in record["withheld"] if w["source"] == EEA and w["field"] == "curb_weight_kg")
    assert (mass["reason"], mass["detail"], mass["value"]) == ("requirement_unmet", ["co2_selected"], 1395)
    assert mass["co2_selection"]["fallback"] == "no_equal_co2"
    selection = held["fuel_consumption_combined_l_100km"]["co2_selection"]
    assert (selection["fallback"], selection["rule"]) == ("no_equal_co2", "none")
    assert selection["survivors"] == selection["selected"] == 3
    assert selection["survivor_co2_values"] == [127, 128, 129]
    assert selection["selected_values"]["curb_weight_kg"] == [1365, 1395]
    assert selection["selected_values"]["fuel_consumption_combined_l_100km"] == [4.8, 5.8, 5.9]


def test_the_public_record_is_byte_identical_to_main():
    from test_facts_api import service

    for record_id in ("19754", "85167"):
        assert public(service(), KEY[record_id]) == GOLDEN[record_id], record_id
    # 22010: every fact of main byte-identical (exact agreement carries no `agreement`); the one addition is the energy
    # consumption its row MIN / MAX (17.7 / 17.8 kWh) now admits within the 0.2 precision (decision 2 of this PR)
    record = json.loads(public(service(), KEY["22010"]))
    added = record["facts"].pop("energy_consumption_kwh_100km")
    assert dumps(record) == GOLDEN["22010"]
    assert added["value"] == 17.7 and "agreement" not in added
    for name, values in CASES.items():
        svc = co2_service(values)
        assert public(svc, KEY["22010"]) == GOLDEN[name], name
        assert "co2_selection" not in svc.records([KEY["22010"]])[0][0]
        assert "co2_selection" in svc.records([KEY["22010"]], debug=True)[0][0]


def test_the_nedc_year_and_a_missing_government_co2_are_fallbacks():
    from test_facts_api import service

    nedc = service().records([KEY["19754"]], debug=True)[0][0]
    assert nedc["co2_selection"][EEA]["fallback"] == "nedc_year"
    row = {**ROWS["22010"], "co2_wltp": None}
    svc = FactsService(FakeMilo([row]), rows_by_source=lambda r: snapshot_rows(r))
    assert svc.records([KEY["22010"]], debug=True)[0][0]["co2_selection"][EEA]["fallback"] == "gov_co2_missing"


# --- P1: facts_coverage ----------------------------------------------------------------------------------------------------

def _key(n: int) -> str:
    return hashlib.sha256(f"coverage-{n}".encode()).hexdigest()


# six keys over two model years: 2020 four European (BMW 22010), 2021 two American (Cadillac 85167)
CATALOGUE = [{**copy.deepcopy(ROWS["22010" if n < 4 else "85167"]), "variant_identity_key": _key(n)} for n in range(6)]


class SampleMilo(FakeMilo):
    """db.LEVEL15_SAMPLE_SQL over the fixture catalogue: one model year, the private segment, an optional
    manufacturer, the first n keys by md5(variant_identity_key)."""

    def __call__(self, sql: str, params: dict) -> list[dict]:
        if "variant_identity_key = ANY" in sql:
            return super().__call__(sql, params)
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("WITH sample AS") and "ORDER BY md5(v.variant_identity_key)" in sql
        assert "vehicle_segment = 'private'" in sql and set(params) == {"year", "n", "manufacturer"}
        rows = [r for r in self.rows.values() if r["shnat_yitzur"] == params["year"]
                and r.get("vehicle_segment", "private") == "private"
                and (params["manufacturer"] is None or r["tozar"] == params["manufacturer"])]
        rows.sort(key=lambda r: hashlib.md5(r["variant_identity_key"].encode()).hexdigest())
        return copy.deepcopy(rows[:params["n"]])


def coverage_service() -> FactsService:
    return FactsService(SampleMilo(CATALOGUE), rows_by_source=lambda row: snapshot_rows(row))


def test_coverage_counts_per_year():
    svc = coverage_service()
    out = C.facts_coverage(svc, 2020, 2021, per_year=120)
    assert not out["truncated"] and [y["model_year"] for y in out["years"]] == [2020, 2021]
    y2020, y2021 = out["years"]
    assert (y2020["n"], y2021["n"]) == (4, 2)
    assert y2020["route"] == {"european": 4, "american": 0, "unknown": 0}
    assert y2021["route"] == {"european": 0, "american": 2, "unknown": 0}
    assert y2020["open_data_match_level"] == {"exact_technical_variant": 4}
    assert (y2020["with_open_data_fact"], y2020["with_eea_fact"], y2020["with_eea_fact_pct"]) == (4, 4, 100.0)
    assert y2021["with_eea_fact"] == 0 and y2021["with_open_data_fact"] == 2
    fields = y2020["fields"]
    assert fields["wheelbase_mm"]["returned"] == 4 and fields["curb_weight_kg"]["returned_pct"] == 100.0
    assert fields["energy_consumption_kwh_100km"]["returned"] == 4          # the 17.7 / 17.8 row (E2 row MIN / MAX)
    assert y2020["ms_per_key"]["n"] == 4 and y2020["ms_per_key"]["p95"] >= y2020["ms_per_key"]["p50"] >= 0
    assert fields["length_mm"]["withheld"] == {"never_admitted": 4}
    assert set(fields) == set(C.admitted_fields())
    co2 = y2020["co2_selection"]
    assert co2["decisions"] == 4 and co2["by_fallback"] == {"None": 4} and co2["noop"] == 0
    assert co2["withheld_disagree"] == 0
    assert out["manufacturers"] == [{"manufacturer": "ב מ וו", "n": 4, "with_eea_fact": 4, "with_eea_fact_pct": 100.0},
                                    {"manufacturer": "קאדילאק", "n": 2, "with_eea_fact": 0, "with_eea_fact_pct": 0.0}]
    samples = [(sql, p) for sql, p in svc._query.queries if "WITH sample" in sql]
    assert [p["year"] for _, p in samples] == [2020, 2021]           # one read-only query per model year


def test_the_sample_is_deterministic_and_bounded():
    svc = coverage_service()
    first = C.facts_coverage(svc, 2020, 2021, per_year=2)
    assert [y["n"] for y in first["years"]] == [2, 2]
    again = C.facts_coverage(svc, 2020, 2021, per_year=2)

    def stable(out):                                # the times vary: elapsed_s and ms_per_key left out
        return dumps({**{k: v for k, v in out.items() if k != "elapsed_s"},
                      "years": [{k: v for k, v in y.items() if k != "ms_per_key"} for y in out["years"]]})

    assert stable(first) == stable(again)
    keys = [r["variant_identity_key"] for r in svc.sample_rows(2020, 2)]
    expected = sorted((r["variant_identity_key"] for r in CATALOGUE if r["shnat_yitzur"] == 2020),
                      key=lambda k: hashlib.md5(k.encode()).hexdigest())[:2]
    assert keys == expected == [r["variant_identity_key"] for r in svc.sample_rows(2020, 2)]
    only = C.facts_coverage(svc, 2020, 2021, per_year=120, manufacturer="קאדילאק")
    assert [y["n"] for y in only["years"]] == [0, 2] and only["manufacturer"] == "קאדילאק"


def test_per_year_above_300_and_bad_years_are_rejected():
    svc = coverage_service()
    for args in ((2020, 2021, 301), (2020, 2021, 0), (2021, 2020, 10), ("x", 2021, 10), (2000, 2030, 10)):
        with pytest.raises(C.CoverageInputError):
            C.facts_coverage(svc, *args)
    assert C.facts_coverage(svc, 2020, 2020, per_year=300)["per_year"] == 300


def test_the_budget_truncates():
    svc = coverage_service()
    ticks = iter(range(1000))
    out = C.facts_coverage(svc, 2020, 2021, per_year=120, budget_s=3, clock=lambda: next(ticks))
    assert out["truncated"] is True and out["years"][0]["truncated"] is True and len(out["years"]) == 1
    assert out["years"][0]["n"] < 4


def test_mcp_facts_coverage_is_read_only_and_rejects_bad_arguments(tmp_path):
    from src.mcp_server.safety import ToolInputError
    from src.mcp_server.server import TOOL_NAMES
    from src.mcp_server.tools import Observer
    from src.storage.paths import resolve_paths

    observer = Observer(resolve_paths({"TRIPY_DATA_DIR": str(tmp_path)}.get), facts=coverage_service())
    out = observer.facts_coverage(2020, 2021, 120)
    assert [y["n"] for y in out["years"]] == [4, 2]
    with pytest.raises(ToolInputError):
        observer.facts_coverage(2020, 2021, 301)
    assert "facts_coverage" in TOOL_NAMES
    unavailable = Observer(resolve_paths({"TRIPY_DATA_DIR": str(tmp_path)}.get), facts=FactsService(None))
    assert unavailable.facts_coverage(2020, 2021)["error"] == "catalog_unavailable"
    assert not list(tmp_path.rglob("*.json"))                              # the MCP writes nothing


def test_the_coverage_log_line_has_counts_and_no_values(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="tripy.facts"):
        C.facts_coverage(coverage_service(), 2020, 2021, per_year=120, manufacturer="ב מ וו")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("facts coverage"))
    assert '"n":{"2020":4,"2021":0}' in line and "ב מ וו" not in line and "1935" not in line

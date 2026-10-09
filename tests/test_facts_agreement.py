"""E1-E4 of the EEA facts: the CO2 / consumption consistency filter, the within-precision survivor agreement (and the
row MIN / MAX rule), the type-code fallback for a missing government CO2 and the shard candidate index. No network:
22010's committed fixture rows (tests/fixtures/facts_rows.json) are rewritten per case, and the index test builds its
own shards."""

from __future__ import annotations

import copy
import json
import random

import pytest

from src.facts.service import FactsService, dumps
from src.open_data import datasets as ds
from src.open_data import match as M
from src.open_data import offers as O
from src.open_data import shard_index as SI
from test_facts_api import KEY, ROWS, FakeMilo, snapshot_rows

EEA = "eea_co2_cars"
TEMPLATE = "eea-2020-F-47216"          # one of 22010's type_code + co2 rows (BMW 530e, JP91)
CONSUMPTION = "fuel_consumption_combined_l_100km"


def eea_rows(values, **common):
    """22010's EEA rows replaced by copies of one JP91 row: each value a dict of the columns to set (petrol, mono-fuel
    by default)."""
    def edit(rows):
        base = next(r for r in rows[EEA] if r["row_id"] == TEMPLATE)
        out = []
        for n, item in enumerate(values):
            row = {**copy.deepcopy(base), "row_id": f"eea-2020-F-9100{n}", "fuel": "PETROL", "fuel_mode": "M",
                   **common, **item}
            for column in ("mass_running_order_kg", "fuel_consumption_l_100km"):
                if column in item:                        # MIN = MAX = the value unless the case sets them
                    for end in ("_min", "_max"):
                        row[column + end] = item.get(column + end, item[column])
            out.append(row)
        rows[EEA] = out
        return rows
    return edit


def record(values, gov=None, **common):
    row = {**ROWS["22010"], "co2_wltp": 133, **(gov or {})}
    svc = FactsService(FakeMilo([row]), rows_by_source=lambda r: snapshot_rows(r, eea_rows(values, **common)))
    return svc.records([KEY["22010"]], debug=True)[0][0]


def held(rec, field, source=EEA):
    return [w for w in rec["withheld"] if w["source"] == source and w["field"] == field]


def row(co2, litres, mass=1395, **extra):
    return {"co2_wltp": co2, "fuel_consumption_l_100km": litres, "mass_running_order_kg": mass, **extra}


# --- E1: the physical-consistency filter ---------------------------------------------------------------------------

def test_e1_the_impossible_petrol_consumption_is_dropped_and_the_rest_agree():
    rec = record([row(133, 4.8), row(133, 5.8), row(133, 5.9)])
    fact = rec["facts"][CONSUMPTION]
    # 133 g/km / 4.8 l = 27.7: outside the petrol band (21.5-24.5); 5.8 (22.9) and 5.9 (22.5) agree within 0.1
    assert fact["value"] == 5.8                                         # the lower median of {5.8, 5.9}
    assert fact["agreement"] == {"rule": "within_precision", "spread": 0.1, "n_values": 2}
    assert fact["row_ids"] == ["eea-2020-F-91001", "eea-2020-F-91002"] and fact["basis"] == "type_code_match+co2"
    drops = [w for w in held(rec, CONSUMPTION) if w["reason"] == "inconsistent_co2_consumption"]
    assert drops == [{"source": EEA, "field": CONSUMPTION, "reason": "inconsistent_co2_consumption",
                      "row_id": "eea-2020-F-91000", "value": 4.8, "co2": 133.0}]
    assert rec["withheld_counts"]["inconsistent_co2_consumption"] == 1
    assert rec["facts"]["curb_weight_kg"]["value"] == 1395                 # only the consumption value was dropped
    public = FactsService(FakeMilo([{**ROWS["22010"], "co2_wltp": 133}]),
                          rows_by_source=lambda r: snapshot_rows(r, eea_rows([row(133, 4.8), row(133, 5.8)]))
                          ).records([KEY["22010"]])[0][0]
    assert "withheld" not in public and public["facts"][CONSUMPTION]["value"] == 5.8   # debug only


def test_e1_bands_per_fuel_and_no_filter_for_hybrids_or_other_fuels():
    diesel = record([row(133, 4.8, fuel="DIESEL"), row(133, 4.9, fuel="DIESEL")])    # 27.7 / 27.1: inside 24.5-28.0
    assert diesel["facts"][CONSUMPTION]["value"] == 4.8 and diesel["facts"][CONSUMPTION]["agreement"]["spread"] == 0.1
    hybrid = record([row(133, 4.8, fuel_mode="H"), row(133, 5.8, fuel_mode="H")])       # no filter: they disagree
    assert [w["reason"] for w in held(hybrid, CONSUMPTION)] == ["offer_survivors_disagree"]
    e85 = record([row(133, 4.8, fuel="E85"), row(133, 4.8, fuel="E85")])               # no band for E85
    assert e85["facts"][CONSUMPTION]["value"] == 4.8
    lpg = record([row(133, 4.8, fuel="LPG", fuel_mode="B")])                           # 27.7: outside 15-18
    assert [w["reason"] for w in held(lpg, CONSUMPTION)] == ["inconsistent_co2_consumption",
                                                             "offer_inconsistent_values"]


def test_e1_the_band_compares_the_rows_own_co2():
    band = O.consistency_band({"fuel": "PETROL", "fuel_mode": "M", "co2_wltp": 140, "co2_nedc": 120},
                              {"field": CONSUMPTION, "source": EEA}, ds.config()["consistency_bands"])
    assert band == (21.5, 24.5, 140.0)
    nedc = O.consistency_band({"fuel": "PETROL", "fuel_mode": "M", "co2_nedc": 120, "year": 2015},
                              {"field": CONSUMPTION, "source": EEA}, ds.config()["consistency_bands"])
    assert nedc == (21.5, 24.5, 120.0)                                   # an NEDC row: its NEDC CO2
    assert O.consistency_band({"fuel": "PETROL", "fuel_mode": "M"}, {"field": CONSUMPTION, "source": EEA},
                              ds.config()["consistency_bands"]) is None   # no CO2: kept


# --- the row MIN / MAX rule (decision 2) ---------------------------------------------------------------------------

def test_a_row_within_precision_counts_and_a_wider_row_loses_only_its_value():
    values = [row(133, 5.8, fuel_consumption_l_100km_min=5.8, fuel_consumption_l_100km_max=5.9)] + \
        [row(133, 5.8) for _ in range(3)] + \
        [row(133, 5.8, fuel_consumption_l_100km_min=5.8, fuel_consumption_l_100km_max=7.3)]
    rec = record(values)
    fact = rec["facts"][CONSUMPTION]
    assert fact["value"] == 5.8 and "agreement" not in fact                 # exact after the drop
    assert "eea-2020-F-91004" not in fact["row_ids"]
    dropped = [w for w in held(rec, CONSUMPTION) if w["reason"] == "row_range_dropped"]
    assert dropped == [{"source": EEA, "field": CONSUMPTION, "reason": "row_range_dropped",
                        "row_id": "eea-2020-F-91004", "value": 5.8, "range": [5.8, 7.3]}]
    out_of_band = record([row(133, 5.8, fuel_consumption_l_100km_min=5.4, fuel_consumption_l_100km_max=5.5)] +
                         [row(133, 5.8) for _ in range(3)])                 # MIN 5.4: 24.6 g per l, outside the band
    assert [w["row_id"] for w in held(out_of_band, CONSUMPTION) if w["reason"] == "row_range_dropped"] == \
        ["eea-2020-F-91000"]


def test_a_mostly_ranged_set_is_withheld_as_offer_range():
    wide = {"fuel_consumption_l_100km_min": 5.8, "fuel_consumption_l_100km_max": 6.4}
    rec = record([row(133, 5.8, **wide), row(133, 5.8, **wide), row(133, 5.8), row(133, 5.8)])   # 2 of 4 > 25 %
    assert CONSUMPTION not in rec["facts"]
    reasons = [w["reason"] for w in held(rec, CONSUMPTION)]
    assert reasons.count("row_range_dropped") == 2 and "offer_range" in reasons
    one = record([row(133, 5.8, **wide)] + [row(133, 5.8) for _ in range(3)])                     # 1 of 4 = 25 %
    assert one["facts"][CONSUMPTION]["value"] == 5.8


def test_mass_keeps_min_equal_max():
    rec = record([row(133, 5.8, 1395, mass_running_order_kg_min=1390, mass_running_order_kg_max=1400),
                  row(133, 5.8, 1395)])
    assert "curb_weight_kg" not in rec["facts"]
    assert [w["reason"] for w in held(rec, "curb_weight_kg")] == ["offer_range"]


# --- E2: the within-precision agreement --------------------------------------------------------------------------------

def test_e2_masses_within_precision_are_returned_with_agreement():
    rec = record([row(133, 5.8, 1365), row(133, 5.8, 1395)])
    fact = rec["facts"]["curb_weight_kg"]
    # spread 30 kg <= 40 and 2.2 % <= 2.5 % of the median: the lower median 1365, rounded to 5 kg
    assert fact["value"] == 1365 and fact["agreement"] == {"rule": "within_precision", "spread": 30, "n_values": 2}


def test_e2_a_wider_mass_spread_stays_withheld():
    rec = record([row(133, 5.8, 1425), row(133, 5.8, 1480), row(133, 5.8, 1548)])
    assert "curb_weight_kg" not in rec["facts"]
    item = held(rec, "curb_weight_kg")[0]
    assert item["reason"] == "offer_survivors_disagree" and item["values"] == ["1425", "1480", "1548"]
    pct = record([row(133, 5.8, 900), row(133, 5.8, 930)])            # 30 kg, but 3.3 % of 900: wider than 2.5 %
    assert held(pct, "curb_weight_kg")[0]["reason"] == "offer_survivors_disagree"


def test_e2_rules_and_rounding():
    rule = {"spread": 40, "spread_pct": 2.5, "round": 5}
    assert O.within_precision([1393, 1401, 1420], rule)["value"] == 1400       # lower median 1401 -> 1400
    assert O.within_precision([1400, 1441], rule) is None                      # 41 kg
    assert O.within_precision([5.8, 5.9, 5.9], {"spread": 0.1, "round": 0.1})["value"] == 5.9
    assert O.within_precision([5.8, 6.0], {"spread": 0.1, "round": 0.1}) is None
    assert O.within_precision([184, 185], {"spread": 1}) == {
        "value": 184, "agreement": {"rule": "within_precision", "spread": 1, "n_values": 2}}
    assert O.round_to(17.75, 0.1) == 17.8 and O.round_to(1397.4, 5) == 1395 and O.round_to(1397.5, 5) == 1400
    assert O.lower_median([3, 1, 2, 4]) == 2


def test_e2_wheelbase_still_agrees_only_exactly():
    rec = record([row(133, 5.8, wheelbase_mm=2975, wheelbase_mm_min=2975, wheelbase_mm_max=2975),
                  row(133, 5.8, wheelbase_mm=2980, wheelbase_mm_min=2980, wheelbase_mm_max=2980)])
    assert "wheelbase_mm" not in rec["facts"]
    assert held(rec, "wheelbase_mm")[0]["reason"] == "offer_survivors_disagree"


def test_a_run_keeps_the_exact_rules():
    """field_offers without an agreement (a run's open-data layer) is unchanged: no filter, no tolerance."""
    result = {"route": "european", "level": "exact_technical_variant", "keys": {"year": 2020},
              "sources": {EEA: {"status": "ambiguous", "keys_matched": ["co2_wltp"],
                                "survivors": [{"row_id": "a", "fuel": "PETROL", "fuel_mode": "M", "co2_wltp": 133,
                                               "fuel_consumption_l_100km": 4.8},
                                              {"row_id": "b", "fuel": "PETROL", "fuel_mode": "M", "co2_wltp": 133,
                                               "fuel_consumption_l_100km": 5.8}]}}}
    run = next(o for o in O.field_offers(result) if o["field"] == CONSUMPTION)
    assert run["status"] == "survivors_disagree" and "dropped" not in run
    facts = next(o for o in O.field_offers(result, agreement={"sources": [EEA], "fields": {}})
                 if o["field"] == CONSUMPTION)
    assert facts["status"] == "offered" and facts["value"] == 5.8 and set(facts["dropped"]) == {
        "inconsistent_co2_consumption"}


# --- E3: no government CO2 in a WLTP year ----------------------------------------------------------------------------

def test_e3_a_2018_wltp_year_without_government_co2_returns_the_type_code_mass():
    gov = {"co2_wltp": None, "shnat_yitzur": 2018}
    rec = record([row(140, 5.9, 1935, year=2018), row(140, 6.0, 1935, year=2019)], gov=gov)
    assert rec["open_data_match"]["level"] == "exact_technical_variant"
    mass = rec["facts"]["curb_weight_kg"]
    assert mass["value"] == 1935 and mass["basis"] == "type_code_match (no government CO2)"
    # consumption keeps co2_selected strictly: without the government CO2 there is no selector
    assert CONSUMPTION not in rec["facts"] and "energy_consumption_kwh_100km" not in rec["facts"]
    fuel = held(rec, CONSUMPTION)
    assert [w["reason"] for w in fuel] == ["offer_missing_key"]            # the offer's own co2_wltp key gate
    assert rec["co2_selection"][EEA]["fallback"] == "gov_co2_missing"
    # the wheelbase never required a CO2: unchanged, exact
    assert rec["facts"]["wheelbase_mm"]["basis"] == "type_code_match"


def test_e3_energy_consumption_is_requirement_unmet_without_government_co2():
    gov = {"co2_wltp": None, "shnat_yitzur": 2018}
    rec = record([row(0, None, 1935, year=2018, energy_wh_km=177, energy_wh_km_min=177, energy_wh_km_max=177)],
                 gov=gov)
    energy = held(rec, "energy_consumption_kwh_100km")
    assert [(w["reason"], w["detail"]) for w in energy] == [("requirement_unmet", ["co2_selected"])]
    assert rec["facts"]["curb_weight_kg"]["basis"] == "type_code_match (no government CO2)"


def test_e3_with_a_government_co2_the_wltp_rule_is_unchanged():
    rec = record([row(127, 5.8, 1935), row(128, 5.8, 1935)])               # no row has the government 133
    assert "curb_weight_kg" not in rec["facts"]
    assert [(w["reason"], w["detail"]) for w in held(rec, "curb_weight_kg")] == [("requirement_unmet",
                                                                                   ["co2_selected"])]


# --- E4: the shard candidate index ------------------------------------------------------------------------------------

MAKES = {"BMW": ["118I", "320D", "530E", "X1", "X5"], "TOYOTA": ["COROLLA", "YARIS", "RAV4", "C-HR"],
         "VOLKSWAGEN": ["GOLF", "POLO", "PASSAT", "TIGUAN"], "VOLVO": ["XC40", "XC60", "V60"]}


def _synthetic_rows(year: int, rnd: random.Random) -> list[dict]:
    rows = []
    for make, models in MAKES.items():
        for n in range(220):
            model = rnd.choice(models)
            variant = f"{model[:2]}{rnd.randint(10, 40)}"
            version = rnd.choice([f"{variant}A{rnd.randint(0, 9)}", f"{variant}-B (EU)", f"V{rnd.randint(100, 999)}"])
            text = rnd.choice([model, f"{model} 2.0", f"NEW {model} XDRIVE", f"{model} HYBRID", ""])
            rows.append({"row_id": f"eea-{year}-F-{make[:2]}{n:04d}", "make": make, "model": text or None,
                         "year": year, "type_approval": f"T{rnd.randint(1, 5)}", "variant": variant, "version": version,
                         "fuel": rnd.choice(["PETROL", "DIESEL", "PETROL/ELECTRIC"]), "fuel_mode": rnd.choice("MHP"),
                         "displacement_cc": rnd.choice([1499, 1998, 2993]), "power_kw": rnd.choice([100, 140, 190]),
                         "co2_wltp": rnd.choice([47, 120, 133, 150]), "mass_running_order_kg": rnd.choice([1365, 1395]),
                         "fuel_consumption_l_100km": rnd.choice([5.8, 5.9, 6.4])})
    return rows


def _keys(rnd: random.Random, rows: list[dict]) -> dict:
    sample = rnd.choice(rows)
    make = sample["make"]
    models = rnd.choice([[rnd.choice(MAKES[make])], MAKES[make][:2], [], ["NOPE"]])
    code = rnd.choice([sample["variant"], sample["version"], str(sample["version"]).split(" (")[0], "ZZ99", None])
    co2 = rnd.choice([None, sample["co2_wltp"], 999])
    return {"manufacturer": make, "makes": [make] if rnd.random() > 0.05 else [make, "ALPINA"], "type_code": code,
            "models": [ds.norm_text(m) for m in models], "family": None, "year": rnd.choice([2020, 2021]),
            "cc": rnd.choice([1499, 1998, None]), "power": rnd.choice([136, 190, 258, None]),
            "propulsion": rnd.choice(["conventional", "hybrid", None]), "drivetrain": None,
            "transmission": None, "body": None, "doors": None, "gross_weight": 1900, "co2_wltp": co2,
            "route": "european", "words": [], "battery": [], "bev_or_no_co2": co2 is None}


def test_e4_the_index_returns_the_scans_candidates_on_200_keys(tmp_path, monkeypatch):
    rnd = random.Random(20261009)
    all_rows = []
    for year in (2020, 2021, 2022):
        rows = _synthetic_rows(year, rnd)
        ds.write_shard(EEA, rows, {}, year=year, folder=tmp_path)
        all_rows += rows
    keys = [_keys(rnd, all_rows) for _ in range(200)]
    monkeypatch.setenv("TRIPY_SHARD_INDEX", "0")                          # the scan (nothing is queued either)
    scanned = [dumps(M.match_source(EEA, k, folder=tmp_path)) for k in keys]
    monkeypatch.setenv("TRIPY_SHARD_INDEX", "1")
    SI.clear()
    for path in ds.shard_files(EEA, folder=tmp_path):
        assert SI.build_now(path) is not None
    real_query = ds.query_rows
    monkeypatch.setattr(ds, "query_rows", lambda *a, **k: pytest.fail("the index path must not scan"))
    indexed = [dumps(M.match_source(EEA, k, folder=tmp_path)) for k in keys]
    monkeypatch.setattr(ds, "query_rows", real_query)
    assert indexed == scanned
    outcomes = [json.loads(s) for s in scanned]
    statuses = {o.get("type_code", {}).get("status") for o in outcomes}
    assert {"match", "no_match"} <= statuses                                # both paths are exercised
    assert sum(o["candidates"] > 0 for o in outcomes) > 50
    SI.clear()


def test_e4_requests_never_wait_and_the_cap_evicts(tmp_path, monkeypatch):
    rows = _synthetic_rows(2020, random.Random(1))
    path = ds.write_shard(EEA, rows, {}, year=2020, folder=tmp_path)
    SI.clear()
    keys = {"makes": ["BMW"], "year": 2020}
    seen = []
    monkeypatch.setattr(SI, "request", lambda p: seen.append(p))           # queued for the worker, never built inline
    assert SI.indexed_candidates(EEA, keys["makes"], [2020, 2021], lambda reps: set(), folder=tmp_path) is None
    assert seen == [path] and SI.get(path) is None
    monkeypatch.undo()
    index = SI.build_now(path)
    assert index.rows == len(rows) and SI.get(path) is index and index.bytes > 0
    monkeypatch.setattr(SI, "MEMORY_CAP_BYTES", index.bytes + 1)
    other = ds.write_shard(EEA, _synthetic_rows(2021, random.Random(2)), {}, year=2021, folder=tmp_path)
    SI.build_now(other)
    assert SI.get(path) is None and SI.get(other) is not None               # the least recently used one went
    assert SI.status()["evicted"] >= 1
    SI.clear()


def test_e4_the_warm_up_uses_the_free_space_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPY_SHARD_WARMUP", "1")
    calls = []

    def files(name, years=None, report=None, **_):
        calls.append((name, tuple(years or [])))
        return []                                     # e.g. every shard refused by the free-space guard: nothing indexed

    monkeypatch.setattr(ds, "shard_files", files)
    monkeypatch.setitem(SI._STATE, "warmup", None)
    SI.start_warmup(years=(2015, 2016))
    SI._STATE["warmup"].join(timeout=10)
    assert calls == [(EEA, (2015,)), (EEA, (2016,))] and SI._STATE["warmup_done"] is True
    monkeypatch.setenv("TRIPY_SHARD_WARMUP", "0")
    monkeypatch.setitem(SI._STATE, "warmup", None)
    SI.start_warmup()
    assert SI._STATE["warmup"] is None


def test_canonical_of_is_memoized_and_unchanged():
    from src.open_data import makes

    table = makes.canonical()
    for spelling in ("BMW", "B.M.W.", "bmw ag", "TOYOTA", "LAND ROVER", "1OYOTA", "", None, "HIUNDAI"):
        key = makes.normalize_make(spelling, table)
        expected = next((m for m in makes.canonical_makes(table) if makes.normalize_make(m, table) == key),
                        None) if key else None
        assert makes.canonical_of(spelling) == expected == makes.canonical_of(spelling), spelling

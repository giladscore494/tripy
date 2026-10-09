"""The vehicle facts API for yeda-rechev (src/facts, /api/facts/v1): the government Level 1.5 row merged with the
admitted open-data facts. No network: a fake read-only query stands in for MILO and the snapshot rows are verbatim
copies of the committed shards (tests/fixtures/facts_rows.json)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.auth import RateLimiter
from src.facts import record as R
from src.facts import service as S
from src.facts import versions as V
from src.facts import versions as V
from src.facts.service import FactsService, SnapshotsUnavailable
from src.open_data import datasets as ds
from test_api import ACCESS, data_root, gate  # noqa: F401  (the API fixtures)

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "tests/fixtures/facts_rows.json").read_text("utf-8"))
FACTS_TOKEN = "facts-token-0123456789abcdef-yeda"


def _level15(record: str) -> dict:
    if record == "19754":
        return copy.deepcopy(FIX["level15_19754"])
    rows = json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    rows += json.loads((ROOT / "data/open_data_coverage_records.json").read_text("utf-8"))["rows"]
    return copy.deepcopy(next(r for r in rows if r["upstream_record_id"] == record))


ROWS = {rec: _level15(rec) for rec in ("22010", "19754", "85167")}
KEY = {rec: row["variant_identity_key"] for rec, row in ROWS.items()}


class FakeMilo:
    """The read-only query function: db.LEVEL15_BY_KEY_SQL by variant_identity_key, the private segment only."""

    def __init__(self, rows=None):
        self.rows = {r["variant_identity_key"]: r for r in (rows or ROWS.values())}
        self.queries = []

    def __call__(self, sql: str, params: dict) -> list[dict]:
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("SELECT") and "variant_identity_key = ANY" in sql
        assert "vehicle_segment = 'private'" in sql
        return [copy.deepcopy(self.rows[k]) for k in params["keys"] if k in self.rows
                and self.rows[k].get("vehicle_segment", "private") == "private"]


def snapshot_rows(row: dict, edit=None) -> dict:
    rows = copy.deepcopy(FIX["rows"][str(row["upstream_record_id"])])
    return edit(rows) or rows if edit else rows


def service(rows=None, edit=None, **kw) -> FactsService:
    return FactsService(FakeMilo(rows), rows_by_source=lambda row: snapshot_rows(row, edit), **kw)


def one(svc: FactsService, record: str, debug: bool = True) -> dict:
    return svc.records([KEY[record]], debug=debug)[0][0]


def _open(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if v["source"] != "government"}


# --- the admission data ------------------------------------------------------------------------------------------------

def test_every_admission_entry_names_an_existing_offer():
    field_map = ds.config()["field_map"]
    offers = {(e["source"], e["field"]) for e in field_map}
    adm = V.admission()
    assert adm["consumer"] == "yeda_rechev" and adm["version"]
    for entry in adm["entries"]:
        assert (entry["source"], entry["field"]) in offers, entry
        if entry.get("definition"):
            assert any(e["source"] == entry["source"] and e["field"] == entry["field"]
                       and e.get("definition") == entry["definition"] for e in field_map), entry
        require = entry["require"]
        names = [r for group in (require.values() if isinstance(require, dict) else [require]) for r in group]
        assert set(names) <= set(adm["requires"])
    assert ds.admission_allowlist() == set()          # the run allowlist is untouched: runs stay in shadow


# --- 22010 ---------------------------------------------------------------------------------------------------------------

def test_22010_merges_the_government_row_and_the_admitted_eea_facts():
    record = one(service(), "22010")
    facts = record["facts"]
    assert record["status"] == "ok" and record["variant_identity_key"] == KEY["22010"]
    assert record["identity"]["sug_tkina"] == "european" and record["identity"]["degem_nm"] == "JP91"
    assert record["identity"]["model_year"] == 2020 and record["identity"]["automatic"] is True
    wheelbase = facts["wheelbase_mm"]
    assert (wheelbase["value"], wheelbase["unit"], wheelbase["source"]) == (2975, "mm", "eea_co2_cars")
    assert wheelbase["source_level"] == "open_data" and wheelbase["identity_level"] == "exact_technical_variant"
    assert wheelbase["basis"] == "type_code_match+co2" and wheelbase["licence"] == "CC-BY-4.0"
    assert "European Environment Agency" in wheelbase["attribution"]
    assert wheelbase["corroborated_by"] == [{"source": "tc_cvs", "value": 2980, "row_ids": ["tc_cvs-2020_en.csv-90"]}]
    mass = facts["curb_weight_kg"]                    # the admission's mass_running_order_kg (eu_running_order)
    assert (mass["value"], mass["unit"], mass["definition"], mass["source"]) == (1935, "kg", "eu_running_order",
                                                                                 "eea_co2_cars")
    consumption = facts["fuel_consumption_combined_l_100km"]
    assert (consumption["value"], consumption["standard"], consumption["dataset_year"]) == (2.1, "WLTP", 2021)
    # never: the CVS dimensions of a European target, EPA cargo / consumption, an American gearbox
    for name in ("length_mm", "width_mm", "height_mm", "cargo_volume_l", "gearbox_type", "gear_count"):
        assert name not in facts
    assert set(_open(facts)) == {"wheelbase_mm", "curb_weight_kg", "fuel_consumption_combined_l_100km",
                                 "energy_consumption_kwh_100km"}
    # E2 (the row MIN / MAX rule): the one JP91 + CO2 47 row states 177 Wh/km with MIN 177 / MAX 178 (17.7-17.8
    # kWh/100km, within 0.2): it counts with its stored value, exactly (no agreement key); main withheld it (offer_range)
    energy = facts["energy_consumption_kwh_100km"]
    assert (energy["value"], energy["unit"], energy["row_ids"]) == (17.7, "kWh/100km", ["eea-2021-F-30738"])
    assert "agreement" not in energy and "agreement" not in consumption and "agreement" not in mass
    reasons = {(w["source"], w["field"]): w["reason"] for w in record["withheld"]}
    assert reasons[("tc_cvs", "length_mm")] == "never_admitted"
    assert reasons[("epa_fueleconomy", "cargo_volume_l")] == "never_admitted"
    assert reasons[("epa_fueleconomy", "gearbox_type")] == "never_admitted"
    assert record["open_data_match"] == {"route": "european", "level": "exact_technical_variant",
                                         "designation": "530E XDRIVE IPERFORMANCE JP91"}
    gov = facts["horsepower"]
    assert (gov["value"], gov["unit"], gov["source"], gov["source_level"]) == (184, "hp", "government", "government")
    assert gov["row_ids"] == ["22010"] and facts["co2_wltp"]["standard"] == "WLTP"
    assert facts["automatic"]["value"] is True and facts["abs"]["value"] is True


def test_the_government_fields_use_yeda_rechevs_canonical_names():
    names = {name for name, *_ in R.GOVERNMENT_FIELDS}
    assert names == {"fuel_type", "propulsion", "drivetrain", "body_style", "engine_cc", "horsepower", "automatic",
                     "doors", "seats", "gross_weight_kg", "towing_braked_kg", "towing_unbraked_kg", "co2_wltp",
                     "nox_wltp", "co_wltp", "hc_wltp", "co2_city", "co2_highway", "pollution_group", "green_index",
                     "safety_score", "safety_equipment_level", "airbags", "abs", "esc"}
    facts = one(service(), "22010", debug=False)["facts"]
    assert len([k for k in facts if k.startswith("adas.")]) == 19          # equipment_stated: all 19 stated


# --- zero semantics -------------------------------------------------------------------------------------------------------

def test_zero_semantics_drop_unknown_zeros_and_keep_values():
    row = {**ROWS["22010"], "kosher_grira_im_blamim": 0, "kosher_grira_bli_blamim": 0, "mispar_kariot_avir": 3}
    record = one(service(rows=[row]), "22010")
    facts = record["facts"]
    assert "towing_braked_kg" not in facts and "towing_unbraked_kg" not in facts
    assert facts["airbags"]["value"] == 3
    zero = {w["field"] for w in record["withheld"] if w["reason"] == "zero_means_unknown"}
    assert {"towing_braked_kg", "towing_unbraked_kg"} <= zero
    no_bags = one(service(rows=[{**row, "mispar_kariot_avir": 0, "co2_wltp": 0, "nox_wltp": 0}]), "22010")["facts"]
    assert "airbags" not in no_bags and "co2_wltp" not in no_bags and "nox_wltp" not in no_bags
    manual = one(service(rows=[{**row, "automatic_ind": 0}]), "22010")["facts"]
    assert manual["automatic"]["value"] is False                            # zero_means value: a manual gearbox


def test_zero_is_unknown_for_power_counts_and_ratings():
    row = {**ROWS["22010"], "koah_sus": 0, "nikud_betihut": 0}
    record = one(service(rows=[row]), "22010")
    assert "horsepower" not in record["facts"] and "safety_score" not in record["facts"]
    zero = {w["field"] for w in record["withheld"] if w["reason"] == "zero_means_unknown"}
    assert {"horsepower", "safety_score"} <= zero
    rules = V.zero_semantics()["fields"]
    for name in ("horsepower", "seats", "doors", "gross_weight_kg", "pollution_group", "green_index", "safety_score",
                 "safety_equipment_level"):
        assert rules[name]["zero_means"] == "unknown", name
    assert rules["abs"]["zero_means"] == rules["esc"]["zero_means"] == "value"
    no_abs = one(service(rows=[{**row, "abs_ind": 0, "bakarat_yatzivut_ind": 0}]), "22010")["facts"]
    assert no_abs["abs"]["value"] is False and no_abs["esc"]["value"] is False


def test_engine_cc_zero_is_a_value_only_for_a_battery_electric_car():
    ev = one(service(rows=[{**ROWS["22010"], "nefah_manoa": 0, "norm_propulsion_technology": "battery_electric"}]),
             "22010")
    assert ev["facts"]["engine_cc"]["value"] == 0
    petrol = one(service(rows=[{**ROWS["22010"], "nefah_manoa": 0, "norm_propulsion_technology": "conventional"}]),
                 "22010")
    assert "engine_cc" not in petrol["facts"]
    assert {"source": "government", "field": "engine_cc", "reason": "zero_means_unknown"} in petrol["withheld"]
    unknown_propulsion = one(service(rows=[{**ROWS["22010"], "nefah_manoa": 0, "norm_propulsion_technology": None}]),
                             "22010")
    assert "engine_cc" not in unknown_propulsion["facts"]


def test_an_adas_flag_appears_only_when_stated():
    row = {**ROWS["22010"], "equipment_stated": 0b101, "equipment_on": 0b001}
    record = one(service(rows=[row]), "22010")
    adas = {k: v["value"] for k, v in record["facts"].items() if k.startswith("adas.")}
    assert adas == {"adas.bakarat_mehirut_isa": True, "adas.bakarat_stiya_activ_s": False}
    unstated = [w for w in record["withheld"] if w["reason"] == "adas_not_stated"]
    assert len(unstated) == 17
    none = one(service(rows=[{**row, "equipment_stated": None, "equipment_on": None}]), "22010")["facts"]
    assert not [k for k in none if k.startswith("adas.")]


# --- government wins -----------------------------------------------------------------------------------------------------

def test_government_wins_and_a_contradiction_is_counted():
    def contradict(rows):
        for r in rows["eea_co2_cars"]:
            r["seats"] = 7                                                # the government row states 5 seats
    record = one(service(edit=contradict), "22010")
    assert not [k for k, v in record["facts"].items() if v["source"] != "government" and k in ("horsepower", "seats")]
    assert record["facts"]["seats"]["value"] == 5 and record["facts"]["seats"]["source"] == "government"
    found = [w for w in record["withheld"] if w["reason"] == "contradicts_government"]
    assert found == [{"source": "eea_co2_cars", "field": "seats", "reason": "contradicts_government",
                      "government_field": "seats", "government": 5, "values": ["7"]}]
    assert "power_kw" not in record["facts"]                              # an EEA column, never a fact


def test_an_open_value_of_a_government_field_is_never_returned():
    result = {"route": "european", "level": "exact_technical_variant", "sources": {
        "eea_co2_cars": {"status": "unique", "survivors": [{"row_id": "r1", "power_kw": 100.0}]}}}
    offers = [{"field": "horsepower", "source": "eea_co2_cars", "status": "offered", "value": 136, "row_ids": ["r1"],
               "identified_by": "unique"}]
    adm = {"entries": [{"source": "eea_co2_cars", "field": "horsepower", "route": "european",
                        "min_level": "exact_technical_variant", "require": ["all_survivors_agree"]}],
           "government_overlap": V.admission()["government_overlap"]}
    gov = {"horsepower": {"value": 184}}
    out = R.open_data_facts(result, offers, gov, adm, field_map=[])
    assert out["facts"] == {}
    reasons = sorted((w["field"], w["reason"]) for w in out["withheld"])
    assert reasons == [("horsepower", "contradicts_government"), ("power_kw", "contradicts_government")]
    same = R.open_data_facts(result, [{**offers[0], "value": 184}], gov, adm, field_map=[])
    assert ("horsepower", "government_wins") in {(w["field"], w["reason"]) for w in same["withheld"]}


# --- two open sources ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cvs_cm, outcome", [(298, "corroborated"), (299, "conflict")])
def test_eea_and_cvs_wheelbase(cvs_cm, outcome):
    def cvs(rows):
        for r in rows["tc_cvs"]:
            r["wheelbase_cm"] = cvs_cm
    record = one(service(edit=cvs), "22010")
    if outcome == "corroborated":                                        # 2975 vs 2980: equal within D1 (cm rounding)
        assert record["facts"]["wheelbase_mm"]["value"] == 2975
        assert record["facts"]["wheelbase_mm"]["corroborated_by"][0]["source"] == "tc_cvs"
    else:                                                                # 2975 vs 2990: neither is returned
        assert "wheelbase_mm" not in record["facts"]
        conflict = [w for w in record["withheld"] if w["reason"] == "source_conflict"]
        assert conflict and conflict[0]["field"] == "wheelbase_mm"
        assert conflict[0]["detail"] == ["eea_co2_cars=2975", "tc_cvs=2990"]


# --- NEDC and American approval -------------------------------------------------------------------------------------------

def test_a_2014_bmw_returns_the_nedc_co2_and_no_wltp_co2_from_eea():
    record = one(service(), "19754")
    facts = record["facts"]
    assert record["identity"]["model_year"] == 2014 and record["identity"]["sug_tkina"] == "european"
    nedc = facts["co2_nedc_g_km"]
    assert (nedc["value"], nedc["standard"], nedc["unit"], nedc["source"]) == (184, "NEDC", "g/km", "eea_co2_cars")
    assert nedc["basis"] == "type_code_match" and nedc["dataset_year"] == [2014, 2015]   # the registration-year window
    assert "co2_wltp" not in facts                                        # the government row has none; EEA never
    assert not [k for k, v in facts.items() if v["source"] == "eea_co2_cars" and "wltp" in k]
    assert facts["curb_weight_kg"]["value"] == 1900                       # NEDC year: type_code_match, not co2


def test_an_american_approval_record_gets_cvs_and_epa_facts():
    record = one(service(), "85167")
    facts = record["facts"]
    assert record["open_data_match"]["route"] == "american" and record["identity"]["sug_tkina"] == "american"
    assert (facts["wheelbase_mm"]["value"], facts["wheelbase_mm"]["source"]) == (2860, "tc_cvs")
    assert (facts["curb_weight_kg"]["value"], facts["curb_weight_kg"]["definition"]) == (1940, "na_curb")
    assert (facts["gearbox_type"]["value"], facts["gear_count"]["value"]) == ("automatic", 9)
    assert facts["gear_count"]["source"] == "epa_fueleconomy"
    assert "length_mm" not in facts                                       # uncorroborated (no second wheelbase)
    assert "fuel_consumption_combined_l_100km" not in facts               # EPA / NRCan cycles: never
    assert "nrcan_fuel_ratings" not in {v["source"] for v in facts.values()}


def test_a_never_rule_wins_over_an_entry():
    adm = copy.deepcopy(V.admission())
    adm["entries"].append({"source": "epa_fueleconomy", "field": "cargo_volume_l", "route": "european",
                           "min_level": "body_powertrain", "require": []})
    result = {"route": "european", "level": "exact_technical_variant", "sources": {}}
    offers = [{"field": "cargo_volume_l", "source": "epa_fueleconomy", "status": "offered", "value": 10}]
    out = R.open_data_facts(result, offers, {}, adm, field_map=[])
    assert out["facts"] == {} and out["withheld"][0]["reason"] == "never_admitted"


# --- the record's shape ---------------------------------------------------------------------------------------------------

def _nulls(value, path="") -> list[str]:
    if value is None or value == "" or value == [] or value == {}:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _nulls(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for n, v in enumerate(value) for p in _nulls(v, f"{path}[{n}]")]
    return []


def test_no_placeholder_or_null_anywhere():
    svc = service()
    records, _ = svc.records([KEY["22010"], KEY["19754"], KEY["85167"]])
    for record in records:
        assert _nulls(record["facts"]) == [] and _nulls(record["identity"]) == []
        assert all(fact["value"] not in (None, "", "unknown") for fact in record["facts"].values())
        assert "withheld" not in record                                   # owner diagnostics only


def test_an_unknown_key_is_not_found_and_the_others_return():
    records, line = service().records([KEY["22010"], "0" * 64])
    assert records[0]["status"] == "ok"
    assert records[1] == {"variant_identity_key": "0" * 64, "status": "not_found", "http_status": 404,
                          "versions": {"contract": "vehicle-facts/1"}}
    assert line["status"] == {KEY["22010"]: "ok", "0" * 64: "not_found"}


def test_a_commercial_row_is_not_served():
    row = {**ROWS["22010"], "vehicle_segment": "commercial"}
    assert service(rows=[row]).records([KEY["22010"]])[0][0]["status"] == "not_found"


def test_two_calls_are_byte_identical():
    keys = [KEY["22010"], KEY["19754"], KEY["85167"]]
    first = S.dumps(service().records(keys)[0])
    again = service()
    assert S.dumps(again.records(keys)[0]) == first
    assert S.dumps(again.records(keys)[0]) == first                       # from the cache: the same bytes


# --- caching and availability ---------------------------------------------------------------------------------------------

def test_the_open_part_is_cached_in_memory_and_on_disk_and_the_government_row_is_read_every_call(tmp_path):
    milo = FakeMilo()
    calls = []

    def rows(row):
        calls.append(row["upstream_record_id"])
        return snapshot_rows(row)
    svc = FactsService(milo, tmp_path / "facts_cache", rows_by_source=rows)
    _, line = svc.records([KEY["22010"]])
    assert line["cache"] == {KEY["22010"]: "miss"} and len(list((tmp_path / "facts_cache").glob("*.json"))) == 1
    _, line = svc.records([KEY["22010"]])
    assert line["cache"] == {KEY["22010"]: "memory"} and len(milo.queries) == 2 and calls == ["22010"]
    fresh = FactsService(milo, tmp_path / "facts_cache", rows_by_source=rows)
    _, line = fresh.records([KEY["22010"]])
    assert line["cache"] == {KEY["22010"]: "disk"} and calls == ["22010"]
    # a MILO update reaches the record without a sync: the government row is read every call
    milo.rows[KEY["22010"]] = {**ROWS["22010"], "mispar_moshavim": 4, "content_sha256": "changed"}
    assert fresh.records([KEY["22010"]])[0][0]["facts"]["seats"]["value"] == 4


def test_the_lru_is_bounded():
    svc = service(lru_size=1)
    svc.records([KEY["22010"]])
    svc.records([KEY["19754"]])
    assert len(svc._lru) == 1
    assert S.LRU_SIZE == 2000


def test_a_refused_cache_write_still_answers(tmp_path, monkeypatch):
    from src.storage import disk

    monkeypatch.setattr(disk, "free_bytes", lambda path: 0)
    svc = service(cache_dir=tmp_path / "facts_cache")
    assert svc.records([KEY["22010"]])[0][0]["status"] == "ok"
    assert not list((tmp_path / "facts_cache").glob("*.json"))


def test_without_milo_the_service_is_unavailable_never_the_snapshot():
    from src.catalog import CatalogUnavailable

    with pytest.raises(CatalogUnavailable):
        FactsService(None).records([KEY["22010"]])

    def down(sql, params):
        raise OSError("connection refused")
    with pytest.raises(CatalogUnavailable):
        FactsService(down).records([KEY["22010"]])


def test_a_refused_shard_decompression_is_snapshots_unavailable(monkeypatch):
    def refused(row, government, **kw):
        ds._STATE["disk_refusals"] = ds.disk_refusals() + 1
        return {"facts": {}, "open_data_match": {}, "withheld": []}
    monkeypatch.setattr(S, "open_data_part", refused)
    svc = service()
    with pytest.raises(SnapshotsUnavailable):
        svc.records([KEY["22010"]])
    assert not svc._lru                                                   # nothing cached


def test_the_free_space_guard_refuses_a_decompression(tmp_path, monkeypatch):
    import gzip
    import hashlib

    from src.storage import disk

    repo = tmp_path / "repo"
    (repo / "eea_co2_cars").mkdir(parents=True)
    shard = repo / "eea_co2_cars" / "2020.sqlite.gz"
    shard.write_bytes(gzip.compress(b"x" * 1000))
    sha = hashlib.sha256(shard.read_bytes()).hexdigest()
    ds.set_repo_dir(repo, tmp_path / "tmp")
    try:
        assert ds.gzip_size(shard) == 1000
        monkeypatch.setattr(disk, "free_bytes", lambda path: disk.MIN_FREE_BYTES + 999)
        before = ds.disk_refusals()
        assert ds._materialize_file("eea_co2_cars", "eea_co2_cars/2020.sqlite.gz", sha) == (None, "insufficient_disk")
        assert ds.disk_refusals() == before + 1 and not (tmp_path / "tmp").exists()
        monkeypatch.setattr(disk, "free_bytes", lambda path: disk.MIN_FREE_BYTES + 1000)   # room again: retried
        path, problem = ds._materialize_file("eea_co2_cars", "eea_co2_cars/2020.sqlite.gz", sha)
        assert problem is None and path.read_bytes() == b"x" * 1000
    finally:
        ds.set_repo_dir(None)


# --- the HTTP API ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(data_root, gate):
    from test_api import make_context, scripted_research

    context = make_context(scripted_research(gate))
    context.facts = service()
    yield context
    gate.set()
    context.manager.shutdown(grace_s=5)


def test_post_vehicles_returns_one_record_per_key_in_order(api):
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        body = {"variant_identity_keys": [KEY["85167"], "missing", KEY["22010"]]}
        first = client.post("/api/facts/v1/vehicles", json=body)
        again = client.post("/api/facts/v1/vehicles", json=body)
        assert first.status_code == 200 and first.content == again.content          # byte-identical
        data = first.json()
        assert data["contract"] == "vehicle-facts/1"
        assert [v["status"] for v in data["vehicles"]] == ["ok", "not_found", "ok"]
        assert first.content.decode("utf-8") == S.dumps(data)                         # sorted keys
        for keys in ([], [KEY["22010"]] * 4):
            assert client.post("/api/facts/v1/vehicles", json={"variant_identity_keys": keys}).status_code == 422
        debug = client.post("/api/facts/v1/vehicles?debug=1", json={"variant_identity_keys": [KEY["22010"]]}).json()
        assert debug["vehicles"][0]["withheld"] and debug["vehicles"][0]["withheld_counts"]["never_admitted"] == 9


def test_the_milo_db_unreachable_is_503_catalog_unavailable(api):
    api.facts = FactsService(None)
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        response = client.post("/api/facts/v1/vehicles", json={"variant_identity_keys": [KEY["22010"]]})
        assert response.status_code == 503 and response.json()["error"]["code"] == "catalog_unavailable"
        assert client.get("/api/facts/v1/catalog/manufacturers").status_code == 503


def test_snapshots_unavailable_is_503(api, monkeypatch):
    def refused(row, government, **kw):
        ds._STATE["disk_refusals"] = ds.disk_refusals() + 1
        return {"facts": {}, "open_data_match": {}, "withheld": []}
    monkeypatch.setattr(S, "open_data_part", refused)
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        response = client.post("/api/facts/v1/vehicles", json={"variant_identity_keys": [KEY["22010"]]})
        assert response.status_code == 503 and response.json()["error"]["code"] == "snapshots_unavailable"


def test_the_contract_endpoint(api):
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        data = client.get("/api/facts/v1/contract").json()
    assert data["contract"] == "vehicle-facts/1" and data["schema"]["title"] == "vehicle-facts/1"
    assert data["admission"] == {"version": "facts-admission-v2", "consumer": "yeda_rechev"}
    assert data["snapshots"]["manifest_sha256"] == V.snapshots()["sha256"] != "no_manifest"
    assert data["snapshots"]["built_at"] == ds.manifest()["built_at"]


class PickerDb:
    ROWS = [{"variant_identity_key": "k1", "upstream_record_id": "1", "tozar": "ב מ וו", "kinuy_mishari": "530E",
             "shnat_yitzur": 2020, "ramat_gimur": "M SPORT", "degem_nm": "JP91", "koah_sus": 184, "nefah_manoa": 1998,
             "norm_propulsion_technology": "plug_in", "norm_drivetrain": "awd", "norm_body_style": "sedan",
             "vehicle_segment": "private"},
            {"variant_identity_key": "k2", "upstream_record_id": "2", "tozar": "ב מ וו", "kinuy_mishari": "530E",
             "shnat_yitzur": 2020, "ramat_gimur": "VAN", "vehicle_segment": "commercial"}]

    def __init__(self):
        self.queries = []

    def __call__(self, sql, params):
        self.queries.append((sql, params))
        rows = [r for r in self.ROWS if r["vehicle_segment"] == params.get("segment", r["vehicle_segment"])
                and all(r[c] == params[k] for k, c in (("manufacturer", "tozar"), ("model", "kinuy_mishari"),
                                                        ("year", "shnat_yitzur")) if k in params)]
        if "GROUP BY" in sql:
            column = sql.split("GROUP BY ")[1].split()[0]
            return [{column: r[column], "n": 1} for r in rows]
        return rows


def test_the_picker_lists_private_variants_with_their_keys(api):
    from src.catalog import CatalogBrowser

    db = PickerDb()
    api.catalog_browser = CatalogBrowser(db)
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        assert client.get("/api/facts/v1/catalog/manufacturers").json() == {
            "manufacturers": [{"manufacturer": "ב מ וו", "variants": 1}]}
        assert client.get("/api/facts/v1/catalog/models", params={"manufacturer": "ב מ וו"}).json()["models"] == [
            {"model": "530E", "variants": 1}]
        assert client.get("/api/facts/v1/catalog/years", params={"manufacturer": "ב מ וו", "model": "530E"}
                          ).json()["years"] == [{"year": 2020, "variants": 1}]
        trims = client.get("/api/facts/v1/catalog/trims", params={"manufacturer": "ב מ וו", "model": "530E",
                                                                  "year": 2020}).json()["trims"]
        assert [t["variant_identity_key"] for t in trims] == ["k1"]
        assert trims[0]["label"] == "M SPORT · 184 hp · 1998 cc · plug_in · JP91"
        assert all(p.get("segment") == "private" for _, p in db.queries)
        assert all("vehicle_segment = %(segment)s" in sql for sql, _ in db.queries)
        client.get("/api/facts/v1/catalog/manufacturers")                 # cached like /api/catalog
        assert len(db.queries) == 4


# --- auth -----------------------------------------------------------------------------------------------------------------

def test_the_facts_token_opens_only_the_facts_api(api, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    monkeypatch.setenv("TRIPY_FACTS_TOKEN", FACTS_TOKEN)
    facts = {"Authorization": f"Bearer {FACTS_TOKEN}"}
    operator = {"Authorization": f"Bearer {ACCESS}"}
    body = {"variant_identity_keys": [KEY["22010"]]}
    with TestClient(create_app(context=api, mount_mcp=False, frontend=None)) as client:
        assert client.get("/api/runs", headers=facts).status_code == 401
        assert client.get("/api/catalog/status", headers=facts).status_code == 401
        assert client.post("/api/facts/v1/vehicles", json=body, headers=facts).status_code == 200
        assert client.post("/api/facts/v1/vehicles", json=body, headers=operator).status_code == 200
        assert client.post("/api/facts/v1/vehicles", json=body).status_code == 401
        wrong = {"Authorization": f"Bearer {FACTS_TOKEN[:-1]}"}
        assert client.post("/api/facts/v1/vehicles", json=body, headers=wrong).status_code == 401
        assert client.get("/api/facts/v1/contract").status_code == 401
        refused = client.post("/api/facts/v1/vehicles?debug=1", json=body, headers=facts)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "debug_requires_operator"
        assert "withheld" in client.post("/api/facts/v1/vehicles?debug=1", json=body,
                                         headers=operator).json()["vehicles"][0]
        assert FACTS_TOKEN not in client.post("/api/facts/v1/vehicles", json=body, headers=wrong).text


def test_production_without_tokens_fails_closed(api, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.delenv("TRIPY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TRIPY_FACTS_TOKEN", raising=False)
    with TestClient(create_app(context=api, mount_mcp=False, frontend=None)) as client:
        response = client.get("/api/facts/v1/contract", headers={"Authorization": f"Bearer {FACTS_TOKEN}"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "access_control_not_configured"
        assert client.get("/api/runs").status_code == 503
    # only the facts token configured: the facts API works with it, the rest of /api stays closed (503)
    monkeypatch.setenv("TRIPY_FACTS_TOKEN", FACTS_TOKEN)
    with TestClient(create_app(context=api, mount_mcp=False, frontend=None)) as client:
        assert client.get("/api/facts/v1/contract",
                          headers={"Authorization": f"Bearer {FACTS_TOKEN}"}).status_code == 200
        assert client.get("/api/runs", headers={"Authorization": f"Bearer {FACTS_TOKEN}"}).status_code == 503


def test_the_facts_token_is_redacted():
    from src.app_config import SECRET_VARS, redact

    assert "TRIPY_FACTS_TOKEN" in SECRET_VARS
    assert FACTS_TOKEN not in redact(f"token {FACTS_TOKEN}", {"TRIPY_FACTS_TOKEN": FACTS_TOKEN}.get)


def test_the_rate_limit_is_60_per_minute_per_token(api):
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert all(limiter.allow("a")[0] for _ in range(60))
    assert limiter.allow("a") == (False, 61) and limiter.allow("b")[0]
    now[0] = 60.0
    assert limiter.allow("a")[0]
    app = create_app(context=api, mount_mcp=False)
    app.state.facts_rate_limiter = RateLimiter(limit=2)
    with TestClient(app) as client:
        assert [client.get("/api/facts/v1/contract").status_code for _ in range(3)] == [200, 200, 429]
        limited = client.get("/api/facts/v1/contract")
        assert limited.json()["error"]["code"] == "rate_limited" and int(limited.headers["Retry-After"]) >= 1


def test_every_call_is_logged_without_values(caplog, tripy_log):
    with tripy_log("tripy.facts"):
        service().records([KEY["22010"], "missing"])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("facts call ")]
    assert len(lines) == 1                                                   # one line per call, captured once
    line = lines[0]
    data = json.loads(line[len("facts call "):])
    assert set(data) == {"time", "keys", "debug", "status", "cache", "facts", "withheld", "latency_ms"}
    assert data["status"] == {KEY["22010"]: "ok", "missing": "not_found"} and data["facts"][KEY["22010"]] > 40
    assert data["withheld"]["never_admitted"] == 9
    assert "2975" not in line and "1935" not in line                     # counts only, never a value


# --- MCP --------------------------------------------------------------------------------------------------------------------

def test_mcp_facts_preview_returns_the_record_plus_withheld(tmp_path):
    from src.mcp_server.server import TOOL_NAMES
    from src.mcp_server.tools import Observer
    from src.storage.paths import resolve_paths

    observer = Observer(resolve_paths({"TRIPY_DATA_DIR": str(tmp_path)}.get), facts=service())
    preview = observer.facts_preview(KEY["22010"])
    assert preview["facts"]["wheelbase_mm"]["value"] == 2975 and preview["withheld"]
    assert preview["withheld_counts"]["never_admitted"] == 9
    assert "facts_preview" in TOOL_NAMES
    assert Observer(resolve_paths({"TRIPY_DATA_DIR": str(tmp_path)}.get),
                    facts=FactsService(None)).facts_preview(KEY["22010"])["error"] == "catalog_unavailable"
    assert not list(tmp_path.rglob("*.json"))                              # the MCP writes nothing

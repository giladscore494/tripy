"""Open-data snapshot builds (F1-F4 of the snapshot-builds PR): required / optional column maps resolved
case-insensitively, the EEA year discovery and per-year schema, the ADEME unit checks against the field schema and the
Min / Max range rule, the EPA luggage / passenger columns, the NRCan per-group / per-file build (the 2-cycle file never
read), the CVS package files and data-dictionary check, the build progress and the read-only status (MCP, run start).

No network. The headers' provenance is stated in tests/fixtures/open_data_live_headers.py: only the EEA 2018 header is
verbatim from a live source (via the reviewer's brief); the NRCan / CVS headers are synthetic.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import datasets as ds
from src.open_data.build import (BuildStopped, build_ademe, build_ckan_files, build_ckan_groups, build_dataset,
                                 build_eea, dictionary_check, resolve_columns, resolve_map, unit_scales)


@pytest.fixture()
def snapshots(tmp_path):
    ds.set_snapshot_dir(tmp_path / "open")
    yield tmp_path / "open"
    ds.set_snapshot_dir(None)


def _cfg(name: str) -> dict:
    return ds.datasets()[name]


# --- F1: required vs optional, case-insensitive ---------------------------------------------------------------------------

def test_every_map_key_states_whether_it_is_required():
    for name, cfg in ds.datasets().items():
        if cfg.get("identity_only"):
            continue
        maps = [cfg["columns"]] if cfg.get("columns") else [g["columns"] for g in cfg.get("resource_groups") or []
                                                            if g.get("columns")]
        assert maps, name
        for columns in maps:
            for key, spec in columns.items():
                assert isinstance(spec.get("required"), bool), (name, key)


def test_the_identity_keys_are_the_required_ones():
    def required(columns):
        return sorted(k for k, v in columns.items() if v["required"])
    assert required(_cfg("eea_co2_cars")["columns"]) == sorted(["make", "model", "fuel", "displacement_cc",
                                                                "power_kw", "year"])
    assert required(_cfg("ademe_car_labelling")["columns"]) == sorted(["make", "model", "fuel", "power_kw"])
    assert required(_cfg("epa_fueleconomy")["columns"]) == sorted(["row_id", "year", "make", "model",
                                                                   "displacement_l", "drive", "transmission"])
    conventional = next(g for g in _cfg("nrcan_fuel_ratings")["resource_groups"] if g["group"] == "conventional")
    assert required(conventional["columns"]) == sorted(["year", "make", "model", "displacement_l", "transmission"])
    assert required(_cfg("tc_cvs")["columns"]) == sorted(["measured_year", "make", "model"])   # year: the file year


def test_spellings_match_case_insensitively_and_an_absent_optional_key_is_recorded_not_fatal():
    columns = {"year": {"source": ["Year"], "required": True}, "range": {"source": ["Range"], "required": False}}
    out = resolve_map(["  YEAR ", "make"], columns)
    assert out["mapping"] == {"year": "  YEAR "} and out["absent"] == ["range"] and not out["missing"]
    with pytest.raises(BuildStopped) as stop:
        resolve_columns(["make"], columns)
    assert stop.value.reason == "schema_mismatch" and stop.value.report["missing"][0]["key"] == "year"
    with pytest.raises(BuildStopped) as stop:                 # two different live columns for one key
        resolve_columns(["Year", "YEAR "], columns)
    assert stop.value.report["ambiguous"][0]["key"] == "year"


def test_a_fallback_spelling_is_read_only_without_the_source_spelling():
    columns = {"model": {"source": ["Modèle"], "fallback": ["Libellé modèle"], "required": True}}
    assert resolve_map(["Modèle", "Libellé modèle"], columns)["mapping"] == {"model": "Modèle"}
    assert resolve_map(["Libellé modèle"], columns)["mapping"] == {"model": "Libellé modèle"}


# --- F2 / F4: each builder against the recorded header ---------------------------------------------------------------------

def test_the_eea_map_resolves_the_2018_header_with_every_required_key():
    out = resolve_map(H.EEA_2018_HEADER, _cfg("eea_co2_cars")["columns"])
    assert not out["missing"] and not out["ambiguous"]
    assert out["mapping"]["displacement_cc"] == "Ec (cm3)" and out["mapping"]["power_kw"] == "Ep (KW)"
    assert out["mapping"]["mass_running_order_kg"] == "M (kg)" and out["mapping"]["energy_wh_km"] == "Z (Wh/km)"
    assert out["mapping"]["fuel_consumption_l_100km"] == "Fc" and out["mapping"]["year"] == "Year"
    assert out["absent"] == ["electric_range_km"]
    assert resolve_map(H.EEA_2018_HEADER, {"r": _cfg("eea_co2_cars")["registrations_column"]})["mapping"] == {"r": "R"}


def eea_grouped(rows: list[dict], query: str) -> list[dict]:
    """A fake DISCODATA for the E1 query: the per-vehicle rows of the query's make value, grouped by the selected
    columns, with SUM(R) as `r` (distinct configurations with a registration count)."""
    import re as _re

    assert "GROUP BY" in query and "ORDER BY" not in query and " AS r " in query + " "
    assert not any(f in query for f in ("MIN(", "MAX(", "AVG(")) and query.count(") AS ") == 1 and query.count(" AS ") == 2
    select = query[len("SELECT "):query.index(" FROM ")]
    columns = [c[1:-1].replace("]]", "]") for c in select.split(",") if c.startswith("[")]
    assert query.split("GROUP BY ", 1)[1] == ",".join(f"[{c}]" for c in columns)
    make = _re.search(r"\[Mk\]=N?'((?:[^']|'')*)'", query).group(1).replace("''", "'")
    groups: dict = {}
    for row in rows:
        if str(row.get("Mk")) != make:
            continue
        key = tuple(row.get(c) for c in columns)
        groups[key] = groups.get(key, 0) + int(row.get("R") or 0)
    return [{**dict(zip(columns, key)), "r": r} for key, r in groups.items()]


def _eea_fetch(pairs, per_year_rows, queries, header=H.EEA_2018_HEADER, grouped=None):
    def fetch(url):
        query = parse_qs(urlparse(url).query)["query"][0]
        queries.append(query)
        assert len(url.split("query=", 1)[1].split("&", 1)[0]) <= 1800
        if query.startswith("SELECT DISTINCT") and "WHERE" not in query:
            return json.dumps({"results": [{"Year": y, "Status": s} for y, s in pairs]}).encode()
        if query.startswith("SELECT TOP 1") and "WHERE" not in query:
            return json.dumps({"results": [H.eea_row(header)]}).encode()
        year = int(query.split("[Year] = ")[1].split()[0])
        status = query.split("[Status] = '")[1][0]
        rows = per_year_rows.get((year, status)) or []
        if query.startswith("SELECT TOP 1"):
            return json.dumps({"results": rows[:1]}).encode()
        if query.startswith("SELECT DISTINCT [Mk]"):
            return json.dumps({"results": [{"Mk": m} for m in sorted({r["Mk"] for r in rows})]}).encode()
        if grouped is not None:
            return grouped(year, status, query)
        return json.dumps({"results": eea_grouped(rows, query)}).encode()
    return fetch


def test_eea_builds_only_the_years_that_exist_preferring_final_rows():
    pairs = [(2017, "F"), (2018, "F"), (2018, "P"), (2019, "P"), (2022, "F")]
    rows = {(2017, "F"): [H.eea_row(Year=2017)], (2018, "F"): [H.eea_row(Year=2018), H.eea_row(Year=2018, Ve="X")],
            (2018, "P"): [H.eea_row(Year=2018, Ve="PROVISIONAL")], (2019, "P"): [H.eea_row(Year=2019, Status="P")]}
    queries: list[str] = []
    built = build_eea(_eea_fetch(pairs, rows, queries))
    reports = {r["year"]: r for r in built["years"]}
    assert sorted(reports) == [2017, 2018, 2019, 2022]            # 2023 (no rows in [latest]) is never queried
    assert not any("2023" in q for q in queries)
    assert reports[2018]["status_used"] == "F" and reports[2018]["rows"] == 2
    assert reports[2019]["status_used"] == "P"                    # provisional only without final rows
    assert reports[2022] == {"year": 2022, "status": "skipped", "reason": "no_rows", "status_used": "F"}
    assert {r["version"] for r in built["rows"]} == {"A1AK1", "X"}   # never the 2018 provisional row
    assert built["rows"][0]["registrations"] == 3 and built["rows"][0]["status"] == "F"
    assert reports[2018]["absent_columns"] == ["electric_range_km"]


def test_eea_reads_each_years_own_schema_and_a_failing_year_stops_only_itself():
    pairs = [(2018, "F"), (2019, "F")]
    no_power = [c for c in H.EEA_2018_HEADER if c != "Ep (KW)"]
    rows = {(2018, "F"): [H.eea_row()], (2019, "F"): [H.eea_row(no_power, Year=2019)]}
    built = build_eea(_eea_fetch(pairs, rows, []))
    reports = {r["year"]: r for r in built["years"]}
    assert reports[2018]["status"] == "built"
    assert reports[2019]["status"] == "stopped" and reports[2019]["missing"][0]["key"] == "power_kw"
    with pytest.raises(BuildStopped) as stop:
        build_eea(_eea_fetch([(2019, "F")], rows, []))
    assert stop.value.reason == "no_year_built"


# the field descriptions the live ADEME schema states (the reviewer's brief, 2026-10-06); the other fields state no unit
ADEME_DESCRIPTIONS = {"Puissance maximale": "Puissance en kW",
                      "Puissance nominale électrique": "Puissance nominale électrique en kW",
                      "Poids à vide": "En Kg", "Masse OM Min": "Masse en ordre de marche mini (en Kg)"}


def _ademe_schema(descriptions=None, **extra) -> list[dict]:
    """The field schema (original names of the brief); descriptions only where the live schema states a unit."""
    descriptions = ADEME_DESCRIPTIONS if descriptions is None else descriptions
    fields = []
    for name in H.ADEME_ORIGINAL_NAMES:
        field = {"key": name.lower().replace(" ", "_"), "x-originalName": name, "type": "string"}
        if name in descriptions:
            field["description"] = descriptions[name]
        if name in extra:
            field["description"] = extra[name]
        fields.append(field)
    return fields


def _ademe_fetch(schema, body):
    cfg = _cfg("ademe_car_labelling")

    def fetch(url):
        if url == cfg["schema_url"]:
            return json.dumps(schema).encode()
        assert url == cfg["csv_url"]
        return body
    return fetch


ADEME_ROW = ["KIA", "EV6", "EV6", "HYUNDAI", "EV6 77.4 kWh GT-Line AWD", "EL", "BERLINE", None, "MOY-SUPER", 8, 239, 160,
             2090, None, "A", 1, None, None, 180, 195, 506, 528, None, None, 0, 0, 2090, 2180, 60000]


def _ademe_rows(*overrides) -> list[list]:
    rows = []
    for n, change in enumerate(overrides or [{}]):
        row = list(ADEME_ROW)
        for column, value in change.items():
            row[H.ADEME_ORIGINAL_NAMES.index(column)] = value
        rows.append(row)
    return rows


def test_ademe_builds_from_the_original_names_with_the_units_its_descriptions_state():
    body = H.csv_text(H.ADEME_ORIGINAL_NAMES, _ademe_rows({}, {"Description Commerciale": "EV6 RWD"},
                                                           {"Nombre rapports": 1}, {"Marque": "RENAULT"},
                                                           {"Conso elec Min": 170}), delimiter=";")
    built = build_ademe(_ademe_fetch(_ademe_schema(), body))
    assert len(built["rows"]) == 5
    units = built["column_units"]
    assert {k: units[k]["unit"] for k in ("power_kw", "electric_power_kw", "mass_empty_kg", "mass_running_order_kg",
                                          "mass_running_order_kg_max")} == \
        {"power_kw": "kW", "electric_power_kw": "kW", "mass_empty_kg": "kg", "mass_running_order_kg": "kg",
         "mass_running_order_kg_max": "kg"}
    assert units["mass_running_order_kg"]["stated"] == "Masse en ordre de marche mini (en Kg)"
    for key in ("energy_wh_km", "fuel_consumption_l_100km", "electric_range_km", "co2_wltp", "displacement_cc"):
        assert units[key]["unit"] == "unknown" and units[key]["reason"] == "unit_unstated", key
    assert built["unit_unknown_distribution"]["energy_wh_km"] == {"n": 5, "p5": 170.0, "p50": 180.0, "p95": 180.0}
    row = built["rows"][0]
    assert (row["make"], row["model"], row["power_kw"], row["gear_count"]) == ("KIA", "EV6", "239", "1")
    assert (row["mass_running_order_kg"], row["mass_running_order_kg_max"]) == ("2090", "2180")
    assert row["energy_wh_km"] == "180" and row["electric_range_km_max"] == "528"     # raw: unit unknown
    assert built["absent_columns"] == []


def test_ademe_scales_a_stated_kwh_per_100km_and_falls_back_to_the_libelle():
    header = [c for c in H.ADEME_ORIGINAL_NAMES if c != "Modèle"]
    rows = [[v for c, v in zip(H.ADEME_ORIGINAL_NAMES, ADEME_ROW) if c != "Modèle"]]
    rows[0][header.index("Conso elec Min")] = 18
    rows[0][header.index("Conso elec Max")] = 19.5
    schema = _ademe_schema(**{"Conso elec Min": "Consommation électrique (kWh/100 km)"})
    built = build_ademe(_ademe_fetch(schema, H.csv_text(header, rows)))
    assert built["rows"][0]["model"] == "EV6" and built["rows"][0]["energy_wh_km"] == 180.0
    assert built["rows"][0]["energy_wh_km_max"] == 195.0 and built["column_units"]["energy_wh_km"]["unit"] == "kWh/100km"


def test_ademe_power_without_a_stated_unit_is_kept_raw_and_never_a_match_key(snapshots):
    body = H.csv_text(H.ADEME_ORIGINAL_NAMES, _ademe_rows())
    schema = _ademe_schema(**{"Puissance maximale": "Puissance maximale (ch)"})
    built = build_dataset("ademe_car_labelling", fetch=_ademe_fetch(schema, body))
    assert built["status"] == "built" and built["column_units"]["power_kw"]["unit"] == "unknown"
    row = ds.query_rows("ademe_car_labelling", makes=["KIA"])[0]
    assert "power_kw" not in row and "energy_wh_km" not in row and row["mass_running_order_kg"] == "2090"


def test_a_unit_in_the_description_yields_offers_and_one_without_yields_none_until_an_override(snapshots,
                                                                                               monkeypatch):
    from src.open_data.offers import field_offers

    body = H.csv_text(H.ADEME_ORIGINAL_NAMES, _ademe_rows())
    assert build_dataset("ademe_car_labelling", fetch=_ademe_fetch(_ademe_schema(), body))["status"] == "built"
    row = ds.query_rows("ademe_car_labelling", makes=["KIA"])[0]
    entries = [e for e in ds.config()["field_map"] if e["source"] == "ademe_car_labelling"
               and e["field"] in ("curb_weight_kg", "energy_consumption_kwh_100km")]

    def offers(survivor):
        result = {"route": "european", "level": "exact_technical_variant",
                  "sources": {"ademe_car_labelling": {"status": "unique", "survivors": [survivor],
                                                      "configurations": ["EV6"]}}}
        return {o["field"]: o for o in field_offers(result, entries)}
    first = offers(row)
    assert first["curb_weight_kg"]["value"] == 2090 and first["curb_weight_kg"]["range"] == [2090, 2180]
    assert "energy_consumption_kwh_100km" not in first                       # unit unknown: no offer
    cfg = json.loads(json.dumps(ds.config()))
    cfg["datasets"]["ademe_car_labelling"]["unit_overrides"]["energy_wh_km"] = {
        "unit": "Wh/km", "scale": 1, "evidence": "probe p5 / p50 / p95"}
    monkeypatch.setattr(ds, "config", lambda path=None: cfg)
    confirmed = ds.query_rows("ademe_car_labelling", makes=["KIA"])[0]
    assert confirmed["energy_wh_km"] == 180.0 and confirmed["energy_wh_km_max"] == 195.0
    assert offers(confirmed)["energy_consumption_kwh_100km"]["range"] == [18, 19.5]


def test_a_decimal_comma_is_never_guessed(monkeypatch):
    header = H.ADEME_ORIGINAL_NAMES
    rows = _ademe_rows({"Conso vitesse mixte Min": "5,6", "Conso vitesse mixte Max": "6,1"})
    config = ds.config()
    undeclared = {**config, "datasets": {**config["datasets"], "ademe_car_labelling": {
        k: v for k, v in config["datasets"]["ademe_car_labelling"].items() if k != "decimal_comma"}}}
    monkeypatch.setattr(ds, "config", lambda path=None: undeclared)
    built = build_ademe(_ademe_fetch(_ademe_schema(), H.csv_text(header, rows, delimiter=";")))
    assert built["rows"][0]["fuel_consumption_l_100km"] is None
    assert built["files"][0]["decimal_comma_values"] == {"fuel_consumption_l_100km": 1,
                                                         "fuel_consumption_l_100km_max": 1}


def test_ademe_declares_its_decimal_comma_and_reads_it():
    """M4: the Action's report (3,470 power values with a decimal comma) is the evidence for the declaration."""
    assert _cfg("ademe_car_labelling")["decimal_comma"] is True
    header = H.ADEME_ORIGINAL_NAMES
    rows = _ademe_rows({"Conso vitesse mixte Min": "5,6", "Conso vitesse mixte Max": "6,1"})
    built = build_ademe(_ademe_fetch(_ademe_schema(), H.csv_text(header, rows, delimiter=";")))
    assert built["rows"][0]["fuel_consumption_l_100km"] == "5.6" and "decimal_comma_values" not in built["files"][0]


def test_unit_scales_records_a_column_missing_from_the_schema_as_unknown():
    scales, units = unit_scales(_cfg("ademe_car_labelling")["columns"], {"power_kw": "Puissance maximale"}, [])
    assert scales == {} and units["power_kw"] == {"unit": "unknown", "reason": "not_in_schema",
                                                  "column": "Puissance maximale"}


def test_epa_reads_the_luggage_and_passenger_columns_and_absent_optional_ones_are_recorded(snapshots):
    header = [c for c in H.EPA_HEADER if c not in ("rangeA", "pv2")]
    row = {"id": 38704, "year": 2018, "make": "Cadillac", "model": "CTS", "baseModel": "CTS", "displ": "2.0",
           "cylinders": 4, "trany": "Automatic (S8)", "drive": "Rear-Wheel Drive", "VClass": "Midsize Cars",
           "fuelType": "Premium Gasoline", "comb08": 25, "lv4": 14, "pv4": 98}
    body = H.csv_text(header, [[row.get(c) for c in header]])
    built = build_dataset("epa_fueleconomy", fetch=lambda url: body)
    assert built["status"] == "built" and built["rows"] == 1 and built["size_bytes"] > 0
    assert built["absent_columns"] == ["ev_range_alt_mi", "passenger_2door_ft3"]
    stored = ds.query_rows("epa_fueleconomy", makes=["CADILLAC"], years=[2018])[0]
    assert (stored["row_id"], stored["luggage_4door_ft3"], stored["passenger_4door_ft3"]) == ("38704", "14", "98")
    meta = ds.snapshot_meta("epa_fueleconomy")
    assert meta["absent_columns"] == ["ev_range_alt_mi", "passenger_2door_ft3"] and meta["size_bytes"] > 0
    assert "build_duration_s" in meta
    stopped = build_dataset("epa_fueleconomy", fetch=lambda url: b"id,year,make\n1,2018,Cadillac\n")
    assert stopped["status"] == "stopped" and stopped["reason"] == "schema_mismatch"
    assert ds.snapshot_meta("epa_fueleconomy")["rows"] == 1                  # the previous snapshot stays


NRCAN_BASE = "https://natural-resources.canada.ca/files/oee/"
NRCAN_FILES = {
    "my2015-2024-fuel-consumption-ratings.csv": ("conventional", 2018),
    "my2025-fuel-consumption-ratings.csv": ("conventional", 2025),
    "my2026-fuel-consumption-ratings.csv": ("conventional", 2026),
    "my1995-2014-fuel-consumption-ratings-5-cycle.csv": ("broken", 2010),
    "original-my1995-2014-fuel-consumption-ratings-2-cycle.csv": ("never", 2010),
    "my2012-2026-battery-electric-vehicles.csv": ("bev", 2024),
    "my2012-2026-plug-in-hybrid-electric-vehicles.csv": ("phev", 2024),
}


def _nrcan_fetch(fetched):
    resources = [{"url": NRCAN_BASE + name, "format": "CSV", "language": ["en"], "name": name}
                 for name in NRCAN_FILES] + [{"url": NRCAN_BASE + "fr.csv", "format": "CSV", "language": ["fr"]}]

    def fetch(url):
        fetched.append(url)
        if "package_show" in url:
            return json.dumps({"result": {"resources": resources}}).encode()
        kind, year = NRCAN_FILES[url.rsplit("/", 1)[-1]]
        if kind == "conventional":
            return H.csv_text(H.NRCAN_CONVENTIONAL_HEADER,
                              [[year, "CADILLAC", "CTS", "Mid-size", "2.0", 4, "AS8", "Z", "9.5"]])
        if kind == "broken":
            return H.csv_text(["MODEL YEAR", "MAKE"], [[year, "CADILLAC"]])
        if kind == "bev":                  # M5: the recorded live header
            row = {"Model year": year, "Make": "KIA", "Model": "EV6 AWD", "Vehicle class": "SUV: Small", "Motor (kW)": 239,
                   "Transmission": "A1", "Fuel type": "B", "Combined (kWh/100 km)": "20.1", "Range (km)": 499}
            return H.csv_text(H.NRCAN_BEV_LIVE_HEADER, [[row.get(c) for c in H.NRCAN_BEV_LIVE_HEADER]])
        if kind == "phev":
            row = {"Model year": year, "Make": "BMW", "Model": "530e xDrive", "Vehicle class": "Mid-size",
                   "Motor (kW)": 80, "Engine size (L)": "2.0", "Cylinders": 4, "Transmission": "A8",
                   "Fuel type 1": "B", "Fuel type 2": "Z", "Range 1 (km)": 32, "Combined (L/100 km)": "8.4"}
            return H.csv_text(H.NRCAN_PHEV_LIVE_HEADER, [[row.get(c) for c in H.NRCAN_PHEV_LIVE_HEADER]])
        raise AssertionError(f"{url} must never be read")
    return fetch


def test_nrcan_builds_per_group_and_a_file_that_fails_stops_only_itself():
    fetched: list[str] = []
    built = build_ckan_groups("nrcan_fuel_ratings", _nrcan_fetch(fetched))
    by_file = {f["url"].rsplit("/", 1)[-1]: f for f in built["files"]}
    assert not any("2-cycle" in url for url in fetched) and not any(url.endswith("fr.csv") for url in fetched)
    assert by_file["original-my1995-2014-fuel-consumption-ratings-2-cycle.csv"]["status"] == "excluded"
    for name in ("my2015-2024-fuel-consumption-ratings.csv", "my2025-fuel-consumption-ratings.csv",
                 "my2026-fuel-consumption-ratings.csv"):
        assert by_file[name]["status"] == "built" and by_file[name]["group"] == "conventional"
    five_cycle = by_file["my1995-2014-fuel-consumption-ratings-5-cycle.csv"]
    assert five_cycle["status"] == "stopped" and five_cycle["group"] == "conventional"
    assert {m["key"] for m in five_cycle["missing"]} == {"model", "displacement_l", "transmission"}
    bev = by_file["my2012-2026-battery-electric-vehicles.csv"]
    phev = by_file["my2012-2026-plug-in-hybrid-electric-vehicles.csv"]
    assert bev["status"] == "built" and bev["group"] == "bev" and bev["absent_columns"] == []   # M5: the live header
    assert phev["status"] == "built" and phev["group"] == "phev"
    assert len(built["rows"]) == 5 and {r["group"] for r in built["rows"]} == {"conventional", "bev", "phev"}
    assert sorted(r["year"] for r in built["rows"]) == ["2018", "2024", "2024", "2025", "2026"]
    ev = next(r for r in built["rows"] if r["group"] == "bev")
    assert (ev["fuel_mode"], ev["motor_kw"], ev["electric_range_km"], ev["energy_kwh_100km"]) == ("E", "239", "499", "20.1")
    ph = next(r for r in built["rows"] if r["group"] == "phev")
    assert (ph["fuel_mode"], ph["fuel"], ph["displacement_l"], ph["electric_range_km"]) == ("P", "Z", "2.0", "32")


def test_an_nrcan_group_without_a_map_still_reports_its_live_header(monkeypatch):
    config = ds.config()
    groups = [{**g, "columns": None} if g["group"] == "bev" else g
              for g in config["datasets"]["nrcan_fuel_ratings"]["resource_groups"]]
    patched = {**config, "datasets": {**config["datasets"], "nrcan_fuel_ratings": {
        **config["datasets"]["nrcan_fuel_ratings"], "resource_groups": groups}}}
    monkeypatch.setattr(ds, "config", lambda path=None: patched)
    built = build_ckan_groups("nrcan_fuel_ratings", _nrcan_fetch([]))
    bev = next(f for f in built["files"] if f["url"].endswith("battery-electric-vehicles.csv"))
    assert bev["status"] == "map_pending" and bev["live_header"] == H.NRCAN_BEV_LIVE_HEADER   # no map from memory


CVS_RESOURCES = [{"url": f"https://open.canada.ca/data/dataset/913f/resource/{n}/download/{y}_en.csv", "format": "CSV"}
                 for n, y in ((1, 2018), (2, 2023))] + [
    {"url": "https://open.canada.ca/data/dataset/913f/resource/3/download/2023_fr.csv", "format": "CSV"},
    {"url": "https://open.canada.ca/data/dataset/913f/resource/9/download/"
            "cvs_canadian_vehicle_specifications_data_dictionary.xls", "format": "XLS"}]


def _cvs_fetch(fetched):
    def fetch(url):
        fetched.append(url)
        if "package_show" in url:
            return json.dumps({"result": {"resources": CVS_RESOURCES}}).encode()
        if url.endswith(".xls"):
            return b"xls"
        year = int(url.rsplit("/", 1)[-1][:4])
        return H.csv_text(H.CVS_HEADER, [[year, "CADILLAC", "CTS", 497, 183, 145, 291, 1650]])
    return fetch


def test_cvs_reads_its_files_from_the_package_and_checks_the_dictionary(monkeypatch):
    from src.open_data import build

    monkeypatch.setattr(build, "_xls_rows", lambda body: H.CVS_DICTIONARY_ROWS)
    fetched: list[str] = []
    built = build_ckan_files("tc_cvs", _cvs_fetch(fetched))
    assert built["file_years"] == [2018, 2023] and built["last_file_year"] == 2023
    assert not any(url.endswith("_fr.csv") for url in fetched)
    assert [r["wheelbase_cm"] for r in built["rows"]] == ["291", "291"]
    # H4: the live dictionary states no unit for the codes: the unit_overrides (CTS 2018 evidence) confirm them
    unitless = [[row[0], row[1].split(" (")[0]] for row in H.CVS_DICTIONARY_ROWS]
    monkeypatch.setattr(build, "_xls_rows", lambda body: unitless)
    assert len(build_ckan_files("tc_cvs", _cvs_fetch([]))["rows"]) == 2
    cfg = json.loads(json.dumps(ds.config()))
    del cfg["datasets"]["tc_cvs"]["unit_overrides"]["wheelbase_cm"]          # a code without an override still stops
    monkeypatch.setattr(ds, "config", lambda path=None: cfg)
    with pytest.raises(BuildStopped) as stop:
        build_ckan_files("tc_cvs", _cvs_fetch([]))
    assert stop.value.reason == "dictionary_mismatch"
    assert stop.value.report["problems"] == [{"code": "WB", "reason": "unit_not_stated", "expected_unit": "cm",
                                              "dictionary_row": ["WB", "Wheelbase"], "override": None}]
    assert ["WB", "Wheelbase"] in stop.value.report["dictionary_rows"]


def test_the_dictionary_check_names_a_code_the_dictionary_lacks():
    rows = [r for r in H.CVS_DICTIONARY_ROWS if r[0] != "CW"]
    assert dictionary_check(rows, _cfg("tc_cvs")["dictionary_check"]) == [{"code": "CW",
                                                                           "reason": "not_in_dictionary"}]


# --- the ADEME Min / Max range rule, the new gear_count offer -------------------------------------------------------------

def test_min_max_identifies_a_value_only_with_one_configuration():
    from src.open_data.offers import field_offers

    entry = next(e for e in ds.config()["field_map"] if e["source"] == "ademe_car_labelling"
                 and e["field"] == "curb_weight_kg")
    row = {"row_id": "a1", "mass_running_order_kg": "2090", "mass_running_order_kg_max": "2180"}

    def result(status, survivors):
        return {"route": "european", "level": "exact_technical_variant",
                "sources": {"ademe_car_labelling": {"status": status, "survivors": survivors,
                                                    "configurations": ["A", "B"][:len(survivors)]}}}
    one = field_offers(result("unique", [row]), [entry])[0]
    assert one["status"] == "offered" and one["value"] == 2090 and one["range"] == [2090, 2180]
    two = field_offers(result("ambiguous", [row, {**row, "row_id": "a2"}]), [entry])[0]
    assert two["status"] == "range" and "value" not in two and two["range"] == [2090, 2180]
    same = field_offers(result("ambiguous", [{**row, "mass_running_order_kg_max": "2090"}]), [entry])[0]
    assert "range" not in same and same["value"] == 2090
    gears = next(e for e in ds.config()["field_map"] if e["source"] == "ademe_car_labelling"
                 and e["field"] == "gear_count")
    assert field_offers(result("unique", [{"row_id": "a1", "gear_count": "8"}]), [gears])[0]["value"] == 8


# --- F3: run start --------------------------------------------------------------------------------------

def test_a_run_start_records_the_datasets_without_a_snapshot_and_the_run_view_says_so(snapshots):
    from src.open_data.engine import missing_snapshots
    from src.presentation.run_views import identity_anchors_view

    ds.write_snapshot("eea_co2_cars", [], {"built_at": "now"})
    assert missing_snapshots() == ["ademe_car_labelling", "epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs"]
    view = identity_anchors_view([{"kind": "run_started", "open_data_no_snapshot": ["tc_cvs"]}])
    assert view == {"open_data_not_built": ["tc_cvs"]}
    assert identity_anchors_view([{"kind": "run_started", "open_data_no_snapshot": []}]) is None


def test_build_dataset_reports_a_stopped_build_with_its_report(snapshots):
    built = build_dataset("tc_cvs", fetch=lambda url: json.dumps({"result": {"resources": []}}).encode())
    assert built["status"] == "stopped" and built["reason"] == "no_dictionary"

"""Benchmark v1 sample, Level 1.5 loading and observational metrics."""

import json
from collections import Counter

import pytest

from src import db
from src.benchmark import aggregate, benchmark_vehicles, manufacturers, select_vehicles
from src.db import Level15Error, build_level15_payload, decode_equipment, load_level15

# Appendix D order of the spec.
SPEC_IDS = ['38626', '36593', '37114', '713', '43528', '43136', '36670', '84619', '13628', '94995', '15400',
            '15376', '13362', '29465', '12949', '24385', '23378', '97877', '67081', '23678', '22278', '20835',
            '54024', '53615', '83645', '53779', '4330', '8878', '48743', '41222', '41314', '29053', '41255',
            '86055', '2914', '28966', '85484', '85439', '39241', '85410', '9606', '85167', '85095', '101122',
            '101136', '101099', '101138', '101101', '101148', '101149']


def test_fixed_sample_matches_spec():
    vehicles = benchmark_vehicles()
    assert [v["upstream_record_id"] for v in vehicles] == SPEC_IDS
    assert [v["ordinal"] for v in vehicles] == list(range(1, 51))
    counts = Counter(v["manufacturer"] for v in vehicles)
    assert counts == {"טויוטה": 8, "אאודי": 7, "ב מ וו": 7, "מרצדס": 7, "יונדאי": 7, "קאדילאק": 7, "אקספנג": 7}
    years = [v["year"] for v in vehicles]
    assert min(years) == 2017 and max(years) == 2026
    assert {v["propulsion"] for v in vehicles} == {"conventional", "hybrid", "plug_in", "battery_electric"}
    assert {v["segment"] for v in vehicles} == {"private", "commercial"}


def test_selection_modes_preserve_order():
    vehicles = benchmark_vehicles()
    assert len(select_vehicles(vehicles, "all")) == 50
    xpeng = select_vehicles(vehicles, "manufacturer", "אקספנג")
    assert [v["upstream_record_id"] for v in xpeng] == SPEC_IDS[43:]
    assert select_vehicles(vehicles, "one", "713")[0]["model"] == "HILUX"
    assert manufacturers(vehicles)[0] == "טויוטה"
    with pytest.raises(ValueError):
        select_vehicles(vehicles, "random")


def test_snapshot_loader_returns_requested_order(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    result = load_level15(["101149", "38626", "nope"])
    assert result.source == "snapshot"
    assert [r["upstream_record_id"] for r in result.rows] == ["101149", "38626"]
    assert result.missing == ["nope"]
    full = load_level15(SPEC_IDS, source="snapshot")
    assert [r["upstream_record_id"] for r in full.rows] == SPEC_IDS


def test_equipment_decoder_matches_database_function():
    rows = load_level15(SPEC_IDS, source="snapshot", dsn="").rows
    for row in rows:
        assert row["equipment_stated"] == 524287
        decoded = decode_equipment(row["equipment_stated"], row["equipment_on"], row["equipment_sources"])
        assert decoded == row["equipment"], row["upstream_record_id"]


def test_payload_contains_full_level15():
    row = load_level15(["36593"], source="snapshot", dsn="").rows[0]
    payload = build_level15_payload(row)
    assert payload["identity"]["commercial_name"] == "RAV 4"
    assert payload["identity"]["model_code"] == "AXAP54L-ANXMBW"
    assert payload["engine_drivetrain"]["power_hp"] == 306
    assert payload["structure"]["towing_braked_kg"] == 1500
    assert payload["environment"]["co2_wltp"] == 26
    assert len(payload["safety"]["assist_systems"]) == 19
    assert len(payload["safety"]["installation_sources"]) == 5
    assert payload["raw_row"] is row  # every column is passed through untouched
    json.dumps(payload, ensure_ascii=False)


def test_database_path_runs_appendix_d_query(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            captured["sql"], captured["params"] = sql, params

        def fetchall(self):
            from decimal import Decimal

            return [{"upstream_record_id": "713", "co2_wltp": Decimal("254"), "nox_wltp": Decimal("46.6")}]

    class Conn:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            captured["read_only"] = self.read_only
            return Cursor()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: Conn())
    result = load_level15(["713", "38626"], source="auto", dsn="postgresql://ro@example/db")
    assert result.source == "database" and result.missing == ["38626"]
    assert result.rows == [{"upstream_record_id": "713", "co2_wltp": 254, "nox_wltp": 46.6}]
    assert "public.catalog_variants_current" in captured["sql"]
    assert "public.catalog_variant_equipment" in captured["sql"]
    assert "array_position" in captured["sql"]
    assert captured["params"] == {"ids": ["713", "38626"]}
    assert captured["read_only"] is True


def test_configured_database_failure_is_not_masked(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(db, "load_from_database", boom)
    with pytest.raises(Level15Error):
        load_level15(["713"], source="auto", dsn="postgresql://x")
    with pytest.raises(Level15Error):
        load_level15(["713"], source="database", dsn="")


def test_aggregate_metrics():
    base = {"status": "completed", "coverage_pct": 50.0, "cost_usd": None}
    metrics = [{**base, "target_filled": 4, "document_cache_hits": 1, "document_cache_misses": 3},
               {**base, "target_filled": 6, "document_cache_hits": 3, "document_cache_misses": 1, "coverage_pct": 70.0}]
    agg = aggregate(metrics)
    assert agg["vehicles"] == 2 and agg["target_filled_total"] == 10 and agg["target_filled_mean"] == 5
    assert agg["coverage_pct_mean"] == 60.0 and agg["document_cache_hit_rate_pct"] == 50.0
    assert agg["cost_usd_total"] is None
    assert aggregate([]) == {"vehicles": 0}

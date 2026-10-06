"""Open-data snapshots built by a GitHub Action into the repository (G0-G4): the data volume is freed and guarded, the
engine reads committed data/open/<dataset>.sqlite.gz verified by the manifest's sha256, the EEA compaction keeps one row
per configuration, the Action builds / compresses / reports in the right order. No network.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import datasets as ds
from src.storage import disk

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _script(name: str):
    import importlib.util
    import sys

    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- G0: the volume -------------------------------------------------------------------------------------------------------

def _sqlite(path: Path, built_at: str | None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    if built_at:
        conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (json.dumps(built_at),))
    conn.commit()
    conn.close()


def test_startup_cleanup_removes_partial_and_unbuilt_files_and_keeps_built_ones(tmp_path):
    folder = tmp_path / "derived" / "open"
    folder.mkdir(parents=True)
    _sqlite(folder / "epa_fueleconomy.sqlite", "2026-10-06T09:00:00+00:00")
    _sqlite(folder / "eea_co2_cars.sqlite", None)                         # disk full before its meta
    (folder / ".eea_co2_cars.sqlite.tmp").write_bytes(b"x" * 4096)
    (folder / "nrcan_fuel_ratings.sqlite-journal").write_bytes(b"y" * 100)
    (folder / "garbage.sqlite").write_bytes(b"not a database")
    out = disk.cleanup_open_data(folder)
    assert sorted(out["removed"]) == [".eea_co2_cars.sqlite.tmp", "eea_co2_cars.sqlite", "garbage.sqlite",
                                      "nrcan_fuel_ratings.sqlite-journal"]
    assert out["bytes_freed"] >= 4196
    assert sorted(p.name for p in folder.iterdir()) == ["epa_fueleconomy.sqlite"]
    assert disk.cleanup_open_data(tmp_path / "missing") == {"removed": [], "bytes_freed": 0}


def test_the_disk_guard_refuses_below_200_mb(tmp_path):
    disk.check_free(tmp_path, "test", free=disk.MIN_FREE_BYTES)               # at the threshold: allowed
    with pytest.raises(disk.InsufficientDisk) as refused:
        disk.check_free(tmp_path, "run start", free=150 * 1024 * 1024)
    assert refused.value.code == "insufficient_disk" and "150 MB free" in str(refused.value)


def test_the_trim_index_build_is_refused_without_space(tmp_path, monkeypatch):
    from src.jobs.derived_index import DerivedIndexJob

    monkeypatch.setattr(disk, "free_bytes", lambda path: 10 * 1024 * 1024)
    job = DerivedIndexJob(tmp_path, rows=lambda: [])
    assert job.build()["status"] == "insufficient_disk"
    assert not job.index_path.exists()


def test_the_storage_panel_and_the_delete_button(client, ctx):
    folder = ctx.paths.data_dir / "derived" / "open"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "eea_co2_cars.sqlite").write_bytes(b"z" * 10_000)
    storage = client.get("/api/data/storage").json()
    assert storage["volume"]["total"] > 0 and storage["min_free_bytes"] == disk.MIN_FREE_BYTES
    names = {f["name"]: f["bytes"] for f in storage["folders"]}
    assert {"runs/", "document cache", "logs/", "derived/"} <= set(names) and names["derived/"] >= 10_000
    refused = client.post("/api/data/storage/delete-open-data", json={})
    assert refused.status_code == 422 and folder.exists()
    deleted = client.post("/api/data/storage/delete-open-data", json={"confirm": True}).json()
    assert deleted["deleted"] and deleted["bytes_freed"] >= 10_000 and not folder.exists()
    assert (ctx.paths.data_dir / "runs").exists()                          # only derived/open


def test_a_run_is_refused_when_the_volume_is_almost_full(client, ctx, monkeypatch):
    monkeypatch.setattr(disk, "free_bytes", lambda path: 50 * 1024 * 1024)
    response = client.post("/api/runs", json={"scope": "one", "record_id": "101122"})
    assert response.status_code == 507 and response.json()["error"]["code"] == "insufficient_disk"
    assert not ctx.manager.active_runs()


def test_the_server_no_longer_builds_open_data(ctx):
    assert not hasattr(ctx.manager, "open_data")
    assert not (ROOT / "src" / "open_data" / "job.py").exists()


# --- G3: the engine reads the committed snapshots -------------------------------------------------------------------------

def _committed(tmp_path, rows: list[dict], *, tamper: bool = False) -> Path:
    """A data/open/ with one built + gzipped EEA snapshot and its manifest (as scripts/build_open_data.py writes it)."""
    work, repo = tmp_path / "work", tmp_path / "repo"
    work.mkdir()
    repo.mkdir()
    ds.write_snapshot("eea_co2_cars", rows, {"built_at": "2026-10-06T10:00:00+00:00"}, folder=work)
    build = _script("build_open_data")
    build.gzip_file(work / "eea_co2_cars.sqlite", repo / "eea_co2_cars.sqlite.gz")
    sha = ds.sha256_file(repo / "eea_co2_cars.sqlite.gz")
    (repo / "manifest.json").write_text(json.dumps({"datasets": {"eea_co2_cars": {
        "file": "eea_co2_cars.sqlite.gz", "sha256": ("0" * 64) if tamper else sha, "rows": len(rows)}}}), "utf-8")
    return repo


@pytest.fixture()
def repo_snapshot(tmp_path):
    yield tmp_path
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def test_the_engine_reads_a_committed_gzip_verified_by_its_sha256(repo_snapshot):
    from src.db import build_level15_payload
    from src.gov_registry import identity_fingerprint
    from src.open_data.match import match

    from fixtures import pr48_open_data as F

    repo = _committed(repo_snapshot, F.CTS_EEA)
    ds.set_repo_dir(repo, repo_snapshot / "tmp")
    assert ds.available("eea_co2_cars") and ds.materialize("eea_co2_cars")[1] is None
    decompressed = ds.materialize("eea_co2_cars")[0]
    assert decompressed.parent == repo_snapshot / "tmp"                  # the temp dir, never the volume
    snapshot = {r["upstream_record_id"]: r for r in
                json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]}
    payload = build_level15_payload(snapshot["85095"])
    result = match(identity_fingerprint(payload), payload)
    assert result["sources"]["eea_co2_cars"]["status"] == "ambiguous"
    assert result["sources"]["epa_fueleconomy"]["status"] == "no_snapshot"   # no manifest entry
    status = {d["dataset"]: d for d in ds.status()["datasets"]}
    assert status["eea_co2_cars"]["available"] and status["tc_cvs"]["problem"] == "no_manifest_entry"


def test_a_sha256_mismatch_is_no_snapshot(repo_snapshot):
    from fixtures import pr48_open_data as F
    from src.open_data.match import match_source

    repo = _committed(repo_snapshot, F.CTS_EEA, tamper=True)
    ds.set_repo_dir(repo, repo_snapshot / "tmp")
    assert ds.materialize("eea_co2_cars") == (None, "sha256_mismatch")
    assert not ds.available("eea_co2_cars")
    keys = {"makes": ["CADILLAC"], "models": ["CTS"], "year": 2018}
    assert match_source("eea_co2_cars", keys)["status"] == "no_snapshot"
    assert not (repo_snapshot / "tmp").exists() or not any((repo_snapshot / "tmp").iterdir())


def test_a_missing_manifest_is_no_snapshot_for_every_dataset(repo_snapshot):
    from src.open_data.engine import missing_snapshots

    ds.set_repo_dir(repo_snapshot / "empty", repo_snapshot / "tmp")
    assert missing_snapshots() == ["ademe_car_labelling", "eea_co2_cars", "epa_fueleconomy", "nrcan_fuel_ratings",
                                   "tc_cvs"]


# --- G2: compaction -------------------------------------------------------------------------------------------------------

def test_group_configurations_keeps_one_row_per_configuration_with_min_max_median():
    """The generic Python grouping (EEA is grouped server-side since H2; compact() passes its rows through)."""
    from src.open_data.build import compact, group_configurations

    base = {"make": "BMW", "model": "530E", "type_approval": "e1", "variant": "JA", "version": "51EA", "fuel": "petrol/electric",
            "fuel_mode": "P", "displacement_cc": 1998, "power_kw": 135, "co2_wltp": 47, "year": 2020, "status": "F",
            "co2_nedc": 42}
    rows = [{**base, "mass_running_order_kg": 1945, "wheelbase_mm": 2975, "electric_range_km": 57, "registrations": 3},
            {**base, "mass_running_order_kg": 1990, "wheelbase_mm": 2975, "electric_range_km": 54, "registrations": 1},
            {**base, "mass_running_order_kg": 2010, "wheelbase_mm": 2975, "electric_range_km": 52, "registrations": 1},
            {**base, "co2_wltp": 51, "mass_running_order_kg": 2080, "wheelbase_mm": 2975, "registrations": 2}]
    keys = ["make", "model", "type_approval", "variant", "version", "fuel", "fuel_mode", "displacement_cc", "power_kw",
            "co2_wltp", "year"]
    out = group_configurations(rows, keys, ["mass_running_order_kg", "wheelbase_mm", "electric_range_km"],
                               "registrations", ["status"])
    assert len(out) == 2
    first = next(r for r in out if r["co2_wltp"] == 47)
    assert (first["mass_running_order_kg"], first["mass_running_order_kg_min"], first["mass_running_order_kg_max"]) \
        == (1945.0, 1945.0, 2010.0)                                          # weighted median: 3 of 5 registrations
    assert (first["electric_range_km_min"], first["electric_range_km_max"]) == (52.0, 57.0)
    assert first["registrations"] == 5.0 and first["status"] == "F" and "co2_nedc" not in first
    second = next(r for r in out if r["co2_wltp"] == 51)
    assert "electric_range_km" not in second                                 # not stated: not invented
    passed = compact("eea_co2_cars", {"rows": rows})                         # server-grouped: no Python regrouping
    assert passed["compaction"]["rows_after"] == 4


def test_compaction_filters_the_years_and_the_vocabulary_makes():
    from src.open_data.build import compact

    rows = [{"make": "Cadillac", "model": "CTS", "year": "2018"}, {"make": "Cadillac", "model": "DeVille", "year": "1999"},
            {"make": "Studebaker", "model": "Lark", "year": "2018"}]
    assert [r["model"] for r in compact("epa_fueleconomy", {"rows": rows})["rows"]] == ["CTS"]
    cvs = [{"make": "CADILLAC", "model": "CTS", "file_year": 2010}, {"make": "CADILLAC", "model": "CTS", "file_year": 2018},
           {"make": "CADILLAC", "model": "CTS", "file_year": 2024}]
    assert [r["file_year"] for r in compact("tc_cvs", {"rows": cvs})["rows"]] == [2018]


def test_eea_offers_a_value_only_when_min_equals_max():
    """H2: an EEA configuration's value is its MIN when MIN = MAX, else an AVG: never offered (range_policy
    equal_only), even with one configuration."""
    from src.open_data.offers import field_offers

    entry = next(e for e in ds.config()["field_map"] if e["source"] == "eea_co2_cars" and e["field"] == "curb_weight_kg")
    assert entry["range_policy"] == "equal_only"
    row = {"row_id": "eea-2020-1", "mass_running_order_kg": 1977.5, "mass_running_order_kg_min": 1945.0,
           "mass_running_order_kg_max": 2010.0}

    def offer(status, survivors):
        return field_offers({"route": "european", "level": "exact_technical_variant",
                             "sources": {"eea_co2_cars": {"status": status, "survivors": survivors,
                                                          "configurations": ["a", "b"][:len(survivors)]}}},
                            [entry])[0]
    one = offer("unique", [row])
    assert one["status"] == "range" and one["range"] == [1945, 2010] and "value" not in one
    equal = {**row, "mass_running_order_kg": 1945.0, "mass_running_order_kg_max": 1945.0}
    exact = offer("unique", [equal])
    assert exact["status"] == "offered" and exact["value"] == 1945 and "range" not in exact


# --- G1: the Action -------------------------------------------------------------------------------------------------------

def test_the_workflow_probes_builds_and_reports_coverage_before_the_pull_request():
    workflow = (ROOT / ".github" / "workflows" / "build-open-data.yml").read_text("utf-8")
    steps = workflow[workflow.index("steps:"):]                             # the steps, not the header comment
    assert steps.index("run: python scripts/probe_open_data.py") < steps.index("run: python scripts/build_open_data.py") \
        < steps.index("run: python scripts/open_data_coverage.py") < steps.index("uses: peter-evans/create-pull-request")
    assert "workflow_dispatch:" in workflow and "schedule:" in workflow and "datasets:" in workflow
    assert "branch: automation/open-data" in workflow and "--max-mb 50" in workflow
    assert "secrets." not in workflow
    coverage = steps[steps.index("run: python scripts/open_data_coverage.py") - 120:steps.index(
        "run: python scripts/open_data_coverage.py")]
    assert "if: always()" in coverage


def test_the_build_script_compresses_records_the_manifest_and_skips_a_failed_probe(tmp_path, monkeypatch):
    build = _script("build_open_data")
    from src.open_data import build as builder

    def fake_build(name, fetch=None):
        rows = [{"row_id": "1", "make": "CADILLAC", "model": "CTS", "year": 2018}]
        ds.write_snapshot(name, rows, {"built_at": "2026-10-06T10:00:00+00:00"})
        return {"status": "built", "rows": 1, "built_at": "2026-10-06T10:00:00+00:00", "absent_columns": ["x"],
                "files": [{"url": "u", "status": "built", "rows": 1}]}
    monkeypatch.setattr(build, "build_dataset", fake_build)
    out = tmp_path / "open"
    out.mkdir()
    (out / "manifest.json").write_text(json.dumps({"datasets": {"tc_cvs": {"file": "tc_cvs.sqlite.gz",
                                                                         "sha256": "abc", "rows": 7}}}), "utf-8")
    probe = {"datasets": {"epa_fueleconomy": {"status": "ok"},
                          "tc_cvs": {"status": "stopped", "reason": "dictionary_mismatch"}}}
    results, manifest, errors = build.run(["epa_fueleconomy", "tc_cvs"], out, tmp_path / "work", probe,
                                          50 * 1024 * 1024, "https://github.com/x/actions/runs/1")
    assert not errors and results["tc_cvs"]["status"] == "skipped"
    epa = manifest["datasets"]["epa_fueleconomy"]
    assert epa["file"] == "epa_fueleconomy.sqlite.gz" and epa["sha256"] == ds.sha256_file(out / epa["file"])
    assert epa["run_url"] == "https://github.com/x/actions/runs/1" and epa["absent_columns"] == ["x"]
    with gzip.open(out / epa["file"]) as handle:
        assert handle.read(16) == b"SQLite format 3\x00"
    cvs = manifest["datasets"]["tc_cvs"]
    assert cvs["sha256"] == "abc" and cvs["rows"] == 7                      # the previous entry stays
    assert cvs["last_attempt"]["status"] == "skipped" and "dictionary_mismatch" in cvs["last_attempt"]["reason"]
    assert json.loads((out / "manifest.json").read_text("utf-8")) == manifest
    first = (out / epa["file"]).read_bytes()
    build.run(["epa_fueleconomy"], out, tmp_path / "work2", {}, 50 * 1024 * 1024, None)
    assert (out / epa["file"]).read_bytes() == first                        # deterministic gzip
    _, _, errors = build.run(["epa_fueleconomy"], out, tmp_path / "work3", {}, 10, None)
    assert errors and "over the 0 MB limit" in errors[0]
    assert (out / epa["file"]).read_bytes() == first                        # a too-large file is never written


def test_the_coverage_step_reports_every_benchmark_record_and_22010(tmp_path, capsys):
    coverage = _script("open_data_coverage")
    ids = {r["upstream_record_id"] for r in coverage.records()}
    assert len(ids) == 51 and {"85095", "23678", "38626", "101136", "22010"} <= ids
    assert coverage.main(["--open", str(tmp_path / "empty"), "--body", str(tmp_path / "body.md")]) == 0
    body = (tmp_path / "body.md").read_text("utf-8")
    assert "| **22010** |" in body and "never built" in body
    ds.set_repo_dir(None)


def test_the_probe_reports_matched_and_missing_columns_per_file():
    from src.open_data.probe import markdown, probe_csv

    header = [c for c in H.EPA_HEADER if c not in ("trany", "rangeA")]
    body = H.csv_text(header, [[1] * len(header)])
    out = probe_csv("epa_fueleconomy", lambda url: body)
    assert out["status"] == "stopped" and out["files"][0]["missing_required"] == ["transmission"]
    assert out["files"][0]["missing_optional"] == ["ev_range_alt_mi"]
    text = markdown({"datasets": {"epa_fueleconomy": {"dataset": "epa_fueleconomy", **out}}})
    assert "missing required: transmission" in text


def test_mcp_open_data_status_returns_the_manifest_and_the_probe(repo_snapshot, monkeypatch):
    from src.mcp_server.tools import Observer
    from src.storage.paths import resolve_paths

    from fixtures import pr48_open_data as F

    repo = _committed(repo_snapshot, F.CTS_EEA)
    (repo / "probe.json").write_text(json.dumps({"version": "open-data-probe-v1", "datasets": {
        "tc_cvs": {"status": "stopped", "reason": "dictionary_mismatch"}}}), "utf-8")
    ds.set_repo_dir(repo, repo_snapshot / "tmp")
    monkeypatch.setenv("TRIPY_DATA_DIR", str(repo_snapshot / "data"))
    out = Observer(resolve_paths()).open_data_status()
    assert out["manifest"]["datasets"]["eea_co2_cars"]["rows"] == len(F.CTS_EEA)
    assert out["probe"]["datasets"]["tc_cvs"]["reason"] == "dictionary_mismatch"
    assert out["no_snapshot"] == ["ademe_car_labelling", "epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs"]


from test_api import client, ctx, data_root, gate  # noqa: E402,F401  (the API fixtures)

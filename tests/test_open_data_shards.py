"""EEA snapshot shards (S1-S3 of the shards PR): one file per year under the size limit, each verified by its own sha256
(a missing or corrupt shard is skipped and reported, the others load), a match reads only its year window, the same
rows give the same bytes, an oversized year splits by make initial, only what matching and offers read is stored
(typed), and one dataset never costs the others in the build step and the workflow. No network.
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
from pathlib import Path

import pytest

from fixtures import open_data_live_headers as H
from fixtures import pr48_open_data as F
from src.open_data import datasets as ds
from src.open_data.build import build_dataset

from test_open_data_builds import _eea_fetch
from test_open_data_repo import _script

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 50 * 1024 * 1024


@pytest.fixture()
def repo(tmp_path):
    yield tmp_path
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def _built_rows(year: int, n: int, makes=("BMW", "CADILLAC", "TOYOTA", "VOLKSWAGEN"), seed: int = 1) -> list[dict]:
    """EEA-shaped built rows (what build_eea returns: the dropped columns included, the builder drops them)."""
    rnd = random.Random(seed + year)
    rows = []
    for i in range(n):
        make = makes[i % len(makes)]
        rows.append({"row_id": f"eea-{year}-F-{i + 1}", "make": make, "model": f"{make[:3]} M{rnd.randint(1, 60)}",
                     "type_approval": f"e{rnd.randint(1, 9)}*2007/46*{rnd.randint(1000, 9999)}",
                     "variant": f"V{rnd.randint(1, 99)}", "version": f"{rnd.random():.8f}", "fuel": "petrol",
                     "fuel_mode": "M", "displacement_cc": rnd.choice([998, 1498, 1998, 2993]),
                     "power_kw": rnd.choice([55, 81.5, 110, 135, 250]), "co2_wltp": rnd.randint(90, 250),
                     "year": year, "mass_running_order_kg": 1400.0 + rnd.randint(0, 600),
                     "mass_running_order_kg_min": 1400.0, "mass_running_order_kg_max": 2100.0,
                     "wheelbase_mm": 2600 + rnd.randint(0, 400), "wheelbase_mm_min": 2600, "wheelbase_mm_max": 3000,
                     "track_front_mm": 1560, "track_front_mm_min": 1550, "track_front_mm_max": 1570,
                     "mass_wltp_test_kg": 1600, "registrations": rnd.randint(1, 900), "status": "F"})
    return rows


def _commit(tmp_path: Path, rows: list[dict], *, max_bytes: int = LIMIT, previous: dict | None = None,
            out: Path | None = None) -> tuple[Path, dict, list[dict]]:
    """data/open/ with the rows written as shards exactly as the build step writes them (scripts/build_open_data.py)."""
    build = _script("build_open_data")
    work = tmp_path / f"work-{random.random()}"
    out = out or tmp_path / "repo"
    shards = ds.write_shards("eea_co2_cars", rows, {"source_url": "https://discodata.eea.europa.eu/sql"},
                             status_used={y: "F" for y in {r["year"] for r in rows}}, folder=work)
    result = {"status": "built", "built_at": "2026-10-06T10:00:00+00:00", "rows": len(rows),
              "shards": [{**s, "path": str(s["path"])} for s in shards]}
    entry, items = build.write_shards("eea_co2_cars", result, out, work, previous or {}, max_bytes, None)
    (out / "manifest.json").write_text(json.dumps({"datasets": {"eea_co2_cars": entry}}), "utf-8")
    return out, entry, items


# --- S1: shards per year, listed in the manifest ---------------------------------------------------------------------------

def test_the_eea_build_writes_one_shard_per_year_listed_in_the_manifest(tmp_path, monkeypatch):
    build = _script("build_open_data")
    rows = {(2018, "F"): [H.eea_row(Year=2018), H.eea_row(Year=2018, Ve="X")],
            (2019, "P"): [H.eea_row(Year=2019, Status="P")]}
    fetch = _eea_fetch([(2018, "F"), (2019, "P")], rows, [])
    monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None: build_dataset(name, fetch))
    out = tmp_path / "open"
    results, manifest, warnings = build.run(["eea_co2_cars"], out, tmp_path / "work", {}, LIMIT, "https://run/1")
    assert not warnings and results["eea_co2_cars"]["written"] == 2
    entry = manifest["datasets"]["eea_co2_cars"]
    assert entry["sharded"] and entry["build_status"] == "built" and "file" not in entry and entry["rows"] == 3
    assert [(s["file"], s["year"], s["part"], s["status_used"], s["rows"]) for s in entry["shards"]] == [
        ("eea_co2_cars/2018.sqlite.gz", 2018, None, "F", 2), ("eea_co2_cars/2019.sqlite.gz", 2019, None, "P", 1)]
    for shard in entry["shards"]:
        path = out / shard["file"]
        assert shard["bytes"] == path.stat().st_size < LIMIT and shard["sha256"] == ds.sha256_file(path)
        with gzip.open(path) as handle:
            assert handle.read(16) == b"SQLite format 3\x00"
    assert not (out / "eea_co2_cars.sqlite.gz").exists()
    text = build.summary(results, manifest)
    assert "| 2018 | — | ok | F | 2 |" in text and "| 2019 | — | ok | P | 1 |" in text      # per year: rows and size
    assert "eea_co2_cars/ (2 shards)" in text


def test_a_shard_stores_only_what_matching_and_offers_read_typed(tmp_path):
    shard = ds.write_shards("eea_co2_cars", _built_rows(2020, 50), {}, folder=tmp_path)[0]["path"]
    with sqlite3.connect(shard) as conn:
        columns = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(rows)")}
        types = dict(conn.execute("SELECT 'cc', typeof(displacement_cc) FROM rows LIMIT 1").fetchall()
                     + conn.execute("SELECT 'kw', typeof(power_kw) FROM rows WHERE power_kw = 81.5 LIMIT 1").fetchall()
                     + conn.execute("SELECT 'wb', typeof(wheelbase_mm) FROM rows LIMIT 1").fetchall())
    for dropped in ("data", "type_approval", "registrations", "status", "track_front_mm", "track_front_mm_min",
                    "mass_wltp_test_kg", "co2_nedc"):
        assert dropped not in columns
    for key in ("wheelbase_mm", "mass_running_order_kg", "energy_wh_km", "fuel_consumption_l_100km",
                "electric_range_km"):
        assert columns[key] == columns[f"{key}_min"] == columns[f"{key}_max"] == "REAL"
    assert columns["displacement_cc"] == "INTEGER" and columns["power_kw"] == "REAL" and columns["make"] == "TEXT"
    assert types == {"cc": "integer", "kw": "real", "wb": "real"}                       # numbers, never text


def test_the_track_widths_are_read_by_neither_the_match_nor_an_offer():
    """S2: At1 / At2 (and Mt) are dropped because no code reads them."""
    for module in ("match.py", "offers.py", "engine.py"):
        text = (ROOT / "src" / "open_data" / module).read_text("utf-8")
        assert "track_" not in text and "mass_wltp_test" not in text
    assert not [e for e in ds.config()["field_map"] if "track" in e["column"] or "wltp_test" in e["column"]]
    eea = ds.datasets()["eea_co2_cars"]
    offered = {e["column"] for e in ds.config()["field_map"] if e["source"] == "eea_co2_cars" and e.get("max_column")}
    assert offered == set(eea["snapshot"]["range_measures"]) == set(eea["measure_keys"])


# --- S1: the engine reads the shards, each verified -----------------------------------------------------------------------

def test_the_engine_reads_every_shard_verified_by_its_sha256(repo):
    out, entry, _ = _commit(repo, _built_rows(2018, 20) + _built_rows(2019, 20) + _built_rows(2020, 20))
    ds.set_repo_dir(out, repo / "tmp")
    assert ds.available("eea_co2_cars") and ds.materialize("eea_co2_cars") == (None, "sharded")
    rows = ds.query_rows("eea_co2_cars", makes=["BMW"], years=[2018, 2019, 2020])
    assert {r["year"] for r in rows} == {2018, 2019, 2020} and all(r["make"] == "BMW" for r in rows)
    assert len(rows) == 15 and "registrations" not in rows[0] and isinstance(rows[0]["displacement_cc"], int)
    status = {d["dataset"]: d for d in ds.status()["datasets"]}["eea_co2_cars"]
    assert status["available"] and status["problem"] is None and len(status["shards"]) == 3
    meta = ds.snapshot_meta("eea_co2_cars")
    assert meta["built_at"] == "2026-10-06T10:00:00+00:00" and meta["rows"] == 60 and meta["sharded"]


@pytest.mark.parametrize("damage, problem", [("corrupt", "sha256_mismatch"), ("delete", "missing_file")])
def test_a_corrupt_or_missing_shard_is_skipped_and_reported_and_the_others_still_load(repo, damage, problem):
    from src.open_data.match import match_source

    out, entry, _ = _commit(repo, _built_rows(2018, 20) + _built_rows(2019, 20))
    broken = out / "eea_co2_cars" / "2018.sqlite.gz"
    if damage == "corrupt":
        broken.write_bytes(broken.read_bytes()[:-10] + b"0123456789")
    else:
        broken.unlink()
    ds.set_repo_dir(out, repo / "tmp")
    report: dict = {}
    rows = ds.query_rows("eea_co2_cars", makes=["BMW", "TOYOTA"], years=[2018, 2019], report=report)
    assert rows and {r["year"] for r in rows} == {2019}                              # 2019 still loads
    assert report["skipped_shards"] == [{"file": "eea_co2_cars/2018.sqlite.gz", "year": 2018, "part": None,
                                         "problem": problem}]
    assert ds.available("eea_co2_cars")
    status = {d["dataset"]: d for d in ds.status()["datasets"]}["eea_co2_cars"]
    assert status["available"] and status["skipped_shards"] == {"eea_co2_cars/2018.sqlite.gz": problem}
    keys = {"makes": ["BMW"], "models": ["XYZ"], "year": 2018}
    assert match_source("eea_co2_cars", keys)["skipped_shards"][0]["problem"] == problem
    assert not any("2018" in p.name for p in (repo / "tmp").iterdir())               # never decompressed


def test_every_shard_broken_is_no_snapshot(repo):
    out, _, _ = _commit(repo, _built_rows(2018, 5))
    (out / "eea_co2_cars" / "2018.sqlite.gz").write_bytes(b"not a gzip")
    ds.set_repo_dir(out, repo / "tmp")
    assert not ds.available("eea_co2_cars")
    status = {d["dataset"]: d for d in ds.status()["datasets"]}["eea_co2_cars"]
    assert status["problem"] == "no_shard_readable" and not status["available"]


def test_a_match_decompresses_only_the_shards_of_its_year_window(repo):
    """D-3: EEA rows of shnat_yitzur and +1 (the CTS of record 85095, 2018): two shards of five."""
    from src.db import build_level15_payload
    from src.gov_registry import identity_fingerprint
    from src.open_data.match import match

    rows = [dict(r) for r in F.CTS_EEA] + [{**r, "row_id": f"{r['row_id']}-{y}", "year": y} for y in (2016, 2017, 2021)
                                           for r in F.CTS_EEA[:1]]
    out, entry, _ = _commit(repo, rows)
    assert sorted(s["year"] for s in entry["shards"]) == [2016, 2017, 2018, 2019, 2021]
    ds.set_repo_dir(out, repo / "tmp")
    snapshot = {r["upstream_record_id"]: r for r in
                json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]}
    payload = build_level15_payload(snapshot["85095"])
    result = match(identity_fingerprint(payload), payload)
    assert result["keys"]["year"] == 2018
    assert result["sources"]["eea_co2_cars"]["status"] == "ambiguous"               # as with the single file
    decompressed = sorted(p.name.split("-")[1] for p in (repo / "tmp").iterdir())
    assert decompressed == ["2018", "2019"]


# --- S1: deterministic bytes ----------------------------------------------------------------------------------------------

def test_the_same_rows_give_byte_identical_shards(tmp_path):
    build = _script("build_open_data")
    rows = _built_rows(2019, 300)
    shuffled = list(rows)
    random.Random(7).shuffle(shuffled)                                    # DISCODATA answers without ORDER BY
    first = ds.write_shards("eea_co2_cars", rows, {"source_url": "u"}, status_used={2019: "F"},
                            folder=tmp_path / "a")[0]["path"]
    second = ds.write_shards("eea_co2_cars", shuffled, {"source_url": "u"}, status_used={2019: "F"},
                             folder=tmp_path / "b")[0]["path"]
    assert first.read_bytes() == second.read_bytes()
    build.gzip_file(first, tmp_path / "a" / "2019.sqlite.gz")
    build.gzip_file(second, tmp_path / "b" / "2019.sqlite.gz")
    assert (tmp_path / "a" / "2019.sqlite.gz").read_bytes() == (tmp_path / "b" / "2019.sqlite.gz").read_bytes()
    header = (tmp_path / "a" / "2019.sqlite.gz").read_bytes()[:30]
    assert header[4:8] == b"\x00\x00\x00\x00" and header[10:22] == b"2019.sqlite\x00"   # mtime 0, a fixed name
    with sqlite3.connect(first) as conn:                                   # the identity-key order
        order = [r[0] for r in conn.execute("SELECT make FROM rows ORDER BY rowid")]
    assert order == sorted(order)


def test_rebuilding_an_unchanged_year_gives_the_same_sha256(tmp_path, monkeypatch):
    """Two full builds (different built_at) of the same final year: the shard's bytes do not change."""
    build = _script("build_open_data")
    rows = {(2018, "F"): [H.eea_row(Year=2018), H.eea_row(Year=2018, Ve="X")]}
    fetch = _eea_fetch([(2018, "F")], rows, [])
    monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None: build_dataset(name, fetch))
    out = tmp_path / "open"
    _, first, _ = build.run(["eea_co2_cars"], out, tmp_path / "w1", {}, LIMIT, "https://run/1")
    before = (out / "eea_co2_cars" / "2018.sqlite.gz").read_bytes()
    monkeypatch.setattr("src.open_data.build.datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2027, 1, 1, tzinfo=tz))}))
    _, second, _ = build.run(["eea_co2_cars"], out, tmp_path / "w2", {}, LIMIT, "https://run/2")
    assert first["datasets"]["eea_co2_cars"]["built_at"] != second["datasets"]["eea_co2_cars"]["built_at"]
    assert (out / "eea_co2_cars" / "2018.sqlite.gz").read_bytes() == before
    assert first["datasets"]["eea_co2_cars"]["shards"] == second["datasets"]["eea_co2_cars"]["shards"]


# --- S1: the size guard ---------------------------------------------------------------------------------------------------

def test_an_oversized_year_splits_by_make_initial_and_a_match_reads_only_its_part(repo):
    rows = _built_rows(2021, 4000, makes=("AUDI", "BMW", "KIA", "MAZDA", "TOYOTA", "VOLVO"))
    whole, _, _ = _commit(repo / "probe", rows)
    size = (whole / "eea_co2_cars" / "2021.sqlite.gz").stat().st_size
    out, entry, items = _commit(repo, rows, max_bytes=size - 1)
    assert [(s["file"], s["year"], s["part"]) for s in entry["shards"]] == [
        ("eea_co2_cars/2021-A-L.sqlite.gz", 2021, "A-L"), ("eea_co2_cars/2021-M-Z.sqlite.gz", 2021, "M-Z")]
    assert all(i["status"] == "ok" and i["split_from_bytes"] == size and i["bytes"] < size for i in items)
    assert sum(s["rows"] for s in entry["shards"]) == 4000 and entry["build_status"] == "built"
    assert not (out / "eea_co2_cars" / "2021.sqlite.gz").exists()
    ds.set_repo_dir(out, repo / "tmp")
    rows_bmw = ds.query_rows("eea_co2_cars", makes=["BMW"], years=[2021])
    assert rows_bmw and {r["make"] for r in rows_bmw} == {"BMW"}
    decompressed = [p.name for p in (repo / "tmp").iterdir()]
    assert len(decompressed) == 1 and "-2021-A-L-" in decompressed[0]                 # only the A-L part decompressed
    assert {r["make"] for r in ds.query_rows("eea_co2_cars", makes=["TOYOTA", "AUDI"], years=[2021])} == \
        {"TOYOTA", "AUDI"}


def test_a_part_still_over_the_limit_is_too_large_and_the_previous_year_stays(repo):
    out, previous, _ = _commit(repo, _built_rows(2018, 5) + _built_rows(2019, 30))
    old = {s["year"]: s["sha256"] for s in previous["shards"]}
    out, entry, items = _commit(repo, _built_rows(2018, 5, seed=9) + _built_rows(2019, 400, seed=5), max_bytes=4000,
                                previous=previous, out=out)
    assert [(i["year"], i["part"], i["status"]) for i in items] == [(2018, None, "ok"), (2019, "A-L", "too_large"),
                                                                    (2019, "M-Z", "too_large")]
    shards = {s["year"]: s for s in entry["shards"]}
    assert shards[2018]["sha256"] != old[2018]                                        # 2018 rebuilt
    assert shards[2019]["sha256"] == old[2019] and shards[2019]["part"] is None       # the previous 2019 stays
    assert (out / "eea_co2_cars" / "2019.sqlite.gz").exists() and entry["build_status"] == "too_large"
    assert [(t["year"], t["part"]) for t in entry["too_large"]] == [(2019, "A-L"), (2019, "M-Z")]


# --- S3: one dataset never costs the others --------------------------------------------------------------------------------

def _fake_builds(sizes: dict[str, int]):
    def fake(name, fetch=None):
        rnd = random.Random(name)
        rows = [{"row_id": str(i), "make": "CADILLAC", "model": "CTS", "year": 2018,
                 "noise": "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(100))}
                for i in range(sizes[name])]
        if sizes[name] < 0:
            return {"status": "failed", "error": "RuntimeError: download failed"}
        ds.write_snapshot(name, rows, {"built_at": "2026-10-06T10:00:00+00:00"})
        return {"status": "built", "rows": len(rows), "built_at": "2026-10-06T10:00:00+00:00"}
    return fake


def _main(build, tmp_path, datasets: str, max_mb: float) -> int:
    return build.main(["--datasets", datasets, "--out", str(tmp_path / "open"), "--probe", "",
                       "--work", str(tmp_path / "work"), "--summary", str(tmp_path / "summary.md"),
                       "--max-mb", str(max_mb)])


def test_one_dataset_too_large_exits_0_with_the_others_written_and_a_warning(tmp_path, monkeypatch, capsys):
    build = _script("build_open_data")
    monkeypatch.setattr(build, "build_dataset", _fake_builds({"epa_fueleconomy": 5, "ademe_car_labelling": 4000,
                                                              "tc_cvs": -1}))
    assert _main(build, tmp_path, "epa_fueleconomy,ademe_car_labelling,tc_cvs", 0.05) == 0
    err = capsys.readouterr().err
    assert "::warning::ademe_car_labelling: " in err and "over the 0 MB limit (not written)" in err
    assert "::warning::tc_cvs: failed" in err and "::error::" not in err
    manifest = json.loads((tmp_path / "open" / "manifest.json").read_text("utf-8"))["datasets"]
    assert manifest["epa_fueleconomy"]["build_status"] == "built" and (tmp_path / "open/epa_fueleconomy.sqlite.gz").exists()
    assert manifest["ademe_car_labelling"]["build_status"] == "too_large"
    assert manifest["ademe_car_labelling"]["last_attempt"]["status"] == "too_large"
    assert not (tmp_path / "open" / "ademe_car_labelling.sqlite.gz").exists()
    assert manifest["tc_cvs"]["last_attempt"]["status"] == "failed"
    summary = (tmp_path / "summary.md").read_text("utf-8")
    assert "**Not written:** ademe_car_labelling" in summary and "**Not written:** tc_cvs" in summary


def test_a_failed_build_keeps_the_previous_file_and_marks_the_entry_failed(tmp_path, monkeypatch):
    build = _script("build_open_data")
    out = tmp_path / "open"
    out.mkdir()
    (out / "tc_cvs.sqlite.gz").write_bytes(b"previous")
    (out / "manifest.json").write_text(json.dumps({"datasets": {"tc_cvs": {
        "file": "tc_cvs.sqlite.gz", "sha256": "abc", "build_status": "built"}}}), "utf-8")
    monkeypatch.setattr(build, "build_dataset", _fake_builds({"tc_cvs": -1, "epa_fueleconomy": 3}))
    _, manifest, warnings = build.run(["tc_cvs", "epa_fueleconomy"], out, tmp_path / "work", {}, LIMIT, None)
    entry = manifest["datasets"]["tc_cvs"]
    assert entry["build_status"] == "failed" and entry["sha256"] == "abc" and entry["file"] == "tc_cvs.sqlite.gz"
    assert (out / "tc_cvs.sqlite.gz").read_bytes() == b"previous" and len(warnings) == 1


def test_nothing_written_exits_1(tmp_path, monkeypatch, capsys):
    build = _script("build_open_data")
    monkeypatch.setattr(build, "build_dataset", _fake_builds({"epa_fueleconomy": 4000, "tc_cvs": -1}))
    assert _main(build, tmp_path, "epa_fueleconomy,tc_cvs", 0.05) == 1
    err = capsys.readouterr().err
    assert "::warning::epa_fueleconomy" in err and "::error::nothing was written" in err
    assert "**Nothing was written**" in (tmp_path / "summary.md").read_text("utf-8")


def test_the_pull_request_and_fallback_steps_always_run_after_coverage():
    workflow = (ROOT / ".github" / "workflows" / "build-open-data.yml").read_text("utf-8")
    steps = workflow[workflow.index("steps:"):]

    def step(name: str) -> str:
        start = steps.index(f"- name: {name}")
        end = steps.find("\n      - ", start + 1)
        return steps[start:end if end > 0 else len(steps)]
    assert "if: always()" in step("Open a pull request")
    assert "if: always() && steps.cpr.outcome == 'failure'" in step("Pull request fallback")
    assert "if: always()" in step("Coverage report")
    assert steps.index("- name: Coverage report") < steps.index("- name: Open a pull request")
    assert "data/open/*/*.sqlite.gz" in step("Open a pull request")             # the shards are committed

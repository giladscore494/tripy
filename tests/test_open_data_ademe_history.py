"""D4: the ADEME catalogue kept per build month (data/open/ademe_car_labelling/<YYYY-MM>.sqlite.gz). A target is
matched against the months whose catalogue year lies within [model year - 1, model year + 1]; the same configuration
in several months counts once (the newest month's values); the former single file still works; an unchanged catalogue
rebuilds to the same bytes and earlier months stay untouched. No network: synthetic ADEME rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures import open_data_live_headers as H
from src.open_data import build as B
from src.open_data import datasets as ds
from src.open_data.build import build_dataset
from src.open_data.match import match_source

from test_open_data_builds import _ademe_fetch, _ademe_rows, _ademe_schema
from test_open_data_repo import _script

SOURCE = "ademe_car_labelling"
LIMIT = 50 * 1024 * 1024


@pytest.fixture(autouse=True)
def _reset():
    yield
    ds.set_repo_dir(None)
    ds.set_snapshot_dir(None)


def _ev6(month: str, mass: int, **extra) -> dict:
    return {"row_id": f"ademe-{month}-{extra.pop('n', 1)}", "make": "KIA", "model": "EV6",
            "version": "EV6 77.4 KWH GT-LINE AWD", "fuel": "EL", "body": "BERLINE", "power_kw": 239,
            "electric_power_kw": 160, "gear_count": 1, "mass_running_order_kg": mass, **extra}


def _keys(year: int) -> dict:
    """A KIA EV6 GT-Line (325 hp, BEV, no co2) of the given model year."""
    return {"makes": ["KIA"], "models": ["EV6"], "type_code": None, "year": year, "cc": None, "power": 325.0,
            "propulsion": "battery_electric", "drivetrain": None, "transmission": None, "body": None, "doors": None,
            "gross_weight": None, "co2_wltp": None, "route": "european", "words": ["ev6", "gt", "line"],
            "battery": [], "bev_or_no_co2": True}


def _two_months(folder: Path) -> None:
    """2025-03: the EV6 (2090 kg) and an e-Niro that left the catalogue later; 2026-10: the EV6 (2100 kg)."""
    niro = {"row_id": "ademe-2025-03-2", "make": "KIA", "model": "E-NIRO", "version": "E-NIRO 64 KWH GT-LINE",
            "fuel": "EL", "body": "BERLINE", "power_kw": 150, "mass_running_order_kg": 1757}
    ds.write_month_snapshot(SOURCE, [_ev6("2025-03", 2090), niro], {"catalogue_date": "2025-03-28T21:00:00Z"},
                            month="2025-03", folder=folder)
    ds.write_month_snapshot(SOURCE, [_ev6("2026-10", 2100)], {"catalogue_date": "2026-09-29T21:00:00Z"},
                            month="2026-10", folder=folder)


def test_a_2024_target_reads_only_the_2025_month(tmp_path):
    _two_months(tmp_path)
    out = match_source(SOURCE, _keys(2024), folder=tmp_path)
    period = out["catalogue"]
    assert period["status"] == "in_period" and period["months"] == ["2025-03"] and period["window"] == [2023, 2025]
    assert period["available_months"] == ["2026-10", "2025-03"] and period["basis"] == "catalogue_date"
    assert out["candidates"] == 1 and out["status"] == "unique"
    assert out["survivors"][0]["mass_running_order_kg"] == 2090 and out["survivors"][0]["catalogue_month"] == "2025-03"


def test_a_2026_target_reads_both_months_and_one_configuration_counts_once_with_the_newest_values(tmp_path):
    _two_months(tmp_path)
    out = match_source(SOURCE, _keys(2026), folder=tmp_path)
    period = out["catalogue"]
    assert period["months"] == ["2026-10", "2025-03"] and period["duplicates_dropped"] == 1 and period["rows"] == 2
    assert out["candidates"] == 1                                  # the e-Niro is another model: not a candidate
    (ev6,) = out["survivors"]
    assert ev6["mass_running_order_kg"] == 2100 and ev6["catalogue_month"] == "2026-10"
    assert ev6["row_id"] == "ademe-2026-10-1"


def test_rows_of_one_month_are_never_merged_with_each_other(tmp_path):
    ds.write_month_snapshot(SOURCE, [_ev6("2026-10", 2100), _ev6("2026-10", 2100, n=2)],
                            {"catalogue_date": "2026-09-29"}, month="2026-10", folder=tmp_path)
    out = match_source(SOURCE, _keys(2026), folder=tmp_path)
    assert out["candidates"] == 2 and out["catalogue"]["duplicates_dropped"] == 0


def test_a_2020_target_is_out_of_period(tmp_path):
    _two_months(tmp_path)
    out = match_source(SOURCE, _keys(2020), folder=tmp_path)
    assert out["status"] == "out_of_period" and out["candidates"] == 0
    assert out["catalogue"]["months"] == [] and out["catalogue"]["window"] == [2019, 2021]


def _commit_legacy(repo: Path, work: Path, rows: list[dict], built_at: str = "2026-10-06T18:23:12+00:00") -> dict:
    """data/open/ademe_car_labelling.sqlite.gz and its manifest entry exactly as on main before D4."""
    build = _script("build_open_data")
    path = ds.write_snapshot(SOURCE, rows, {"built_at": built_at}, folder=work)
    repo.mkdir(parents=True, exist_ok=True)
    build.gzip_file(path, repo / f"{SOURCE}.sqlite.gz")
    entry = {"file": f"{SOURCE}.sqlite.gz", "sha256": ds.sha256_file(repo / f"{SOURCE}.sqlite.gz"),
             "bytes": (repo / f"{SOURCE}.sqlite.gz").stat().st_size, "built_at": built_at, "rows": len(rows),
             "build_status": "built"}
    (repo / "manifest.json").write_text(json.dumps({"datasets": {SOURCE: entry}}), "utf-8")
    return entry


def test_the_legacy_single_file_is_read_as_the_month_of_its_built_at(tmp_path):
    repo = tmp_path / "repo"
    _commit_legacy(repo, tmp_path / "work", [{**_ev6("x", 2095), "row_id": "ademe-1"}])
    ds.set_repo_dir(repo, tmp_path / "tmp")
    assert ds.available(SOURCE)
    out = match_source(SOURCE, _keys(2026))
    assert out["catalogue"]["months"] == ["2026-10"] and out["catalogue"]["legacy_month"] == "2026-10"
    assert out["catalogue"]["basis"] == "built_at" and out["survivors"][0]["row_id"] == "ademe-1"
    assert match_source(SOURCE, _keys(2020))["status"] == "out_of_period"
    assert ds.query_rows(SOURCE, makes=["KIA"])[0]["mass_running_order_kg"] == 2095


def _ademe_build(monkeypatch, month: str, rows=None):
    body = H.csv_text(H.ADEME_ORIGINAL_NAMES, rows or _ademe_rows({}, {"Description Commerciale": "EV6 RWD"}),
                      delimiter=";")
    fetch = _ademe_fetch(_ademe_schema(), body)
    monkeypatch.setattr(B, "build_month", lambda: month)
    return fetch


def test_the_build_writes_the_month_and_keeps_earlier_months_and_the_legacy_file(tmp_path, monkeypatch):
    build = _script("build_open_data")
    out = tmp_path / "open"
    legacy = _commit_legacy(out, tmp_path / "legacy", [{**_ev6("x", 2095), "row_id": "ademe-1"}])
    legacy_bytes = (out / f"{SOURCE}.sqlite.gz").read_bytes()
    shas = {}
    for n, month in enumerate(("2026-10", "2026-10", "2026-11")):
        fetch = _ademe_build(monkeypatch, month)
        monkeypatch.setattr(build, "build_dataset", lambda name, fetch_=None, f=fetch: build_dataset(name, f))
        results, manifest, warnings = build.run([SOURCE], out, tmp_path / f"work{n}", {}, LIMIT, f"https://run/{n}")
        assert not warnings and results[SOURCE]["written"] == 1
        entry = manifest["datasets"][SOURCE]
        shas[n] = {s["month"]: s["sha256"] for s in entry["shards"]}
        if n == 0:
            first = (out / SOURCE / "2026-10.sqlite.gz").read_bytes()
    assert shas[0] == shas[1]                                       # the same catalogue, the same month: same bytes
    assert set(shas[2]) == {"2026-10", "2026-11"} and shas[2]["2026-10"] == shas[0]["2026-10"]
    assert (out / SOURCE / "2026-10.sqlite.gz").read_bytes() == first          # an earlier month stays untouched
    assert (out / f"{SOURCE}.sqlite.gz").read_bytes() == legacy_bytes          # the former single file: never moved
    entry = manifest["datasets"][SOURCE]
    assert entry["monthly"] and entry["legacy"]["file"] == legacy["file"] and entry["legacy"]["month"] == "2026-10"
    assert "file" not in entry and [s["file"] for s in entry["shards"]] == [
        f"{SOURCE}/2026-10.sqlite.gz", f"{SOURCE}/2026-11.sqlite.gz"]
    for shard in entry["shards"]:
        assert shard["sha256"] == ds.sha256_file(out / shard["file"]) and shard["rows"] == 2 and shard["bytes"] > 0
        assert shard["catalogue_date"] and shard["year"] == 2026
    ds.set_repo_dir(out, tmp_path / "tmp")
    files = ds.month_files(SOURCE)
    assert [f["month"] for f in files] == ["2026-11", "2026-10"]                 # legacy 2026-10 superseded
    rows = ds.query_rows(SOURCE, makes=["KIA"])
    assert {r["row_id"] for r in rows} == {"ademe-2026-11-1", "ademe-2026-11-2", "ademe-2026-10-1",
                                           "ademe-2026-10-2"}
    text = build.summary(results, manifest)
    assert "Monthly snapshots of ademe_car_labelling (D4" in text
    assert f"| 2026-10 | {SOURCE}/2026-10.sqlite.gz |" in text and f"| 2026-11 | {SOURCE}/2026-11.sqlite.gz |" in text
    assert "superseded by the monthly snapshot of that month" in text
    total = sum(s["bytes"] for s in entry["shards"]) + entry["legacy"]["bytes"]
    assert f"ademe_car_labelling: 3 month(s), {total / 1024 / 1024:.1f} MB in total." in text


def test_a_month_snapshot_is_byte_identical_when_rebuilt(tmp_path, monkeypatch):
    raw = []
    for n in (1, 2):
        fetch = _ademe_build(monkeypatch, "2026-10")
        ds.set_snapshot_dir(tmp_path / f"w{n}")
        try:
            built = build_dataset(SOURCE, fetch)
        finally:
            ds.set_snapshot_dir(None)
        assert built["status"] == "built" and built["months"][0]["month"] == "2026-10"
        raw.append(Path(built["months"][0]["path"]).read_bytes())
    assert raw[0] == raw[1]

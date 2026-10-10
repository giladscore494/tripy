"""Concurrent first reads of a committed .gz snapshot (production log, 2026-10-10 00:36): two request threads
decompressing the same file shared one temp name (`.<target>.<pid>.tmp`); the loser's os.replace raised
FileNotFoundError, `decompress_failed` was remembered for the life of the process and every facts call of a key that
reads that dataset answered with skipped shards, never cached. One decompression per target now, whoever asks."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from src.facts import service as S
from src.gov_data import snapshot as SN
from src.open_data import datasets as ds
from src.storage.atomic import gunzip_once
from test_facts_api import KEY, FakeMilo, snapshot_rows

THREADS = 8


def _gz(path: Path, size_mb: int = 6) -> str:
    """A .gz of `size_mb` MB (big enough that concurrent decompressions overlap); its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    block = b"".join(i.to_bytes(4, "little") for i in range(256 * 1024))        # 1 MB, compresses a little
    with gzip.open(path, "wb", compresslevel=1) as fh:
        for _ in range(size_mb):
            fh.write(block)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _together(fn):
    """Run fn() in THREADS threads released at the same instant; their results (or exceptions)."""
    barrier, out = threading.Barrier(THREADS), [None] * THREADS

    def run(n):
        barrier.wait()
        try:
            out[n] = fn()
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            out[n] = exc
    threads = [threading.Thread(target=run, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


def test_gunzip_once_decompresses_once_for_concurrent_callers(tmp_path):
    source = tmp_path / "src" / "x.sqlite.gz"
    _gz(source)
    target = tmp_path / "out" / "x.sqlite"
    results = _together(lambda: gunzip_once(source, target))
    assert results == [target] * THREADS
    assert target.stat().st_size == 6 * 1024 * 1024
    assert [p.name for p in target.parent.iterdir()] == ["x.sqlite"]          # no temp file left behind


def test_a_corrupt_gz_raises_and_leaves_nothing(tmp_path):
    source = tmp_path / "bad.sqlite.gz"
    source.write_bytes(b"not a gzip file")
    target = tmp_path / "out" / "bad.sqlite"
    with pytest.raises((OSError, EOFError)):
        gunzip_once(source, target)
    assert not target.exists() and list(target.parent.iterdir()) == []


def test_concurrent_first_reads_of_an_open_data_shard_all_get_it(tmp_path):
    repo = tmp_path / "open"
    sha = _gz(repo / "ademe_car_labelling" / "2026-10.sqlite.gz")
    ds.set_repo_dir(repo, tmp_path / "decompressed")
    try:
        results = _together(lambda: ds._materialize_file("ademe_car_labelling", "ademe_car_labelling/2026-10.sqlite.gz",
                                                         sha))
        assert all(isinstance(r, tuple) and r[1] is None and r[0] and r[0].exists() for r in results), results
        assert len({r[0] for r in results}) == 1
        # nothing poisoned: the next read is the same file, no problem
        assert ds._materialize_file("ademe_car_labelling", "ademe_car_labelling/2026-10.sqlite.gz", sha)[1] is None
    finally:
        ds.set_repo_dir(None)


def test_concurrent_first_reads_of_a_gov_snapshot_all_get_it(tmp_path):
    gov = tmp_path / "gov"
    sha = _gz(gov / "recall_notices.sqlite.gz")
    (gov / "manifest.json").write_text(json.dumps({"datasets": {"recall_notices": {"file": "recall_notices.sqlite.gz",
                                                                                   "sha256": sha}}}), "utf-8")
    SN.set_gov_dir(gov, tmp_path / "decompressed")
    try:
        results = _together(lambda: SN.materialize("recall_notices"))
        assert all(isinstance(r, Path) and r.exists() for r in results), results
        assert len(set(results)) == 1
        assert sorted(os.listdir(tmp_path / "decompressed")) == [results[0].name]
    finally:
        SN.set_gov_dir(None)


def test_the_call_log_names_the_skipped_files_and_the_part_is_not_cached(monkeypatch, caplog, tripy_log):
    skipped = [{"source": "ademe_car_labelling", "file": "ademe_car_labelling/2026-10.sqlite.gz",
                "problem": "decompress_failed"}]

    def part_with_a_skip(row, government, **kw):
        return {"facts": {}, "withheld": [], "skipped_shards": ["decompress_failed"], "skipped_files": skipped}

    monkeypatch.setattr(S, "open_data_part", part_with_a_skip)
    svc = S.FactsService(FakeMilo(), rows_by_source=lambda row: snapshot_rows(row))
    with tripy_log("tripy.facts"):
        _, line = svc.records([KEY["22010"]])
        _, again = svc.records([KEY["22010"]])
    assert line["skipped"] == {KEY["22010"]: skipped}
    assert line["cache"] == again["cache"] == {KEY["22010"]: "miss"}                 # answered, never cached
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("facts call ")]
    assert '"file":"ademe_car_labelling/2026-10.sqlite.gz"' in logged[0] and '"problem":"decompress_failed"' in logged[0]


def test_a_call_without_skips_has_no_skipped_field():
    svc = S.FactsService(FakeMilo(), rows_by_source=lambda row: snapshot_rows(row))
    _, line = svc.records([KEY["22010"]])
    assert "skipped" not in line

"""E4: the in-process candidate index of the year shards (EEA), so a match never scans every row of a make.

    index      per decompressed shard file: its rows grouped by the text the candidate search reads (make, model,
               base_model, variant, version, year; the group key) -> the SQLite rowids of the group. A group's
               representative row carries exactly those columns: the model-alias filter (match._model_matches) and the
               type-code rules (match.type_code_rule, by canonical make) give one answer for every row of a group, so
               they are evaluated once per group and only the groups they keep are read from the shard
               (`SELECT * ... WHERE rowid IN (...)`, then the scan's row_id order)
    build      never inside a request: the first request that opens a shard asks for it (`request`) and is answered by
               the scan; a single background worker builds it. At boot `start_warmup` asks for the shards of
               WARMUP_YEARS (2015-2025) through datasets.shard_files, so the free-space guard of the decompression
               still applies (a refused shard is skipped, never indexed)
    memory     MEMORY_CAP_BYTES for all indices (an estimate per index: strings, groups, rowids); over it the least
               recently used index (one shard = one year or one make part of a year) is dropped
    off        TRIPY_SHARD_INDEX=0 disables it (every request scans); TRIPY_SHARD_WARMUP=0 skips the boot warm-up

Pure equivalence: the index returns the same candidate rows, in the same order, as the scan (tests: 200 keys).
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

from . import datasets as ds

GROUP_COLUMNS = ("make", "model", "base_model", "variant", "version", "year")
MEMORY_CAP_BYTES = 300 * 1024 * 1024
WARMUP_YEARS = tuple(range(2015, 2026))
WARMUP_DATASETS = ("eea_co2_cars",)
SIZE_MARGIN = 1.25                        # sys.getsizeof misses the small ints and the over-allocation (traced: +17-25 %)
log = logging.getLogger("tripy.open_data")


class ShardIndex:
    """One shard file's groups: {make: [(group key tuple, rowid | rowids)]} (+ its measured size). The group key holds
    the `columns` values (strings interned per index); a group of one row keeps a bare rowid."""

    def __init__(self, path: Path, columns: list[str], by_make: dict[str, list[tuple[tuple, Any]]], rows: int):
        self.path, self.columns, self.by_make, self.rows = path, columns, by_make, rows
        self.groups = sum(len(groups) for groups in by_make.values())
        self.bytes = int(_size(by_make) * SIZE_MARGIN)

    def groups_of(self, makes: list[str], years: list[int] | None) -> list[tuple[dict, Any]]:
        """[(representative row, rowids)] of these makes / years (a representative: the group's non-null columns)."""
        year_at = self.columns.index("year") if "year" in self.columns else None
        out = []
        for make in makes:
            for key, ids in self.by_make.get(make) or []:
                if years is None or (year_at is not None and key[year_at] in years):
                    out.append(({c: v for c, v in zip(self.columns, key) if v is not None}, ids))
        return out


def _size(by_make: dict) -> int:
    """The index's memory (bytes): its containers, tuples, arrays and distinct strings (sys.getsizeof)."""
    import sys

    total = sys.getsizeof(by_make)
    seen: set[int] = set()
    for make, groups in by_make.items():
        total += sys.getsizeof(groups)
        for key, ids in groups:
            total += sys.getsizeof(key) + 56 + (sys.getsizeof(ids) if isinstance(ids, array) else 0)
            for value in key:
                if isinstance(value, str) and id(value) not in seen:
                    seen.add(id(value))
                    total += sys.getsizeof(value)
    return total


def _ids(ids: Any) -> list[int]:
    return [ids] if isinstance(ids, int) else list(ids)


_LOCK = threading.Lock()
_INDICES: "OrderedDict[str, ShardIndex]" = OrderedDict()
_PENDING: set[str] = set()
_FAILED: set[str] = set()
_QUEUE: "queue.Queue[tuple[str, Path]]" = queue.Queue()
_STATE: dict[str, Any] = {"worker": None, "warmup": None, "warmup_done": False, "built": 0, "evicted": 0,
                          "build_s": 0.0}


def enabled() -> bool:
    return os.environ.get("TRIPY_SHARD_INDEX", "1").strip() not in ("0", "false", "no")


def _key(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return str(path)
    return f"{path}|{stat.st_size}|{stat.st_mtime_ns}"          # a rewritten file is another index


def build(path: Path) -> ShardIndex | None:
    """The index of one shard file (synchronous; the worker and the tests call it). None when unreadable."""
    started = time.perf_counter()
    try:
        with ds._connect(path) as conn:
            names = [r[1] for r in conn.execute("PRAGMA table_info(rows)")]
            if "data" in names:
                return None                         # an untyped single snapshot: the scan reads it
            columns = [c for c in GROUP_COLUMNS if c in names]
            cursor = conn.execute(f"SELECT rowid, {', '.join(columns)} FROM rows")
            groups: dict[tuple, Any] = {}
            pool: dict[str, str] = {}
            rows = 0
            for record in cursor:
                rows += 1
                key = tuple(pool.setdefault(v, v) if isinstance(v, str) else v for v in record[1:])
                ids = groups.get(key)
                if ids is None:
                    groups[key] = int(record[0])
                elif isinstance(ids, int):
                    groups[key] = array("q", (ids, record[0]))
                else:
                    ids.append(record[0])
    except sqlite3.Error:
        return None
    by_make: dict[str, list[tuple[tuple, Any]]] = {}
    make_at = columns.index("make")
    for key, ids in groups.items():
        by_make.setdefault(key[make_at], []).append((key, ids))
    index = ShardIndex(path, columns, by_make, rows)
    _STATE["build_s"] = float(_STATE["build_s"]) + time.perf_counter() - started
    return index


def _store(key: str, index: ShardIndex) -> None:
    with _LOCK:
        _INDICES[key] = index
        _INDICES.move_to_end(key)
        _STATE["built"] = int(_STATE["built"]) + 1
        while sum(i.bytes for i in _INDICES.values()) > MEMORY_CAP_BYTES and len(_INDICES) > 1:
            _INDICES.popitem(last=False)
            _STATE["evicted"] = int(_STATE["evicted"]) + 1


def build_now(path: Path) -> ShardIndex | None:
    """Build and keep the index of a file now (the tests; the worker)."""
    key = _key(path)
    index = build(path)
    if index is None:
        with _LOCK:
            _FAILED.add(key)
        return None
    _store(key, index)
    return index


def get(path: Path) -> ShardIndex | None:
    """The ready index of a file, or None (never waits)."""
    if not enabled():
        return None
    key = _key(path)
    with _LOCK:
        index = _INDICES.get(key)
        if index is not None:
            _INDICES.move_to_end(key)
        return index


def request(path: Path) -> None:
    """Ask the background worker to index a file (no-op when indexed, queued, failed, or the index is off)."""
    if not enabled():
        return
    key = _key(path)
    with _LOCK:
        if key in _INDICES or key in _PENDING or key in _FAILED:
            return
        _PENDING.add(key)
    _QUEUE.put((key, path))
    _ensure_worker()


def _ensure_worker() -> None:
    with _LOCK:
        worker = _STATE.get("worker")
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(target=_work, name="shard-index", daemon=True)
        _STATE["worker"] = worker
    worker.start()


def _work() -> None:
    while True:
        try:
            key, path = _QUEUE.get(timeout=30)
        except queue.Empty:
            return
        try:
            if path.exists():
                build_now(path)
        except Exception:  # noqa: BLE001 - the index is an accelerator: a failure leaves the scan
            log.warning("shard index: %s not indexed", path.name, exc_info=True)
            with _LOCK:
                _FAILED.add(key)
        finally:
            with _LOCK:
                _PENDING.discard(key)


def start_warmup(years: Iterable[int] = WARMUP_YEARS, datasets: Iterable[str] = WARMUP_DATASETS) -> None:
    """At boot (once per process): decompress (free-space guard) and index the shards of these years in the
    background. Requests never wait for it."""
    if not enabled() or os.environ.get("TRIPY_SHARD_WARMUP", "1").strip() in ("0", "false", "no"):
        return
    with _LOCK:
        if _STATE.get("warmup") is not None:
            return
        thread = threading.Thread(target=_warm, args=(tuple(years), tuple(datasets)), name="shard-warmup",
                                  daemon=True)
        _STATE["warmup"] = thread
    thread.start()


def _warm(years: tuple[int, ...], names: tuple[str, ...]) -> None:
    started = time.perf_counter()
    for name in names:
        for year in years:
            try:
                report: dict = {}
                for path in ds.shard_files(name, years=[year], report=report):
                    key = _key(path)
                    with _LOCK:
                        done = key in _INDICES or key in _FAILED
                    if not done:
                        build_now(path)
            except Exception:  # noqa: BLE001 - the warm-up never costs the process
                log.warning("shard warm-up: %s %s skipped", name, year, exc_info=True)
    _STATE["warmup_done"] = True
    log.info("shard warm-up done in %.1f s: %s", time.perf_counter() - started, status())


def status() -> dict:
    with _LOCK:
        return {"enabled": enabled(), "indices": len(_INDICES), "bytes": sum(i.bytes for i in _INDICES.values()),
                "cap_bytes": MEMORY_CAP_BYTES, "pending": len(_PENDING), "failed": len(_FAILED),
                "built": _STATE["built"], "evicted": _STATE["evicted"], "build_s": round(float(_STATE["build_s"]), 2),
                "warmup_done": bool(_STATE["warmup_done"])}


def clear() -> None:
    """Drop every index (tests)."""
    with _LOCK:
        _INDICES.clear()
        _PENDING.clear()
        _FAILED.clear()


def fetch(dataset: str, path: Path, groups: list[tuple[dict, Any]]) -> list[dict]:
    """The full rows of these groups, exactly as datasets._query_file returns them (units applied, nulls left out,
    ordered by row_id)."""
    ids = sorted({i for _, group in groups for i in _ids(group)})
    if not ids:
        return []
    import json

    try:
        with ds._connect(path) as conn:
            out = []
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                cursor = conn.execute(f"SELECT * FROM rows WHERE rowid IN ({','.join('?' * len(chunk))})", chunk)
                columns = [d[0] for d in cursor.description]
                out += cursor.fetchall()
            units = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta WHERE key = 'column_units'")}
    except sqlite3.Error:
        return []
    usable = ds.unit_rules(dataset, units.get("column_units") or {})
    rows = [ds._apply_units({k: v for k, v in zip(columns, row) if v is not None}, usable) for row in out]
    rows.sort(key=lambda r: (r.get("row_id") is not None, str(r.get("row_id") or "")))   # NULL first
    return rows


def indexed_candidates(dataset: str, makes: Iterable[str], years: Iterable[int] | None,
                       keep: Callable[[list[dict]], set[int]], folder=None,
                       report: dict | None = None) -> tuple[list[dict], list[dict]] | None:
    """(the representatives of every group of the asked makes / years, the full rows of the groups `keep` selects)
    when every shard file of the query has a ready index; None otherwise (the caller scans; the missing files are
    queued for the worker). `keep(representatives)` returns the indices (into the representatives) to read."""
    makes = sorted({" ".join(str(m).split()).upper() for m in makes if str(m or "").strip()})
    if not makes or not enabled():
        return None
    years_list = sorted({int(y) for y in years}) if years else None
    paths = ds.shard_files(dataset, years=years_list, makes=makes, folder=folder, report=report)
    indices = []
    for path in paths:
        index = get(path) if path.exists() else None
        if index is None:
            if path.exists():
                request(path)
            indices.append(None)
        else:
            indices.append(index)
    if any(i is None for i in indices):
        return None
    reps: list[dict] = []
    owners: list[tuple[int, tuple[dict, Any]]] = []
    for n, index in enumerate(indices):
        for group in index.groups_of(makes, years_list):
            reps.append(group[0])
            owners.append((n, group))
    chosen = keep(reps)
    rows: list[dict] = []
    for n, path in enumerate(paths):                  # file by file, in the scan's file order
        groups = [owners[i][1] for i in sorted(chosen) if owners[i][0] == n]
        rows += fetch(dataset, path, groups)
    return reps, rows

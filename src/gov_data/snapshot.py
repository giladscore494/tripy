"""Deterministic SQLite snapshots of the government datasets (data/gov/<dataset>.sqlite.gz) and their read side.

Write: typed tables, the rows sorted, a meta table free of timestamps, VACUUMed, then gzip with mtime 0 and a fixed
inner name: the same aggregates give the same bytes (and the same sha256). The provenance timestamps live in
data/gov/manifest.json, never in the snapshot.

Read: the manifest entry's sha256 is verified, the file decompressed once into the container's temp dir (never the
data volume) under the open-data free-space guard (a refusal is counted by open_data.datasets.disk_refusals, which
the facts service turns into snapshots_unavailable). A missing file, a mismatched sha256 or no manifest entry means
no facts of that dataset (never an error).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

GOV_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "gov"
MANIFEST_NAME = "manifest.json"
MAP_NAME = "recall_model_map.json"
MANIFEST_VERSION = "gov-manifest-v1"
_STATE: dict[str, Any] = {"gov_dir": None, "temp_dir": None}
_MATERIALIZED: dict[str, tuple[str, Path | None]] = {}
_LOCK = threading.Lock()


def set_gov_dir(path: Path | str | None, temp_dir: Path | str | None = None) -> None:
    """Tests point the committed folder (and the decompression folder) elsewhere."""
    _STATE["gov_dir"] = Path(path) if path else None
    _STATE["temp_dir"] = Path(temp_dir) if temp_dir else None
    _MATERIALIZED.clear()


def gov_dir() -> Path:
    return _STATE.get("gov_dir") or GOV_DIR


def _temp_dir() -> Path:
    return _STATE.get("temp_dir") or Path(tempfile.gettempdir()) / "tripy-gov-data"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def manifest(folder: Path | None = None) -> dict:
    return read_json((folder or gov_dir()) / MANIFEST_NAME)


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- write ------------------------------------------------------------------------------------------------------------

def _order(value: Any) -> tuple:
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, float(value), "")
    return (2, 0.0, str(value))


def write_sqlite(path: Path, tables: dict[str, tuple[dict[str, str], Iterable[tuple]]], meta: dict) -> Path:
    """Write `tables` ({name: ({column: SQL type}, rows)}) and `meta` deterministically to `path` (atomic swap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    for leftover in (tmp, tmp.with_name(tmp.name + "-journal")):
        if leftover.exists():
            leftover.unlink()
    with _LOCK:
        conn = sqlite3.connect(tmp)
        try:
            for name, (columns, rows) in sorted(tables.items()):
                conn.execute(f"CREATE TABLE [{name}] ({', '.join(f'[{c}] {t}' for c, t in columns.items())})")
                ordered = sorted((tuple(r) for r in rows), key=lambda r: tuple(_order(v) for v in r))
                conn.executemany(f"INSERT INTO [{name}] VALUES ({','.join('?' * len(columns))})", ordered)
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO meta VALUES (?, ?)",
                             [(k, json.dumps(meta[k], ensure_ascii=False, sort_keys=True, default=str))
                              for k in sorted(meta)])
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        os.replace(tmp, path)
    return path


def gzip_file(source: Path, target: Path) -> None:
    """Deterministic gzip (mtime 0, the inner name = the target's name without .gz)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    with open(source, "rb") as src, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename=target.name.removesuffix(".gz"), mode="wb", fileobj=raw, mtime=0,
                           compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    os.replace(tmp, target)


# --- read -------------------------------------------------------------------------------------------------------------

def materialize(dataset: str) -> Path | None:
    """The decompressed snapshot of a dataset, verified against the manifest (None when not usable)."""
    entry = (manifest().get("datasets") or {}).get(dataset) or {}
    file, expected = entry.get("file"), entry.get("sha256")
    if not file or not expected:
        return None
    cached = _MATERIALIZED.get(dataset)
    if cached and cached[0] == expected and cached[1] is not None and cached[1].exists():
        return cached[1]
    source = gov_dir() / str(file)
    if not source.is_file() or sha256_file(source) != expected:
        _MATERIALIZED[dataset] = (expected, None)
        return None
    target = _temp_dir() / f"{dataset}-{expected[:16]}.sqlite"
    if not target.exists():
        from ..open_data.datasets import _room_for

        if not _room_for(source, target.parent, f"gov data {dataset}"):
            return None                                  # counted as a refusal: the service answers 503
        from ..storage.atomic import gunzip_once

        try:
            gunzip_once(source, target)          # one decompression per target, even for concurrent requests
        except (OSError, EOFError, gzip.BadGzipFile):
            _MATERIALIZED[dataset] = (expected, None)
            return None
    _MATERIALIZED[dataset] = (expected, target)
    return target


def query(path: Path, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        try:
            cursor = conn.execute(sql, list(params))
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def meta_of(path: Path) -> dict:
    return {r["key"]: json.loads(r["value"]) for r in query(path, "SELECT key, value FROM meta")}

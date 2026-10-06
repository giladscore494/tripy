"""Dataset configuration (data/open_datasets.json) and the SQLite snapshots.

The snapshots are built by the build-open-data GitHub Action (network + runner disk) and committed to the repository:
data/open/<dataset>.sqlite.gz with data/open/manifest.json (per dataset: file, sha256 of the .gz, rows, years / files,
absent columns, units, built_at, the Action run URL, licence / attribution). The deploy image carries them; at first use
a snapshot is verified against the manifest's sha256 and decompressed into the container's temporary directory
(ephemeral disk, NEVER the data volume). A missing file or a sha256 mismatch is `no_snapshot`. `set_snapshot_dir`
points the engine at a folder of plain <dataset>.sqlite files instead (the build and the tests).

One file per dataset: <dataset>.sqlite with

    rows(row_id TEXT PRIMARY KEY, make TEXT, model TEXT, year INTEGER, data TEXT)   data = the canonical columns (JSON)
    meta(key TEXT PRIMARY KEY, value TEXT)                                          built_at, source_url, rows, schema
                                                                                    (the live header), licence, years /
                                                                                    files (per year / file report),
                                                                                    absent_columns, build_duration_s
                                                                                    (+ size_bytes when read)

The engine only reads (a read-only URI connection); the builder writes a new file next to the old one and swaps it in
atomically, so a failed build keeps the previous snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_datasets.json"
ADMISSION_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_data_admission.json"
REPO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "open"
MANIFEST_NAME = "manifest.json"
PROBE_NAME = "probe.json"
UNKNOWN_UNIT = "unknown"
_STATE: dict[str, Any] = {"snapshot_dir": None, "repo_dir": None, "temp_dir": None}
_MATERIALIZED: dict[str, tuple[str, Path | None, str | None]] = {}      # dataset -> (sha256, path, problem)
log = logging.getLogger("tripy.open_data")
_CONFIG: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def config(path: Path | str | None = None) -> dict:
    path = Path(path or CONFIG_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CONFIG.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text("utf-8"))
    _CONFIG[str(path)] = (mtime, data)
    return data


def datasets() -> dict[str, dict]:
    return {k: v for k, v in (config().get("datasets") or {}).items() if isinstance(v, dict)}


def admission_allowlist(path: Path | str | None = None) -> set[tuple[str, str, str]]:
    """(source, field, route) triples admit mode may admit (data/open_data_admission.json; starts empty: the proof
    gate decides what goes in)."""
    try:
        data = json.loads(Path(path or ADMISSION_PATH).read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    out = set()
    for row in data.get("triples") or []:
        if isinstance(row, dict) and row.get("source") and row.get("field") and row.get("route"):
            out.add((str(row["source"]), str(row["field"]), str(row["route"])))
    return out


def set_snapshot_dir(path: Path | str | None) -> None:
    """<TRIPY_DATA_DIR>/derived/open (set by the RunManager; tests point it at a temporary folder)."""
    _STATE["snapshot_dir"] = Path(path) if path else None


def snapshot_dir() -> Path | None:
    return _STATE.get("snapshot_dir")


def set_repo_dir(path: Path | str | None, temp_dir: Path | str | None = None) -> None:
    """The committed snapshots (default data/open/) and where they are decompressed (default: the system temp dir);
    tests point both at temporary folders."""
    _STATE["repo_dir"] = Path(path) if path else None
    _STATE["temp_dir"] = Path(temp_dir) if temp_dir else None
    _MATERIALIZED.clear()


def repo_dir() -> Path:
    return _STATE.get("repo_dir") or REPO_DIR


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def manifest() -> dict:
    """data/open/manifest.json ({} before the first Action build)."""
    return _read_json(repo_dir() / MANIFEST_NAME)


def probe_summary() -> dict:
    """data/open/probe.json: the last live-schema probe of the Action ({} before the first)."""
    return _read_json(repo_dir() / PROBE_NAME)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temp_dir() -> Path:
    return _STATE.get("temp_dir") or Path(tempfile.gettempdir()) / "tripy-open-data"


def materialize(dataset: str) -> tuple[Path | None, str | None]:
    """(path of the decompressed snapshot, problem): the committed data/open/<dataset>.sqlite.gz verified against the
    manifest's sha256 and decompressed once into the temp dir. problem: no_manifest_entry | missing_file |
    sha256_mismatch | decompress_failed."""
    entry = (manifest().get("datasets") or {}).get(dataset) or {}
    if not entry.get("file") or not entry.get("sha256"):
        return None, "no_manifest_entry"
    expected = str(entry["sha256"])
    cached = _MATERIALIZED.get(dataset)
    if cached and cached[0] == expected and (cached[1] is None or cached[1].exists()):
        return cached[1], cached[2]
    source = repo_dir() / str(entry["file"])
    if not source.is_file():
        _MATERIALIZED[dataset] = (expected, None, "missing_file")
        return None, "missing_file"
    actual = sha256_file(source)
    if actual != expected:
        log.error("open data %s: sha256 mismatch (manifest %s, file %s): no snapshot", dataset, expected[:12],
                  actual[:12])
        _MATERIALIZED[dataset] = (expected, None, "sha256_mismatch")
        return None, "sha256_mismatch"
    target = _temp_dir() / f"{dataset}-{expected[:16]}.sqlite"
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(tmp, target)
        except (OSError, EOFError, gzip.BadGzipFile):
            log.error("open data %s: could not decompress %s", dataset, source, exc_info=True)
            _MATERIALIZED[dataset] = (expected, None, "decompress_failed")
            return None, "decompress_failed"
    _MATERIALIZED[dataset] = (expected, target, None)
    return target, None


def snapshot_path(dataset: str, folder: Path | None = None) -> Path | None:
    """A plain snapshot folder when one is given (or set: the build, the tests), else the committed snapshot."""
    folder = folder or snapshot_dir()
    if folder:
        return folder / f"{dataset}.sqlite"
    return materialize(dataset)[0]


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def snapshot_meta(dataset: str, folder: Path | None = None) -> dict:
    path = snapshot_path(dataset, folder)
    if path is None or not path.exists():
        return {}
    try:
        with _connect(path) as conn:
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
        meta["size_bytes"] = path.stat().st_size
        return meta
    except (sqlite3.Error, OSError):
        return {}


def available(dataset: str, folder: Path | None = None) -> bool:
    path = snapshot_path(dataset, folder)
    return bool(path and path.exists())


def query_rows(dataset: str, *, makes: Iterable[str], years: Iterable[int] | None = None,
               folder: Path | None = None) -> list[dict]:
    """Rows of the snapshot whose make is one of `makes` (upper case) and, when given, whose year is in `years`.
    Each row: the canonical columns + `row_id`. Empty without a snapshot."""
    path = snapshot_path(dataset, folder)
    makes = sorted({str(m).strip().upper() for m in makes if str(m or "").strip()})
    if path is None or not path.exists() or not makes:
        return []
    sql = f"SELECT row_id, data FROM rows WHERE make IN ({','.join('?' * len(makes))})"
    params: list[Any] = list(makes)
    years = sorted({int(y) for y in years}) if years is not None else None
    if years:
        sql += f" AND year IN ({','.join('?' * len(years))})"
        params += years
    try:
        with _connect(path) as conn:
            rows = conn.execute(sql + " ORDER BY row_id", params).fetchall()
            units = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta WHERE key = 'column_units'")}
    except sqlite3.Error:
        return []
    usable = unit_rules(dataset, units.get("column_units") or {})
    return [_apply_units({**json.loads(data), "row_id": row_id}, usable) for row_id, data in rows]


def unit_rules(dataset: str, column_units: dict) -> dict[str, float | None]:
    """{key: scale} for the columns stored with an unknown unit: the scale of the dataset's `unit_overrides` entry (the
    operator confirmed the unit) or None (the key is dropped: no match key, no offer)."""
    overrides = (datasets().get(dataset) or {}).get("unit_overrides") or {}
    rules: dict[str, float | None] = {}
    for key, info in (column_units or {}).items():
        if not isinstance(info, dict) or info.get("unit") != UNKNOWN_UNIT:
            continue
        override = overrides.get(info.get("unit_of") or key) if isinstance(overrides, dict) else None
        rules[key] = float(override.get("scale") or 1) if isinstance(override, dict) and override.get("unit") else None
    return rules


def _apply_units(row: dict, rules: dict[str, float | None]) -> dict:
    for key, scale in rules.items():
        if key not in row:
            continue
        if scale is None:
            row.pop(key)
            continue
        try:
            row[key] = float(str(row[key]).strip()) * scale if row[key] not in (None, "") else None
        except ValueError:
            row[key] = None
    return row


def write_snapshot(dataset: str, rows: list[dict], meta: dict, folder: Path | None = None) -> Path:
    """Write a dataset snapshot atomically (a new file swapped in). Each row needs row_id, make, model, year."""
    folder = folder or snapshot_dir()
    if folder is None:
        raise ValueError("no snapshot directory")
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{dataset}.sqlite"
    tmp = folder / f".{dataset}.sqlite.tmp"
    if tmp.exists():
        tmp.unlink()
    with _LOCK:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("CREATE TABLE rows (row_id TEXT PRIMARY KEY, make TEXT, model TEXT, year INTEGER, data TEXT)")
            conn.execute("CREATE INDEX rows_make_year ON rows (make, year)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?, ?)", [
                (str(r["row_id"]), str(r.get("make") or "").strip().upper(), str(r.get("model") or ""),
                 _int(r.get("year")), json.dumps({k: v for k, v in r.items() if k != "row_id"}, ensure_ascii=False))
                for r in rows])
            conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                             [(k, json.dumps(v, ensure_ascii=False)) for k, v in {**meta, "rows": len(rows)}.items()])
            conn.commit()
        finally:
            conn.close()
        tmp.replace(final)
    return final


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def status() -> dict:
    """The Data page / MCP view of the committed snapshots: the manifest (build date, Action run URL) and per dataset its
    rows, years / files, absent columns, units, licence / attribution and whether the engine can read it (the sha256
    verified)."""
    from ..source_authority import dataset_policy

    man = manifest()
    entries = man.get("datasets") or {}
    rows = []
    for name, cfg in datasets().items():
        entry = entries.get(name) or {}
        policy = dataset_policy(cfg.get("policy_id") or name)
        problem = None
        if not cfg.get("identity_only"):
            problem = materialize(name)[1] if entry else "no_manifest_entry"
        rows.append({"dataset": name, "label": cfg.get("label"), "source_url": cfg.get("source_url"),
                     "market": cfg.get("market"), "routes": cfg.get("routes"),
                     "identity_only": bool(cfg.get("identity_only")), "policy": policy.get("policy"),
                     "licence": policy.get("licence"), "attribution": policy.get("attribution"),
                     "available": not cfg.get("identity_only") and problem is None, "problem": problem,
                     **{k: entry.get(k) for k in ("file", "bytes", "sha256", "built_at", "run_url", "rows", "years",
                                                  "files", "absent_columns", "column_units",
                                                  "unit_unknown_distribution", "last_file_year", "build_status",
                                                  "last_attempt")}})
    return {"repo_dir": str(repo_dir()), "manifest_built_at": man.get("built_at"), "run_url": man.get("run_url"),
            "config_version": config().get("version"), "datasets": rows}

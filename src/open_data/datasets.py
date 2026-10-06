"""Dataset configuration (data/open_datasets.json) and the local SQLite snapshots.

One file per dataset: <snapshot dir>/<dataset>.sqlite with

    rows(row_id TEXT PRIMARY KEY, make TEXT, model TEXT, year INTEGER, data TEXT)   data = the canonical columns (JSON)
    meta(key TEXT PRIMARY KEY, value TEXT)                                          built_at, source_url, rows, columns,
                                                                                    schema (the live header), licence

The engine only reads (a read-only URI connection); the builder writes a new file next to the old one and swaps it in
atomically, so a failed build keeps the previous snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_datasets.json"
ADMISSION_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_data_admission.json"
_STATE: dict[str, Any] = {"snapshot_dir": None}
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


def snapshot_path(dataset: str, folder: Path | None = None) -> Path | None:
    folder = folder or snapshot_dir()
    return folder / f"{dataset}.sqlite" if folder else None


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def snapshot_meta(dataset: str, folder: Path | None = None) -> dict:
    path = snapshot_path(dataset, folder)
    if path is None or not path.exists():
        return {}
    try:
        with _connect(path) as conn:
            return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
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
    except sqlite3.Error:
        return []
    return [{**json.loads(data), "row_id": row_id} for row_id, data in rows]


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

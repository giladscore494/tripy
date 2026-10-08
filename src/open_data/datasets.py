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

or, for a dataset with a `snapshot.shard_by: year` config (EEA), one shard per year: <dataset>/<year>.sqlite (an
oversized year split by make initial into <dataset>/<year>-A-L.sqlite and <year>-M-Z.sqlite), compressed to
data/open/<dataset>/<stem>.sqlite.gz and listed in the manifest entry's `shards` (file, year, part, status_used, rows,
bytes, sha256). A shard stores only what matching and offers read (`snapshot.columns`, the weighted median + min / max
of `snapshot.range_measures`, the median of `snapshot.median_measures`) in typed columns (INTEGER / REAL, never JSON
text), in the identity-key order, VACUUMed, with a meta free of timestamps: an unchanged year gives identical bytes.

    rows(row_id TEXT, make TEXT, model TEXT, year INTEGER, <column> TEXT | INTEGER | REAL, ...)
    meta(key TEXT PRIMARY KEY, value TEXT)                                          dataset, year, part, status_used,
                                                                                    rows, columns, source_url, licence

Each shard is verified by its own sha256: a missing or mismatched shard is skipped and reported (`skipped_shards`), the
others still load; a query reads only the shards of the years (and make parts) it asks for, so a match decompresses at
most the two shards of its year window.

The engine only reads (a read-only URI connection); the builder writes a new file next to the old one and swaps it in
atomically, so a failed build keeps the previous snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterable

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_datasets.json"
ADMISSION_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "open_data_admission.json"
REPO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "open"
MANIFEST_NAME = "manifest.json"
PROBE_NAME = "probe.json"
UNKNOWN_UNIT = "unknown"
_STATE: dict[str, Any] = {"snapshot_dir": None, "repo_dir": None, "temp_dir": None}
_MATERIALIZED: dict[str, tuple[str, Path | None, str | None]] = {}      # dataset | file -> (sha256, path, problem)
_VERIFIED: dict[tuple[str, int, int], str] = {}                          # (path, size, mtime_ns) -> sha256
SHARD_FILE = re.compile(r"^(\d{4})(?:-([A-Z]-[A-Z]))?\.sqlite$")
MONTH_FILE = re.compile(r"^(\d{4})-(\d{2})\.sqlite$")                   # D4: <dataset>/<YYYY-MM>.sqlite
SPLIT_PARTS = {"A-L": (None, "M"), "M-Z": ("M", None)}                  # make initial: [low, high)
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


DEFAULT_TEXT_IDENTITY = ("make", "model", "base_model", "type_approval", "variant", "version", "fuel", "fuel_mode")


def text_identity_keys() -> tuple[str, ...]:
    """D0: the text identity columns every dataset normalizes before its local grouping, in its shard / snapshot and
    in the match (data/open_datasets.json `text_identity_keys`)."""
    keys = config().get("text_identity_keys")
    return tuple(str(k) for k in keys) if isinstance(keys, list) and keys else DEFAULT_TEXT_IDENTITY


def norm_text(value: Any) -> str | None:
    """D0: a text identity value as stored and compared: Unicode NFKC, trimmed, internal whitespace collapsed to one
    space, upper case ('Petrol', 'PETROL  ' and 'PETROL' are one value; DISCODATA's GROUP BY treats them as one and
    returns an arbitrary spelling per group). None (or blank) stays None; a number is kept as it is."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = " ".join(unicodedata.normalize("NFKC", str(value)).split()).upper()
    return text or None


def normalize_identity(row: dict, keys: Iterable[str] | None = None) -> dict:
    """The row with its text identity columns normalized (in place; returned for chaining)."""
    for key in keys if keys is not None else text_identity_keys():
        if key in row:
            row[key] = norm_text(row[key])
    return row


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
    _VERIFIED.clear()


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


def _entry(dataset: str) -> dict:
    return (manifest().get("datasets") or {}).get(dataset) or {}


def _verified(source: Path, expected: str) -> bool:
    """The file's sha256 equals the manifest's (cached per path, size and mtime: a run hashes each file once)."""
    stat = source.stat()
    key = (str(source), stat.st_size, stat.st_mtime_ns)
    if key not in _VERIFIED:
        _VERIFIED[key] = sha256_file(source)
    return _VERIFIED[key] == expected


def _materialize_file(label: str, file: str, expected: str) -> tuple[Path | None, str | None]:
    """(decompressed path, problem) of one committed .sqlite.gz (a whole snapshot or one shard): verified against its
    sha256, decompressed once into the temp dir. problem: missing_file | sha256_mismatch | decompress_failed |
    insufficient_disk (the free-space guard: not remembered, retried)."""
    cached = _MATERIALIZED.get(f"{label}|{file}")
    if cached and cached[0] == expected and (cached[1] is None or cached[1].exists()):
        return cached[1], cached[2]
    source = repo_dir() / file
    if not source.is_file():
        _MATERIALIZED[f"{label}|{file}"] = (expected, None, "missing_file")
        return None, "missing_file"
    if not _verified(source, expected):
        log.error("open data %s: sha256 mismatch on %s (manifest %s): skipped", label, file, expected[:12])
        _MATERIALIZED[f"{label}|{file}"] = (expected, None, "sha256_mismatch")
        return None, "sha256_mismatch"
    stem = Path(file).name.removesuffix(".gz").removesuffix(".sqlite")
    target = _temp_dir() / f"{label}-{stem}-{expected[:16]}.sqlite"
    if not target.exists():
        if not _room_for(source, target.parent, f"open data {label} {file}"):
            return None, "insufficient_disk"            # not remembered: retried once there is room again
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(tmp, target)
        except (OSError, EOFError, gzip.BadGzipFile):
            log.error("open data %s: could not decompress %s", label, source, exc_info=True)
            _MATERIALIZED[f"{label}|{file}"] = (expected, None, "decompress_failed")
            return None, "decompress_failed"
    _MATERIALIZED[f"{label}|{file}"] = (expected, target, None)
    return target, None


def gzip_size(path: Path) -> int:
    """The uncompressed size a .gz states in its trailer (ISIZE, modulo 2**32); 0 when unreadable."""
    try:
        with open(path, "rb") as handle:
            handle.seek(-4, os.SEEK_END)
            return int.from_bytes(handle.read(4), "little")
    except OSError:
        return 0


def disk_refusals() -> int:
    """How many decompressions the free-space guard refused in this process (the facts API compares it before and
    after a match: a refusal there is `snapshots_unavailable`, never a smaller answer)."""
    return int(_STATE.get("disk_refusals") or 0)


def _room_for(source: Path, folder: Path, what: str) -> bool:
    """The existing free-space guard (storage.disk.check_free) applied to a decompression: refused when the temp dir's
    free space would drop under MIN_FREE_BYTES once the file is decompressed."""
    from ..storage.disk import MIN_FREE_BYTES, InsufficientDisk, check_free

    try:
        check_free(folder, what, MIN_FREE_BYTES + gzip_size(source))
    except InsufficientDisk:
        _STATE["disk_refusals"] = disk_refusals() + 1
        return False
    return True


def materialize(dataset: str) -> tuple[Path | None, str | None]:
    """(path of the decompressed snapshot, problem): the committed data/open/<dataset>.sqlite.gz verified against the
    manifest's sha256 and decompressed once into the temp dir. problem: no_manifest_entry | missing_file |
    sha256_mismatch | decompress_failed | sharded (a sharded dataset: `shard_files` / `shard_problems`)."""
    entry = _entry(dataset)
    if entry.get("shards"):
        return None, "sharded"
    if not entry.get("file") or not entry.get("sha256"):
        return None, "no_manifest_entry"
    return _materialize_file(dataset, str(entry["file"]), str(entry["sha256"]))


def part_covers(part: str | None, make: str) -> bool:
    """Whether a shard part ("A-L", "M-Z"; None = the whole year) holds this make (by its initial)."""
    if not part:
        return True
    if part not in SPLIT_PARTS:
        return True                             # an unknown part is read rather than missed
    low, high = SPLIT_PARTS[part]
    initial = str(make or "")[:1].upper()
    return (low is None or initial >= low) and (high is None or initial < high)


def _wanted(shard: dict, years: list[int] | None, makes: list[str] | None) -> bool:
    if years is not None and _int(shard.get("year")) not in years:
        return False
    return makes is None or any(part_covers(shard.get("part"), m) for m in makes)


def _folder_shards(dataset: str, folder: Path) -> list[dict]:
    """The plain shards of a snapshot folder (the build, the tests): <folder>/<dataset>/<year>[-<part>].sqlite."""
    out = []
    shard_dir = folder / dataset
    if not shard_dir.is_dir():
        return out
    for path in sorted(shard_dir.iterdir()):
        m = SHARD_FILE.match(path.name)
        if m and path.is_file():
            out.append({"file": f"{dataset}/{path.name}", "year": int(m.group(1)), "part": m.group(2), "path": path})
    return out


def shard_files(dataset: str, *, years: Iterable[int] | None = None, makes: Iterable[str] | None = None,
                folder: Path | None = None, report: dict | None = None) -> list[Path]:
    """The readable snapshot files of a dataset for a query: the single snapshot, or the shards of the asked years /
    make parts (each verified by its own sha256 and decompressed; a missing or mismatched shard is skipped and listed in
    report["skipped_shards"], the others still load)."""
    if history_spec(dataset):                  # D4: every readable month (the match selects its window itself)
        return [item["path"] for item in month_files(dataset, folder=folder, report=report)]
    years_list = sorted({int(y) for y in years}) if years is not None else None
    makes_list = sorted({str(m) for m in makes}) if makes is not None else None
    folder = folder or snapshot_dir()
    if folder:
        out = [folder / f"{dataset}.sqlite"] if (folder / f"{dataset}.sqlite").exists() else []
        return out + [s["path"] for s in _folder_shards(dataset, folder) if _wanted(s, years_list, makes_list)]
    entry = _entry(dataset)
    if not entry.get("shards"):
        path = materialize(dataset)[0]
        return [path] if path else []
    out = []
    for shard in entry["shards"]:
        if not isinstance(shard, dict) or not _wanted(shard, years_list, makes_list):
            continue
        if not shard.get("file") or not shard.get("sha256"):
            problem, path = "no_manifest_entry", None
        else:
            path, problem = _materialize_file(dataset, str(shard["file"]), str(shard["sha256"]))
        if path is None:
            if report is not None:
                report.setdefault("skipped_shards", []).append({"file": shard.get("file"), "year": shard.get("year"),
                                                                "part": shard.get("part"), "problem": problem})
            continue
        out.append(path)
    return out


def shard_problems(dataset: str) -> dict[str, str]:
    """{shard file: problem} of a sharded dataset's committed shards, checked by sha256 only (nothing decompressed)."""
    problems = {}
    for shard in _entry(dataset).get("shards") or []:
        if not isinstance(shard, dict):
            continue
        file = str(shard.get("file") or "")
        source = repo_dir() / file
        if not file or not shard.get("sha256"):
            problems[file or "?"] = "no_manifest_entry"
        elif not source.is_file():
            problems[file] = "missing_file"
        elif not _verified(source, str(shard["sha256"])):
            problems[file] = "sha256_mismatch"
    return problems


def snapshot_path(dataset: str, folder: Path | None = None) -> Path | None:
    """A plain snapshot folder when one is given (or set: the build, the tests), else the committed snapshot (None for a
    sharded dataset: `shard_files`)."""
    folder = folder or snapshot_dir()
    if folder:
        return folder / f"{dataset}.sqlite"
    return materialize(dataset)[0]


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def _read_meta(path: Path) -> dict:
    try:
        with _connect(path) as conn:
            return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except (sqlite3.Error, OSError, ValueError):
        return {}


def snapshot_meta(dataset: str, folder: Path | None = None) -> dict:
    """The snapshot's meta (+ size_bytes). A sharded dataset: its manifest entry (built_at, rows, shards, bytes) when
    committed, the shards' summed rows in a plain folder; nothing is decompressed."""
    folder = folder or snapshot_dir()
    entry = {} if folder else _entry(dataset)
    if entry.get("shards"):
        return {**{k: entry.get(k) for k in ("built_at", "run_url", "rows", "years", "absent_columns", "shards")
                   if entry.get(k) is not None}, "size_bytes": entry.get("bytes"), "sharded": True}
    shards = _folder_shards(dataset, folder) if folder else []
    if shards and not (folder / f"{dataset}.sqlite").exists():
        metas = [_read_meta(s["path"]) for s in shards]
        return {"rows": sum(int(m.get("rows") or 0) for m in metas), "sharded": True,
                "shards": [{k: v for k, v in s.items() if k != "path"} for s in shards],
                "size_bytes": sum(s["path"].stat().st_size for s in shards)}
    path = snapshot_path(dataset, folder)
    if path is None or not path.exists():
        return {}
    meta = _read_meta(path)
    if meta:
        meta["size_bytes"] = path.stat().st_size
    return meta


def available(dataset: str, folder: Path | None = None) -> bool:
    folder = folder or snapshot_dir()
    if folder:
        return (folder / f"{dataset}.sqlite").exists() or bool(_folder_shards(dataset, folder)) or \
            bool(history_spec(dataset) and _folder_months(dataset, folder))
    entry = _entry(dataset)
    if entry.get("shards"):
        problems = shard_problems(dataset)
        return any(isinstance(s, dict) and str(s.get("file") or "") not in problems for s in entry["shards"])
    path = materialize(dataset)[0]
    return bool(path and path.exists())


def query_rows(dataset: str, *, makes: Iterable[str], years: Iterable[int] | None = None,
               folder: Path | None = None, report: dict | None = None) -> list[dict]:
    """Rows of the snapshot whose make is one of `makes` (upper case) and, when given, whose year is in `years`.
    Each row: the canonical columns + `row_id`. Empty without a snapshot. A sharded dataset reads only the shards of
    those years and make parts (a skipped shard is listed in report["skipped_shards"])."""
    makes = sorted({" ".join(str(m).split()).upper() for m in makes if str(m or "").strip()})
    if not makes:
        return []
    years = sorted({int(y) for y in years}) if years else None
    out: list[dict] = []
    for path in shard_files(dataset, years=years, makes=makes, folder=folder, report=report):
        out += _query_file(dataset, path, makes, years)
    return out


def _query_file(dataset: str, path: Path, makes: list[str], years: list[int] | None) -> list[dict]:
    if not path.exists():
        return []
    try:
        with _connect(path) as conn:
            names = [r[1] for r in conn.execute("PRAGMA table_info(rows)")]
            typed = "data" not in names
            sql = f"SELECT {'*' if typed else 'row_id, data'} FROM rows WHERE make IN ({','.join('?' * len(makes))})"
            params: list[Any] = list(makes)
            if years:
                sql += f" AND year IN ({','.join('?' * len(years))})"
                params += years
            cursor = conn.execute(sql + " ORDER BY row_id", params)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            units = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta WHERE key = 'column_units'")}
    except sqlite3.Error:
        return []
    usable = unit_rules(dataset, units.get("column_units") or {})
    if typed:
        return [_apply_units({k: v for k, v in zip(columns, row) if v is not None}, usable) for row in rows]
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
                (str(r["row_id"]), " ".join(str(r.get("make") or "").split()).upper(), str(r.get("model") or ""),
                 _int(r.get("year")), json.dumps({k: v for k, v in r.items() if k != "row_id"}, ensure_ascii=False))
                for r in rows])
            conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                             [(k, json.dumps(v, ensure_ascii=False)) for k, v in {**meta, "rows": len(rows)}.items()])
            conn.commit()
        finally:
            conn.close()
        tmp.replace(final)
    return final


def shard_spec(dataset: str) -> dict | None:
    """{columns: {key: TEXT | INTEGER | REAL}, order: [identity keys]} of a dataset sharded by year (its `snapshot`
    config), else None. Stored: row_id, make, model, year, the `columns`, each `range_measures` key with its _min / _max
    and each `median_measures` key (the median only); nothing else of the built row."""
    snap = (datasets().get(dataset) or {}).get("snapshot") or {}
    if snap.get("shard_by") != "year":
        return None
    columns = {"row_id": "TEXT", "make": "TEXT", "model": "TEXT", "year": "INTEGER"}
    columns.update({k: str(v).upper() for k, v in (snap.get("columns") or {}).items() if not k.startswith("_")})
    for key in snap.get("range_measures") or []:
        columns.update({key: "REAL", f"{key}_min": "REAL", f"{key}_max": "REAL"})
    for key in snap.get("median_measures") or []:
        columns[key] = "REAL"
    return {"columns": columns, "order": [k for k in snap.get("identity_order") or [] if k in columns]}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = value
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if float(number).is_integer() else float(number)


def _typed(key: str, value: Any, kind: str, text_keys: Iterable[str] = ()) -> Any:
    if value is None or value == "":
        return None
    if kind in ("INTEGER", "REAL"):
        return _number(value)
    if key == "make" or key in text_keys:
        return norm_text(value)                     # D0: one spelling per value, whatever the source returned
    return str(value)


def _order_value(value: Any) -> tuple:
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, float(value), "")
    return (2, 0.0, str(value))


def _shard_stem(year: int, part: str | None = None) -> str:
    return f"{int(year)}" + (f"-{part}" if part else "")


def write_shard(dataset: str, rows: list[dict], meta: dict, *, year: int, part: str | None = None,
                folder: Path | None = None) -> Path:
    """Write one year's shard (<folder>/<dataset>/<year>[-<part>].sqlite) atomically: the `shard_spec` columns typed,
    the rows in the identity-key order, VACUUMed, the meta as given (no timestamp): the same rows give the same bytes."""
    spec = shard_spec(dataset)
    if spec is None:
        raise ValueError(f"{dataset} is not sharded")
    folder = folder or snapshot_dir()
    if folder is None:
        raise ValueError("no snapshot directory")
    shard_dir = folder / dataset
    shard_dir.mkdir(parents=True, exist_ok=True)
    stem = _shard_stem(year, part)
    final, tmp = shard_dir / f"{stem}.sqlite", shard_dir / f".{stem}.sqlite.tmp"
    for leftover in (tmp, tmp.with_name(tmp.name + "-journal")):
        if leftover.exists():
            leftover.unlink()
    columns = spec["columns"]
    text_keys = frozenset(text_identity_keys())
    values = [tuple(_typed(k, r.get(k), t, text_keys) for k, t in columns.items()) for r in rows]
    index = {k: i for i, k in enumerate(columns)}
    values.sort(key=lambda v: (tuple(_order_value(v[index[k]]) for k in spec["order"]),
                               _order_value(v[index["row_id"]])))
    info = {**meta, "dataset": dataset, "year": int(year), "part": part, "rows": len(values), "columns": list(columns)}
    with _LOCK:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute(f"CREATE TABLE rows ({', '.join(f'[{k}] {t}' for k, t in columns.items())})")
            conn.execute("CREATE INDEX rows_make ON rows (make)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(f"INSERT INTO rows VALUES ({','.join('?' * len(columns))})", values)
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                (k, json.dumps(info[k], ensure_ascii=False, sort_keys=True, default=str)) for k in sorted(info)])
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        tmp.replace(final)
    return final


def write_shards(dataset: str, rows: list[dict], meta: dict, *, status_used: dict | None = None,
                 folder: Path | None = None) -> list[dict]:
    """One shard per year of the rows ([{file, year, part, status_used, rows, path}]); a stale shard of this dataset in
    the folder is removed."""
    folder = folder or snapshot_dir()
    if folder is None:
        raise ValueError("no snapshot directory")
    by_year: dict[int, list[dict]] = {}
    for row in rows:
        year = _int(row.get("year"))
        if year is not None:
            by_year.setdefault(year, []).append(row)
    out = []
    for year in sorted(by_year):
        status = (status_used or {}).get(year)
        path = write_shard(dataset, by_year[year], {**meta, "status_used": status}, year=year, folder=folder)
        out.append({"file": f"{dataset}/{path.name}", "year": year, "part": None, "status_used": status,
                    "rows": len(by_year[year]), "path": path})
    written = {s["path"].name for s in out}
    for stale in _folder_shards(dataset, folder):
        if stale["path"].name not in written:
            stale["path"].unlink()
    return out


def read_shard(path: Path) -> tuple[list[dict], dict]:
    """(rows, meta) of a typed shard, in its stored order."""
    with _connect(path) as conn:
        cursor = conn.execute("SELECT * FROM rows ORDER BY rowid")
        columns = [d[0] for d in cursor.description]
        rows = [{k: v for k, v in zip(columns, row) if v is not None} for row in cursor.fetchall()]
    return rows, _read_meta(path)


def split_shard(dataset: str, path: Path) -> list[dict]:
    """The size guard: a year's shard split by make initial into <year>-A-L and <year>-M-Z (same folder, same meta);
    [{file, year, part, status_used, rows, path}] of the non-empty parts."""
    rows, meta = read_shard(path)
    year = int(meta.get("year"))
    base = {k: v for k, v in meta.items() if k not in ("dataset", "year", "part", "rows", "columns")}
    out = []
    for part in SPLIT_PARTS:
        subset = [r for r in rows if part_covers(part, r.get("make"))]
        if not subset:
            continue
        written = write_shard(dataset, subset, base, year=year, part=part, folder=path.parent.parent)
        out.append({"file": f"{dataset}/{written.name}", "year": year, "part": part,
                    "status_used": meta.get("status_used"), "rows": len(subset), "path": written})
    return out


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
        problem, skipped = None, None
        if not cfg.get("identity_only") and entry.get("shards"):
            skipped = shard_problems(name)          # sha256 only: nothing decompressed
            if len(skipped) == len(entry["shards"]):
                problem = "no_shard_readable"
        elif not cfg.get("identity_only"):
            problem = materialize(name)[1] if entry else "no_manifest_entry"
        rows.append({"dataset": name, "label": cfg.get("label"), "source_url": cfg.get("source_url"),
                     "market": cfg.get("market"), "routes": cfg.get("routes"),
                     "identity_only": bool(cfg.get("identity_only")), "policy": policy.get("policy"),
                     "licence": policy.get("licence"), "attribution": policy.get("attribution"),
                     "available": not cfg.get("identity_only") and problem is None, "problem": problem,
                     **({"skipped_shards": skipped} if skipped else {}),
                     **{k: entry.get(k) for k in ("file", "bytes", "sha256", "built_at", "run_url", "rows", "years",
                                                  "files", "absent_columns", "column_units",
                                                  "unit_unknown_distribution", "last_file_year", "build_status",
                                                  "shards", "last_attempt")}})
    return {"repo_dir": str(repo_dir()), "manifest_built_at": man.get("built_at"), "run_url": man.get("run_url"),
            "config_version": config().get("version"), "datasets": rows}


# --- D4: monthly history (a catalogue without a model year: ADEME) ---------------------------------------------------------

def history_spec(dataset: str) -> dict | None:
    """The dataset's `history` config when it keeps one snapshot per build month (`layout: monthly`), else None."""
    spec = (datasets().get(dataset) or {}).get("history") or {}
    return spec if spec.get("layout") == "monthly" else None


def month_of(value: Any) -> str | None:
    """'YYYY-MM' of a date / timestamp text, or None."""
    m = re.match(r"^\s*(\d{4})-(\d{2})", str(value or ""))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def write_month_snapshot(dataset: str, rows: list[dict], meta: dict, *, month: str, folder: Path | None = None) -> Path:
    """D4: one build month's snapshot (<folder>/<dataset>/<YYYY-MM>.sqlite), the layout of `write_snapshot` (rows with
    the canonical columns as JSON) written deterministically: the rows in row-id order, JSON with sorted keys, the
    meta as given (no timestamp), VACUUMed: the same rows give the same bytes."""
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise ValueError(f"not a month: {month!r}")
    folder = folder or snapshot_dir()
    if folder is None:
        raise ValueError("no snapshot directory")
    target_dir = folder / dataset
    target_dir.mkdir(parents=True, exist_ok=True)
    final, tmp = target_dir / f"{month}.sqlite", target_dir / f".{month}.sqlite.tmp"
    for leftover in (tmp, tmp.with_name(tmp.name + "-journal")):
        if leftover.exists():
            leftover.unlink()
    values = sorted(((str(r["row_id"]), " ".join(str(r.get("make") or "").split()).upper(), str(r.get("model") or ""),
                      _int(r.get("year")), json.dumps({k: v for k, v in r.items() if k != "row_id"},
                                                      ensure_ascii=False, sort_keys=True, default=str))
                     for r in rows), key=lambda v: v[0])
    info = {**meta, "dataset": dataset, "month": month, "rows": len(values)}
    with _LOCK:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("CREATE TABLE rows (row_id TEXT PRIMARY KEY, make TEXT, model TEXT, year INTEGER, data TEXT)")
            conn.execute("CREATE INDEX rows_make_year ON rows (make, year)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?)", values)
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                (k, json.dumps(info[k], ensure_ascii=False, sort_keys=True, default=str)) for k in sorted(info)])
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        tmp.replace(final)
    return final


def _folder_months(dataset: str, folder: Path) -> list[Path]:
    month_dir = folder / dataset
    return sorted(p for p in month_dir.iterdir() if MONTH_FILE.match(p.name) and p.is_file()) \
        if month_dir.is_dir() else []


def _month_item(month: str | None, catalogue_date: Any, path: Path, basis: str, legacy: bool = False) -> dict | None:
    date = str(catalogue_date) if catalogue_date else None
    month = month or month_of(date)
    year = _int((date or month or "")[:4])
    if not month or year is None:
        return None
    return {"month": month, "catalogue_date": date or month, "year": year, "path": path, "basis": basis,
            "legacy": legacy}


def month_files(dataset: str, *, folder: Path | None = None, report: dict | None = None) -> list[dict]:
    """D4: [{month, catalogue_date, year (the catalogue year), path, basis, legacy}] of a monthly dataset's readable
    snapshots, newest month first. The monthly snapshots, plus the single-file snapshot of the former layout as the
    month of its catalogue date (else its built_at) unless a monthly snapshot of that month exists. A plain folder
    (the build, the tests) or the committed files (each verified by its sha256; a missing or mismatched file is
    skipped and listed in report["skipped_shards"])."""
    folder = folder or snapshot_dir()
    items: list[dict] = []
    legacy: dict | None = None
    if folder:
        for path in _folder_months(dataset, folder):
            meta = _read_meta(path)
            item = _month_item(path.name[:7], meta.get("catalogue_date"), path,
                               "catalogue_date" if meta.get("catalogue_date") else "month")
            if item:
                items.append(item)
        single = folder / f"{dataset}.sqlite"
        if single.exists():
            meta = _read_meta(single)
            date = meta.get("catalogue_date") or meta.get("built_at")
            legacy = _month_item(None, date, single, "catalogue_date" if meta.get("catalogue_date") else "built_at",
                                 legacy=True)
    else:
        entry = _entry(dataset)
        for shard in entry.get("shards") or []:
            if not isinstance(shard, dict) or not shard.get("month"):
                continue
            path, problem = _materialize_file(dataset, str(shard.get("file") or ""), str(shard.get("sha256") or ""))
            if path is None:
                if report is not None:
                    report.setdefault("skipped_shards", []).append({"file": shard.get("file"),
                                                                    "month": shard.get("month"), "problem": problem})
                continue
            item = _month_item(str(shard["month"]), shard.get("catalogue_date"), path,
                               "catalogue_date" if shard.get("catalogue_date") else "month")
            if item:
                items.append(item)
        old = entry.get("legacy") or (entry if entry.get("file") else None)
        if isinstance(old, dict) and old.get("file") and old.get("sha256"):
            path, problem = _materialize_file(dataset, str(old["file"]), str(old["sha256"]))
            if path is not None:
                date = old.get("catalogue_date") or old.get("built_at")
                legacy = _month_item(None, date, path, "catalogue_date" if old.get("catalogue_date") else "built_at",
                                     legacy=True)
            elif report is not None:
                report.setdefault("skipped_shards", []).append({"file": old.get("file"), "problem": problem})
    if legacy and all(i["month"] != legacy["month"] for i in items):
        items.append(legacy)
    return sorted(items, key=lambda i: i["month"], reverse=True)


def query_files(dataset: str, paths: Iterable[Path], makes: Iterable[str]) -> list[list[dict]]:
    """The rows of `makes` (as `query_rows` reads them) per snapshot file, in the order given."""
    makes = sorted({" ".join(str(m).split()).upper() for m in makes if str(m or "").strip()})
    return [_query_file(dataset, Path(p), makes, None) if makes else [] for p in paths]

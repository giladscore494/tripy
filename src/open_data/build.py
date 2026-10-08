"""D-1: the snapshot builders. One dataset at a time, into a work folder's <dataset>.sqlite; they run in the
build-open-data GitHub Action (scripts/build_open_data.py compresses each snapshot into data/open/<dataset>.sqlite.gz
and records it in data/open/manifest.json), never on the server.

Every build reads the LIVE header first and resolves the column map of data/open_datasets.json against it
(`resolve_map`): spellings are matched case-insensitively, whitespace trimmed; a `required` key (the identity keys) whose
spellings are all absent stops that table / file (`schema_mismatch`); an absent optional key is recorded
(`absent_columns` in the snapshot meta) and its offers are simply not produced; two different live columns matching one
key are `ambiguous` (stop). Nothing is guessed, and a failed build keeps the previous snapshot. Downloads go through
the production source policy (a dataset whose domain is not `allowed` is never downloaded) with the fetch tools' user
agent. Each dataset's `builder`:

    discodata    EEA: `SELECT TOP 1 *` for the year / status columns, `SELECT DISTINCT year, status`, then per existing
                 year (final rows preferred, provisional rows only for a year without final ones) the year's own
                 schema (`TOP 1` of that year), the make values present that year (`SELECT DISTINCT Mk`; kept when
                 their spelling is an alias spelling) and per (year, make spelling) one short query (E1: identity and
                 measurement columns + SUM(R) AS r, grouped by the same columns, no other alias, no MIN / MAX / AVG, no
                 ORDER BY; its URL-encoded query= value <= EEA_MAX_QUERY_BYTES, else split in two on the measurement
                 columns, else `query_too_long`); the runner computes the registration-weighted median / min / max per
                 identity key; a failing spelling stops only itself, a year whose spellings all fail stops only itself.
                 Y1 / Y2: from `years_from` (2010); a year up to `final_only_through_year` (2021) is built from its
                 final rows only (none: `no_final_rows`, not built, reported); each year's header resolves through the
                 column map plus that year's `column_aliases_by_year` (`column_sources_by_year` replaces a key's
                 spellings for a year: co2_nedc = E (g/km) in 2010-2016), and the grouping uses only the keys it maps (a
                 year without Ewltp groups by co2_nedc); each year's report records its live header, the mapping and
                 the Y4 audit statistics (src/open_data/audit.year_stats: computed here, where the registrations are
                 still known). K3: the years from `csv_years.from_year` that DISCODATA does not have ([latest] ends at
                 2022), and an earlier year DISCODATA lacks, come from the EEA datahub's downloadable CSV
                 (`build_eea_csv`: the records read from the datahub API, the zip streamed to the runner disk and
                 grouped locally exactly like the DISCODATA rows)
    data_fair    ADEME: the raw CSV and the field schema; the unit of every column with a `unit_from_description` rule
                 is read from the schema field's description ("Puissance en kW"); a column without a stated unit is
                 stored raw with unit `unknown` (its p5 / p50 / p95 recorded) and yields nothing until a
                 `unit_overrides` entry confirms its unit
    csv          EPA: the CSV files of `download_urls` (one zip)
    ckan_groups  NRCan: the package's CSV resources in the dataset's language, each assigned to the first
                 `resource_groups` entry whose pattern its file name contains (excluded patterns are never read); each
                 group has its own map, a group without a map reports the live header (`map_pending`), and a file that
                 fails stops only itself
    ckan_files   CVS: the package's resources matching `resource_pattern`; the data dictionary must state every mapped
                 code and its unit (`dictionary_check`), else the build stops with the dictionary's rows

A dataset with a `snapshot.shard_by: year` config (EEA) is written as one shard per year (`ds.write_shards`: only the
columns matching and offers read, typed, in the identity-key order, no timestamp in the file).

After the build, `compact` keeps only what matching and offers use (the dataset's `compaction` rules): the rows whose
make spelling normalizes to a canonical make of data/make_canonical.json (a model-gated make only with a catalog model
match), the year window, and the grouping; it records the kept makes and their spellings.

`progress(text)` (optional) is called with the current stage.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse

from . import datasets as ds

Fetcher = Callable[[str], bytes]
Progress = Callable[[str], None]
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
EEA_PAGE = 50_000
MAX_EEA_PAGES = 200
EEA_MAX_QUERY_BYTES = 1800                   # E1: the URL-encoded query= value (IIS 404.15 above 2,048)
PLAIN_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
COMMA_NUMBER = re.compile(r"^-?\d+,\d+$")


class BuildStopped(RuntimeError):
    """The build (or one table / file / year) stopped with a report (schema mismatch, no download URL, a policy-blocked
    source, an unstated unit)."""

    def __init__(self, reason: str, **report: Any):
        super().__init__(reason)
        self.reason, self.report = reason, report


def _noop(_: str) -> None:
    return None


def default_fetcher(session=None, timeout_s: float = 120.0) -> Fetcher:
    import requests

    from ..source_authority import fetch_allowed
    from ..tools.fetch import USER_AGENT

    http = session or requests.Session()

    def fetch(url: str) -> bytes:
        if not fetch_allowed(url):
            raise BuildStopped("policy_blocked", url=url)
        resp = http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=(15, timeout_s), stream=True)
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(1 << 16):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise BuildStopped("download_too_large", url=url, bytes=size)
            chunks.append(chunk)
        return b"".join(chunks)

    def fetch_json(url: str) -> bytes:
        """The same, asking for JSON (the EEA datahub API answers XML to */*)."""
        if not fetch_allowed(url):
            raise BuildStopped("policy_blocked", url=url)
        resp = http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=(15, timeout_s))
        resp.raise_for_status()
        return resp.content

    def fetch_prefix(url: str, limit: int = 4 << 20) -> bytes:
        """The first `limit` bytes of a URL (streamed, the connection closed after them): a CSV header, never the
        file (D2: the probe reads each datahub CSV's header without downloading it)."""
        if not fetch_allowed(url):
            raise BuildStopped("policy_blocked", url=url)
        chunks, size = [], 0
        with http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=(15, timeout_s),
                      stream=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 16):
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit:
                    break
        return b"".join(chunks)[:limit]
    fetch.json = fetch_json
    fetch.prefix = fetch_prefix
    return fetch


# --- column maps -----------------------------------------------------------------------------------------------------------

def _norm(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _present(header: list[str], spellings: list[str]) -> list[str]:
    wanted = {_norm(s) for s in spellings or []}
    return [h for h in header if _norm(h) in wanted]


def resolve_map(header: list[str], columns: dict[str, dict]) -> dict:
    """{mapping: {key: live column}, missing: [...], ambiguous: [...], absent: [...]}. `missing` holds the required keys
    whose spellings (and fallback spellings) are all absent; `absent` the optional ones. A key without `required` is
    required (fail closed)."""
    mapping, missing, ambiguous, absent = {}, [], [], []
    for key, spec in columns.items():
        if not isinstance(spec, dict):
            continue
        found = _present(header, spec.get("source") or [])
        if not found:
            found = _present(header, spec.get("fallback") or [])
        if len(found) == 1:
            mapping[key] = found[0]
        elif found:
            ambiguous.append({"key": key, "present": found})
        elif spec.get("required", True):
            missing.append({"key": key, "expected": (spec.get("source") or []) + (spec.get("fallback") or [])})
        else:
            absent.append(key)
    return {"mapping": mapping, "missing": missing, "ambiguous": ambiguous, "absent": absent}


def resolve_columns(header: list[str], columns: dict[str, dict]) -> dict[str, str]:
    """{canonical key: the live source column}, or BuildStopped(schema_mismatch) naming every required key it cannot
    resolve and every ambiguous one (absent optional keys are left out of the mapping)."""
    resolved = resolve_map(header, columns)
    if resolved["missing"] or resolved["ambiguous"]:
        raise BuildStopped("schema_mismatch", missing=resolved["missing"], ambiguous=resolved["ambiguous"],
                           absent=resolved["absent"], live_header=list(header)[:200])
    return resolved["mapping"]


# --- CSV -------------------------------------------------------------------------------------------------------------------

def _decode(body: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _csv_tables(body: bytes) -> list[tuple[str, list[dict], list[str]]]:
    """(name, rows, header) of a CSV body or of every CSV inside a zip."""
    if body[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".csv"):
                    out += [(name, rows, header) for _, rows, header in _csv_tables(archive.read(name))]
        return out
    text = _decode(body)
    if not text.strip():
        return [("csv", [], [])]
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = [h for h in (reader.fieldnames or []) if h is not None]
    return [("csv", list(reader), header)]


def _number_value(value: Any, scale: float, decimal_comma: bool, counts: dict, key: str) -> Any:
    """A unit column's value: plain numbers stay as stated (scale 1), a decimal-comma number is read only when the
    dataset declares `decimal_comma` (else None and counted: never guessed)."""
    if value is None or isinstance(value, (int, float)):
        return value * scale if isinstance(value, (int, float)) and scale != 1 else value
    text = str(value).strip()
    if not text:
        return None
    if COMMA_NUMBER.match(text):
        if not decimal_comma:
            counts[key] = counts.get(key, 0) + 1
            return None
        text = text.replace(",", ".")
    if scale == 1:
        return text
    if PLAIN_NUMBER.match(text):
        return float(text) * scale
    return None


def _canonical(rows: list[dict], mapping: dict[str, str], columns: dict, keep_makes: set[str] | None,
               id_prefix: str = "", scales: dict | None = None, decimal_comma: bool = False,
               extra: dict | None = None) -> tuple[list[dict], dict]:
    out, unreadable = [], {}
    scales = scales or {}
    for n, row in enumerate(rows):
        item = {}
        for key, column in mapping.items():
            value = row.get(column)
            value = None if value in ("",) else value
            if (columns.get(key) or {}).get("unit") and key not in ("row_id", "year"):
                value = _number_value(value, float(scales.get(key, 1)), decimal_comma, unreadable, key)
            item[key] = value
        make = str(item.get("make") or "").strip().upper()
        if keep_makes is not None and make not in keep_makes:
            continue
        own_id = item.pop("row_id", None)
        item["row_id"] = f"{id_prefix}{own_id}" if own_id not in (None, "") else f"{id_prefix}{n + 1}"
        if extra:
            item.update(extra)
        out.append(item)
    return out, unreadable


def _build_file(url: str, fetch: Fetcher, columns: dict | None, *, keep_makes=None, id_prefix: str = "",
                scales: dict | None = None, decimal_comma: bool = False, extra: dict | None = None,
                body: bytes | None = None) -> tuple[list[dict], dict]:
    """(rows, report) of one CSV file (or zip). The report: url, status built | stopped | map_pending | failed, rows,
    absent_columns, live_header (when stopped), missing / ambiguous."""
    report: dict[str, Any] = {"url": url}
    try:
        tables = _csv_tables(body if body is not None else fetch(url))
    except BuildStopped as stop:
        return [], {**report, "status": "stopped", "reason": stop.reason, **stop.report}
    except Exception as exc:  # noqa: BLE001 - one file never costs the others
        return [], {**report, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    rows: list[dict] = []
    absent: set[str] = set()
    unreadable: dict[str, int] = {}
    for t, (name, table, header) in enumerate(tables):
        if not header:
            continue
        if columns is None:
            return [], {**report, "status": "map_pending", "live_header": header[:200],
                        "reason": "no map for this file's group: write it from this live header"}
        resolved = resolve_map(header, columns)
        if resolved["missing"] or resolved["ambiguous"]:
            return [], {**report, "status": "stopped", "reason": "schema_mismatch", "table": name,
                        "missing": resolved["missing"], "ambiguous": resolved["ambiguous"],
                        "absent_columns": resolved["absent"], "live_header": header[:200]}
        absent |= set(resolved["absent"])
        built, bad = _canonical(table, resolved["mapping"], columns, keep_makes, f"{id_prefix}{t}-" if len(tables) > 1
                                else id_prefix, scales, decimal_comma, extra)
        for key, count in bad.items():
            unreadable[key] = unreadable.get(key, 0) + count
        rows += built
        report.setdefault("live_header", header[:200])
    if not report.get("live_header"):
        return [], {**report, "status": "stopped", "reason": "no_rows"}
    report.update(status="built", rows=len(rows), absent_columns=sorted(absent))
    if unreadable:
        report["decimal_comma_values"] = unreadable
    return rows, report


def build_csv_dataset(dataset: str, urls: list[str], fetch: Fetcher, *, keep_makes: set[str] | None = None,
                      progress: Progress = _noop) -> dict:
    """Every URL is one file of the dataset's single map; any file that stops stops the dataset (one-map datasets)."""
    cfg = ds.datasets()[dataset]
    columns = cfg.get("columns") or {}
    rows: list[dict] = []
    files = []
    for u, url in enumerate(urls):
        progress(f"file {u + 1}/{len(urls)}: {url.rsplit('/', 1)[-1]}")
        built, report = _build_file(url, fetch, columns, keep_makes=keep_makes, id_prefix=f"{dataset}-{u}-"
                                    if u else "", decimal_comma=bool(cfg.get("decimal_comma")))
        files.append(report)
        if report["status"] == "failed":
            raise RuntimeError(report.get("error") or "download failed")
        if report["status"] != "built":
            raise BuildStopped(report.get("reason") or report["status"], files=files, **{
                k: report[k] for k in ("missing", "ambiguous", "live_header") if k in report})
        rows += built
    if not files:
        raise BuildStopped("no_download_url", note=f"data/open_datasets.json {dataset}.download_urls is empty")
    return {"rows": rows, "schema": files[0].get("live_header"), "urls": urls, "files": files,
            "absent_columns": sorted({c for f in files for c in f.get("absent_columns") or []})}


def known_makes() -> set[str]:
    """Every make spelling of the aliases (data/open_data_make_aliases.json, generated for the whole catalog by
    scripts/build_make_aliases.py, merged with the reviewed identity_vocabulary entries)."""
    from .makes import aliases

    return {m for makes in aliases().values() for m in makes}


# --- EEA (DISCODATA) -------------------------------------------------------------------------------------------------------

class EeaQueryError(RuntimeError):
    """DISCODATA answered without results (an error message, or no `results` key): never "a year with no cars"."""


def _q(column: str) -> str:
    return "[" + column.replace("]", "]]") + "]"


def eea_response(body: bytes) -> list[dict]:
    """The rows of a DISCODATA response; EeaQueryError (message kept, <= 500 chars) when it carries an error or no
    `results` key."""
    text = _decode(body)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise EeaQueryError(f"not JSON: {text[:400]}") from exc
    if not isinstance(data, dict):
        raise EeaQueryError(f"unexpected response: {text[:400]}")
    errors = {k: v for k, v in data.items() if "error" in k.lower() and v}
    if errors:
        raise EeaQueryError(json.dumps(errors, ensure_ascii=False, default=str)[:500])
    for key in ("results", "Results"):
        if key in data:
            rows = data[key]
            if not isinstance(rows, list):
                raise EeaQueryError(f"`{key}` is not a list: {str(rows)[:400]}")
            return rows
    raise EeaQueryError(f"no `results` in the response: {text[:450]}")


class EeaQueryTooLong(EeaQueryError):
    """E1: a DISCODATA query whose URL-encoded `query=` value is over EEA_MAX_QUERY_BYTES (never sent: IIS answers 404
    above its 2,048-byte query-string limit)."""

    def __init__(self, length: int, limit: int):
        super().__init__(f"query_too_long: {length} bytes > {limit}")
        self.reason, self.length, self.limit = "query_too_long", length, limit


def eea_query_bytes(query: str) -> int:
    """The length of the URL-encoded `query=` value (what the server's query-string limit counts)."""
    return len(quote(query))


def assert_query_length(query: str, limit: int | None = None) -> int:
    """The query's encoded length, checked before it is sent (EeaQueryTooLong over the limit)."""
    limit = EEA_MAX_QUERY_BYTES if limit is None else limit
    length = eea_query_bytes(query)
    if length > limit:
        raise EeaQueryTooLong(length, limit)
    return length


def _sql_text(value: str) -> str:
    text = "'" + str(value).replace("'", "''") + "'"
    return text if text.isascii() else "N" + text


def eea_query(table: str, columns: list[str], registrations: str | None, where: str, make_column: str,
              make_value: str) -> str:
    """E1: one (year, make spelling): SELECT the columns + SUM(TRY_CAST([R] AS bigint)) AS r, GROUP BY the same
    columns; no alias but `r`, no MIN / MAX / AVG, no ORDER BY."""
    listed = ",".join(_q(c) for c in columns)
    total = f",SUM(TRY_CAST({_q(registrations)} AS bigint)) AS r" if registrations else ""
    return (f"SELECT {listed}{total} FROM {table} WHERE {where} AND {_q(make_column)}={_sql_text(make_value)} "
            f"GROUP BY {listed}")


def eea_query_plan(table: str, mapping: dict[str, str], cfg: dict, registrations: str | None, where: str,
                   make_value: str, limit: int | None = None) -> list[tuple[list[str], str]]:
    """[(measure keys, query)]: one query with every measurement column; when that is over the limit, the measurement
    columns split into two queries (joined locally on the identity key). Every query is checked against the limit
    (EeaQueryTooLong when even a split one is over it)."""
    limit = EEA_MAX_QUERY_BYTES if limit is None else limit
    keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
    measures = [k for k in cfg.get("measure_keys") or [] if k in mapping]

    def build(part: list[str]) -> str:
        return eea_query(table, [mapping[k] for k in keys + part], registrations, where, mapping["make"], make_value)
    whole = build(measures)
    if eea_query_bytes(whole) <= limit or len(measures) < 2:
        assert_query_length(whole, limit)
        return [(measures, whole)]
    half = (len(measures) + 1) // 2
    plan = [(part, build(part)) for part in (measures[:half], measures[half:])]
    for _, query in plan:
        assert_query_length(query, limit)
    return plan


def eea_aggregate(parts: list[tuple[list[str], list[dict]]], mapping: dict[str, str], cfg: dict,
                  status: str) -> list[dict]:
    """G2 locally: the server rows of one (year, make) (distinct configurations with a registration count `r`) ->
    one row per identity key with the registration-weighted median, min and max of each measurement and the summed
    registrations; the parts of a split query are joined on the identity key. D0: the identity values are normalized
    first (text: NFKC, trimmed, whitespace collapsed, upper case; numbers: one numeric type), so the spellings
    DISCODATA's case- and trailing-space-insensitive GROUP BY returns arbitrarily ('Petrol' / 'PETROL  ') are one
    configuration: summed registrations, the weighted median / min / max recomputed from all their inputs."""
    keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
    text_keys = [k for k in keys if k in ds.text_identity_keys()]
    numeric = {k for k, kind in ((ds.shard_spec("eea_co2_cars") or {}).get("columns") or {}).items()
               if kind in ("INTEGER", "REAL")} & set(keys)
    merged: dict[tuple, dict] = {}
    for p, (measures, server_rows) in enumerate(parts):
        rows = []
        for raw in server_rows:
            row = {**{k: raw.get(mapping[k]) for k in keys + measures}, "r": raw.get("r")}
            for k in text_keys:
                row[k] = ds.norm_text(row[k])
            for k in numeric:
                number = ds._number(row[k]) if row[k] not in (None, "") else None
                row[k] = number if number is not None or row[k] in (None, "") else row[k]
            rows.append(row)
        for item in group_configurations(rows, keys, measures, "r"):
            ident = tuple(str(item.get(k)) for k in keys)
            target = merged.setdefault(ident, {k: item.get(k) for k in keys})
            for m in measures:
                for suffix in ("", "_min", "_max"):
                    if f"{m}{suffix}" in item:
                        target[f"{m}{suffix}"] = item[f"{m}{suffix}"]
            if p == 0 or "registrations" not in target:
                target["registrations"] = ds._int(item.get("r"))
    out = []
    for ident in sorted(merged):
        item = merged[ident]
        item["status"] = status
        out.append(item)
    return out


def _by_year(table: dict | None, year: int | None) -> dict:
    """The entries of a per-year table (`column_aliases_by_year`, `column_sources_by_year`) for one year: its own key
    ("2014") merged over every range key that covers it ("2010-2016")."""
    out: dict = {}
    if year is None:
        return out
    for key, entry in sorted((table or {}).items()):
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        low, _, high = key.partition("-")
        if low.isdigit() and (high.isdigit() if high else True) and int(low) <= int(year) <= int(high or low):
            out.update({k: v for k, v in entry.items() if not k.startswith("_")})
    return out


def eea_year_columns(cfg: dict, year: int | None, *, csv_file: bool = False) -> dict:
    """Y2: the column map of one year: `columns` plus that year's `column_aliases_by_year` spellings (and, for the
    datahub CSV, `csv_years.column_aliases`), each added to the key's accepted `source` spellings; then that year's
    `column_sources_by_year` REPLACE a key's accepted spellings (source and fallback) for that year. A replacement is
    for a year whose header carries the key's usual spelling empty next to the column that holds its values (2010-2016:
    Enedc empty, E holds the NEDC CO2): an added alias would match both columns and stop the year as `ambiguous`."""
    aliases: dict[str, list[str]] = {}
    sources = [((cfg.get("csv_years") or {}).get("column_aliases") or {}) if csv_file else {},
               _by_year(cfg.get("column_aliases_by_year"), year)]
    for source in sources:
        for key, spellings in source.items():
            if not key.startswith("_") and isinstance(spellings, list):
                aliases.setdefault(key, []).extend(str(s) for s in spellings)
    replaced = {k: [str(s) for s in v] for k, v in _by_year(cfg.get("column_sources_by_year"), year).items()
                if isinstance(v, list) and v}
    columns = {}
    for key, spec in (cfg.get("columns") or {}).items():
        spec = dict(spec)
        if key in aliases:
            known = list(spec.get("source") or [])
            spec["source"] = known + [a for a in aliases[key] if a not in known]
        if key in replaced:
            spec["source"] = replaced[key]
            spec.pop("fallback", None)
        columns[key] = spec
    return columns


def accepted_status(cfg: dict, year: int, present: set[str]) -> tuple[str | None, str | None]:
    """(the status a year is built from, the reason when none): final rows preferred, provisional rows only for a year
    after `final_only_through_year` without final rows (Y1: up to it, final rows only: `no_final_rows`)."""
    through = cfg.get("final_only_through_year")
    final_only = through is not None and int(year) <= int(through)
    preference = ["F"] if final_only else (cfg.get("status_preference") or ["F", "P"])
    status = next((s for s in preference if s in present), None)
    if status is not None:
        return status, None
    return None, "no_final_rows" if final_only else "no_preferred_status"


def build_eea(fetch: Fetcher, *, years: list[int] | None = None, progress: Progress = _noop) -> dict:
    """Year discovery, then per year its own schema, the make spellings present that year, and per (year, make
    spelling) the E1 query (or its two-part split); a query error is an error, never a year without cars, and a failing
    spelling stops only itself."""
    from .audit import empty_columns, year_stats
    from .makes import canonical_of, row_filter, spelling

    cfg = ds.datasets()["eea_co2_cars"]
    base, table = cfg["source_url"], cfg["table"]
    columns = cfg["columns"]

    def sql(query: str, page: int = 1) -> list[dict]:
        assert_query_length(query)
        return eea_response(fetch(f"{base}?query={quote(query)}&p={page}&nrOfHits={EEA_PAGE}"))

    def sql_all(query: str) -> list[dict]:
        """Every page of a query (DISCODATA pages by p / nrOfHits): a full page asks for the next one."""
        out, page = [], 1
        while True:
            chunk = sql(query, page)
            out += chunk
            if len(chunk) < EEA_PAGE or page >= MAX_EEA_PAGES:
                return out
            page += 1

    progress("schema")
    sample = sql(f"SELECT TOP 1 * FROM {table}")
    header = list(sample[0].keys()) if sample else []
    head = resolve_columns(header, {"year": columns["year"], "status": cfg["status_column"]})
    year_col, status_col = head["year"], head["status"]
    progress("year discovery")
    statuses: dict[int, set[str]] = {}
    for pair in sql_all(f"SELECT DISTINCT {_q(year_col)}, {_q(status_col)} FROM {table}"):
        year = ds._int(pair.get(year_col))
        if year is not None:
            statuses.setdefault(year, set()).add(str(pair.get(status_col) or "").strip())
    floor = int(cfg.get("years_from") or 0)
    wanted = sorted(y for y in statuses if y >= floor and (years is None or y in years))
    makes = sorted(known_makes())
    if not makes:
        raise BuildStopped("no_makes", note="no open-data make aliases (data/make_canonical.json)")
    keep = row_filter()
    rows: list[dict] = []
    reports: list[dict] = []
    absent_by_year: dict[str, list[str]] = {}
    for year in ([y for y in (years or []) if y not in statuses] if years else []):
        reports.append({"year": year, "status": "skipped", "reason": "no_rows"})
    for year in wanted:
        status, why = accepted_status(cfg, year, statuses[year])
        if status is None:
            reports.append({"year": year, "status": "skipped", "reason": why, "statuses": sorted(statuses[year])})
            continue
        year_columns = eea_year_columns(cfg, year)
        progress(f"year {year} (status {status})")
        where = f"{_q(year_col)} = {int(year)} AND {_q(status_col)} = '{status}'"
        try:
            sample = sql(f"SELECT TOP 1 * FROM {table} WHERE {where}")
        except EeaQueryError as exc:
            reports.append({"year": year, "status": "failed", "status_used": status, "stage": "schema",
                            "error": str(exc)[:500]})
            continue
        if not sample:
            reports.append({"year": year, "status": "skipped", "reason": "no_rows", "status_used": status})
            continue
        year_header = list(sample[0].keys())
        resolved = resolve_map(year_header, year_columns)
        if resolved["missing"] or resolved["ambiguous"]:
            reports.append({"year": year, "status": "stopped", "reason": "schema_mismatch", "status_used": status,
                            "missing": resolved["missing"], "ambiguous": resolved["ambiguous"],
                            "live_header": year_header[:200]})
            continue
        mapping = resolved["mapping"]
        registrations = resolve_map(year_header, {"registrations": cfg["registrations_column"]})["mapping"].get(
            "registrations")
        report: dict[str, Any] = {"year": year, "status_used": status, "absent_columns": resolved["absent"],
                                  "registrations_column": registrations, "mode": "per_make",
                                  "live_header": year_header[:200], "mapping": mapping}
        # the raw make values present this year whose spelling is an alias spelling or normalizes to a canonical make
        # (the server compares the exact value); without that list, the alias spellings themselves
        wanted_makes = set(makes)
        try:
            present = [str(r.get(mapping["make"])) for r in
                       sql_all(f"SELECT DISTINCT {_q(mapping['make'])} FROM {table} WHERE {where}")
                       if r.get(mapping["make"]) not in (None, "")]
            values = sorted({v for v in present if spelling(v) in wanted_makes or canonical_of(v)})
            report["spelling_source"] = "year_distinct"
        except EeaQueryError as exc:
            values = makes
            report.update(spelling_source="aliases", distinct_error=str(exc)[:300])
        built: list[dict] = []
        errors: dict[str, str] = {}
        sizes: list[int] = []
        queries = source_rows = 0
        for value in values:
            progress(f"year {year} (status {status}): {value}")
            try:
                plan = eea_query_plan(table, mapping, cfg, registrations, where, value)
                parts = []
                for measures, query in plan:
                    sizes.append(eea_query_bytes(query))
                    queries += 1
                    parts.append((measures, sql_all(query)))
            except EeaQueryError as exc:
                errors[spelling(value)] = str(exc)[:500]
                continue
            source_rows += len(parts[0][1]) if parts else 0
            if len(plan) > 1:
                report["split"] = True
            found = [r for r in eea_aggregate(parts, mapping, cfg, status)
                     if keep(r.get("make"), " ".join(str(r.get(k) or "") for k in ("model", "variant", "version")))]
            for item in found:
                item["make"] = spelling(item.get("make"))
            built += found
        report.update(queries=queries, spellings=len(values), max_query_bytes=max(sizes) if sizes else None,
                      source_rows=source_rows)
        if errors:
            report["make_errors"] = errors
        if values and len(errors) == len(values):
            reports.append({**report, "status": "failed", "error": next(iter(errors.values()))})
            continue
        by_make: dict[str, int] = {}
        for n, item in enumerate(built):
            item["row_id"] = f"eea-{year}-{status}-{n + 1}"
            by_make[str(item.get("make") or "")] = by_make.get(str(item.get("make") or ""), 0) + 1
        rows += built
        absent_by_year[str(year)] = resolved["absent"]
        stats = year_stats(built, list(year_columns), mapping)
        reports.append({**report, "status": "partial" if errors else "built", "rows": len(built),
                        "empty_columns": empty_columns(stats), "audit": stats,
                        "by_make": dict(sorted(by_make.items()))})
    if not any(r["status"] in ("built", "partial") for r in reports):
        raise BuildStopped("no_year_built", years=reports, discovered={str(y): sorted(s) for y, s in statuses.items()})
    return {"rows": rows, "schema": header, "urls": [base], "years": reports, "absent_columns": absent_by_year}


# --- EEA 2023+ (the datahub's downloadable CSV, K3) ------------------------------------------------------------------------

Downloader = Callable[[str, Path], int]
FILE_SUFFIXES = (".zip", ".csv", ".csv.gz")
NOT_CSV = ("accdb", "mdb", "xlsx", "xls", "sqlite", "pdf", "json", "xml")
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def default_downloader(max_bytes: int, session=None, timeout_s: float = 900.0) -> Downloader:
    """Stream a URL to a file on the runner disk (policy-checked, size-capped); the bytes written."""
    import requests

    from ..source_authority import fetch_allowed
    from ..tools.fetch import USER_AGENT

    http = session or requests.Session()

    def download(url: str, target: Path) -> int:
        if not fetch_allowed(url):
            raise BuildStopped("policy_blocked", url=url)
        size = 0
        with http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=(15, timeout_s),
                      stream=True) as resp:
            resp.raise_for_status()
            with open(target, "wb") as handle:
                for chunk in resp.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise BuildStopped("download_too_large", url=url, bytes=size)
                    handle.write(chunk)
        return size
    return download


def _text_of(value: Any) -> str:
    """A datahub field that may be a string, a {language: text} map or a list of either."""
    if isinstance(value, dict):
        for key in ("eng", "en", "default", "#text", "value"):
            if isinstance(value.get(key), str):
                return value[key]
        return next((v for v in value.values() if isinstance(v, str)), "")
    if isinstance(value, list):
        return next((t for t in (_text_of(v) for v in value) if t), "")
    return str(value) if value is not None else ""


def datahub_related(data: Any) -> dict:
    """{children: [{uuid, title}], onlines: [{url, title, function, protocol}]} of a datahub record's `/related` JSON
    (read defensively: any `children` list, any `onlines` list, any object with an http(s) `url`)."""
    children: dict[str, str] = {}
    onlines: dict[str, dict] = {}

    def walk(node: Any, parent: str = "") -> None:
        if isinstance(node, dict):
            url = _text_of(node.get("url")) if "url" in node else ""
            if url.startswith(("http://", "https://")) and url not in onlines:
                onlines[url] = {"url": url, "title": _text_of(node.get("title"))[:200],
                                "function": _text_of(node.get("function")), "protocol": _text_of(node.get("protocol"))}
            for key, value in node.items():
                if key == "children" and isinstance(value, list):
                    for child in value:
                        if isinstance(child, dict):
                            uuid = _text_of(child.get("id") or child.get("uuid") or child.get("metadataUuid"))
                            if uuid:
                                children.setdefault(uuid, _text_of(child.get("title"))[:300])
                walk(value, key)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent)
    walk(data)
    return {"children": [{"uuid": k, "title": v} for k, v in children.items()], "onlines": list(onlines.values())}


def title_year_status(title: str, pattern: str) -> tuple[int, str] | None:
    """(year, F | P) from a datahub record title ("..., 2023 - Final data"), or None."""
    m = re.search(pattern, title or "", re.I)
    if not m:
        return None
    return int(m.group("year")), "F" if m.group("status").lower().startswith("final") else "P"


def _is_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(FILE_SUFFIXES)


def csv_candidates(links: list[str], fetch: Fetcher, file_pattern: str | None = None) -> tuple[list[str], list[str]]:
    """(the CSV / zip file links, every link seen): a link that is not a file is read as an index page and its same-host
    file links are followed (one level). `file_pattern` (data) narrows them; never a constructed URL."""
    seen, files = [], []
    for url in links:
        seen.append(url)
        if _is_file(url):
            files.append(url)
            continue
        try:
            page = _decode(fetch(url))
        except Exception:  # noqa: BLE001 - an index that cannot be read is listed, not fatal
            continue
        host = urlparse(url).netloc
        for href in HREF.findall(page):
            target = urljoin(url if url.endswith("/") else url + "/", href)
            if urlparse(target).netloc == host and _is_file(target) and target not in files:
                seen.append(target)
                files.append(target)
    files = [f for f in files if not any(t in f.lower().rsplit("/", 1)[-1] for t in NOT_CSV)]
    if file_pattern:
        files = [f for f in files if re.search(file_pattern, f, re.I)]
    return sorted(set(files)), seen


RECORD_API = re.compile(r"/api/records/([0-9A-Za-z][0-9A-Za-z_-]{0,63})(?:[/?#]|$)")
NOT_DATA = ("accdb", "mdb", "xlsx", "xls", "sqlite", "pdf", "json", "xml", "png", "jpg", "jpeg", "gif", "svg", "doc",
            "docx", "ppt", "pptx")
DOWNLOAD_FORMATS = ("zip", "csv", "text/csv", "application/zip", "application/x-zip-compressed")
RELATION_KEYS = ("children", "parent", "siblings", "brothersandsisters", "associated", "datasets", "services",
                 "sources", "hassources", "related")
METADATA_ENDPOINT = "{api}{uuid} (Accept: application/json)"


def _local(key: Any) -> str:
    """A JSON key without its namespace prefix, lower case ('gmd:CI_OnlineResource' -> 'ci_onlineresource')."""
    return str(key).rsplit(":", 1)[-1].lower()


def _deep_text(value: Any) -> str:
    """The first text of a datahub field however deeply it is wrapped ({'gmd:URL': ...}, {'gco:CharacterString':
    {'#text': ...}}, {language: text}, a list)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("eng", "en", "default", "#text", "value", "$"):
            if isinstance(value.get(key), str):
                return value[key]
        for item in value.values():
            text = _deep_text(item)
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = _deep_text(item)
            if text:
                return text
    return ""


def record_metadata(data: Any) -> dict:
    """D1: {title, resources: [{url, protocol, name, description, format}], temporal_year} of a datahub record's own
    metadata (the record API in JSON, read defensively: ISO 19139 / 19115-3 converted to JSON or a flat record). A
    resource is an online linkage / url under the distribution part (a key naming distribution, transfer options or
    online resources), never a contact's website; the title is the citation's title."""
    out: dict[str, Any] = {"title": None, "resources": {}, "begin": None, "end": None}

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            keys = {_local(k): k for k in node}
            if out["title"] is None and "title" in keys and any("citation" in p for p in path):
                out["title"] = _deep_text(node[keys["title"]]).strip()[:300] or None
            distribution = any(w in p for p in path for w in ("distribution", "transferoption", "online"))
            contact = any(w in p for p in path for w in ("contact", "party", "responsib", "thumbnail", "overview"))
            for name in ("linkage", "url"):
                if name in keys and distribution and not contact:
                    url = _deep_text(node[keys[name]]).strip()
                    if url.startswith(("http://", "https://")) and url not in out["resources"]:
                        out["resources"][url] = {
                            "url": url, **{field: _deep_text(node[keys[field]]).strip()[:200] if field in keys else ""
                                           for field in ("protocol", "name", "description")},
                            "format": next((_deep_text(node[keys[f]]).strip()[:80] for f in
                                            ("format", "mimetype", "applicationprofile") if f in keys), "")}
            for name, slot in (("beginposition", "begin"), ("endposition", "end")):
                if name in keys and out[slot] is None:
                    out[slot] = _deep_text(node[keys[name]])
            for key, value in node.items():
                walk(value, path + (_local(key),))
        elif isinstance(node, list):
            for item in node:
                walk(item, path)
    walk(data, ())
    years = [re.search(r"(?<!\d)(19|20)\d\d(?!\d)", str(v or "")) for v in (out["begin"], out["end"])]
    temporal = None
    if all(years) and years[0].group(0) == years[1].group(0):
        temporal = int(years[0].group(0))
    return {"title": out["title"], "resources": list(out["resources"].values()), "temporal_year": temporal}


def datahub_related_records(data: Any) -> list[dict]:
    """[{uuid, title, relation}] of every record a datahub `/related` answer names (children, parent, siblings,
    associated, ...), plus the record-API URLs among its online links."""
    found: dict[str, dict] = {}

    def walk(node: Any, relation: str | None) -> None:
        if isinstance(node, dict):
            if relation:                            # an item of a relation list names a record
                uuid = _text_of(node.get("id") or node.get("uuid") or node.get("metadataUuid")).strip()
                if uuid:
                    found.setdefault(uuid, {"uuid": uuid, "title": _text_of(node.get("title"))[:300],
                                            "relation": relation})
            for key, value in node.items():
                local = _local(key)
                walk(value, local if local in RELATION_KEYS else None)
        elif isinstance(node, list):
            for item in node:
                walk(item, relation)
    walk(data, None)
    for online in datahub_related(data)["onlines"]:
        m = RECORD_API.search(urlparse(online["url"]).path + "/")
        if m:
            found.setdefault(m.group(1), {"uuid": m.group(1), "title": online.get("title") or "", "relation": "link"})
    return list(found.values())


def _allowed_host(url: str, hosts: list[str]) -> bool:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return any(host == h or host.endswith("." + h) for h in hosts)


def classify_resource(resource: dict, hosts: list[str]) -> str:
    """D1: download | landing | record | excluded. A download states it (protocol WWW:DOWNLOAD*, format zip / csv /
    text/csv) or its URL path ends in .zip / .csv / .csv.gz; a table definition, document, image or DOI is never one;
    only EEA hosts and data.europa.eu (`hosts`)."""
    url = str(resource.get("url") or "")
    parsed = urlparse(url)
    name = parsed.path.lower().rsplit("/", 1)[-1]
    extensions = name.split(".")[1:]
    if parsed.netloc.lower().endswith("doi.org") or any(t in extensions for t in NOT_DATA):
        return "excluded"
    if not _allowed_host(url, hosts):
        return "excluded"
    if RECORD_API.search(parsed.path + "/"):
        return "record"
    protocol = str(resource.get("protocol") or "").lower()
    fmt = str(resource.get("format") or "").lower().strip()
    if name.endswith(FILE_SUFFIXES) or "download" in protocol or fmt in DOWNLOAD_FORMATS:
        return "download"
    return "landing"


def _record_year_status(title: str | None, temporal: int | None, pattern: str) -> tuple[int | None, str | None, str]:
    """(year, F | P, basis) from a record title (`title_pattern`) or its temporal extent (the status then from the
    title's own words); never from a file name."""
    parsed = title_year_status(title or "", pattern) if title else None
    if parsed:
        return parsed[0], parsed[1], "title"
    words = re.search(r"\b(final|provisional)\b", title or "", re.I)
    if temporal and words:
        return temporal, "F" if words.group(1).lower() == "final" else "P", "temporal_extent"
    return None, None, ""


def eea_datahub_discover(fetch: Fetcher, csv_cfg: dict, *, max_depth: int | None = None,
                         max_records: int | None = None) -> tuple[dict[int, dict], list[dict]]:
    """D1: ({year: choice}, per-record report). From the series and the listed records, every record a related answer
    or a record-API link names, at most `max_depth` levels deep and `max_records` records per run (a record is read
    once: a cycle is not followed twice). Per record its own metadata (the record API in JSON) and its `/related`
    answer; their online resources classified (`classify_resource`). Year and status from the record's title or
    temporal extent (a listed record's configured year / status, the reviewer's, last). Per year, final records
    preferred: their download resources (none: the EEA landing pages read once for same-host file links); exactly one
    -> chosen, several -> `file_pattern`, still several -> `ambiguous_download` with every candidate; none ->
    `no_csv_link`. A provisional child whose year has a final child in a listing is not read. Never a guessed URL."""
    fetch_json = getattr(fetch, "json", fetch)
    api, pattern = str(csv_cfg.get("api") or ""), str(csv_cfg.get("title_pattern") or "")
    hosts = [str(h).lower() for h in csv_cfg.get("allowed_hosts") or ["eea.europa.eu", "data.europa.eu"]]
    max_depth = int(csv_cfg.get("max_depth") or 2) if max_depth is None else max_depth
    max_records = int(csv_cfg.get("max_records") or 40) if max_records is None else max_records
    queue: list[dict] = [{"uuid": str(s.get("uuid")), "kind": "series", "depth": 0, "via": None}
                         for s in csv_cfg.get("series") or [] if s.get("uuid")]
    configured: dict[str, dict] = {}
    for record in csv_cfg.get("records") or []:
        if record.get("uuid"):
            configured[str(record["uuid"])] = record
            queue.append({"uuid": str(record["uuid"]), "kind": "listed", "depth": 0, "via": None})
    listed_titles: dict[str, str] = {}
    seen: set[str] = set()
    reports: list[dict] = []
    records: list[dict] = []

    def read(url: str) -> tuple[Any, str | None]:
        try:
            return json.loads(_decode(fetch_json(url))), None
        except BuildStopped as stop:
            return None, stop.reason
        except Exception as exc:  # noqa: BLE001 - one record never costs the others
            return None, f"{type(exc).__name__}: {str(exc)[:200]}"

    while queue:
        item = queue.pop(0)
        uuid = item["uuid"]
        if uuid in seen:
            continue
        seen.add(uuid)
        listed = title_year_status(listed_titles.get(uuid, ""), pattern) if listed_titles.get(uuid) else None
        if listed and listed[1] == "P" and any(title_year_status(t, pattern) == (listed[0], "F")
                                               for t in listed_titles.values()):
            reports.append({**item, "title": listed_titles[uuid], "year": listed[0], "status": "P",
                            "status_note": "superseded (final preferred); not read"})
            continue
        if len(records) >= max_records:
            reports.append({**item, "status_note": f"not read: record limit {max_records}"})
            continue
        meta_data, meta_error = read(f"{api}{uuid}")
        related_data, related_error = read(f"{api}{uuid}/related")
        meta = record_metadata(meta_data) if meta_data is not None else {"title": None, "resources": [],
                                                                          "temporal_year": None}
        related = datahub_related(related_data) if related_data is not None else {"children": [], "onlines": []}
        references = datahub_related_records(related_data) if related_data is not None else []
        for child in related["children"]:
            listed_titles.setdefault(child["uuid"], child["title"])
        resources = {r["url"]: dict(r) for r in meta["resources"]}
        for online in related["onlines"]:
            resources.setdefault(online["url"], {"url": online["url"], "protocol": online.get("protocol") or "",
                                                 "name": online.get("title") or "", "description": "",
                                                 "format": ""})
        for resource in resources.values():
            resource["class"] = classify_resource(resource, hosts)
            m = RECORD_API.search(urlparse(resource["url"]).path + "/")
            if resource["class"] == "record" and m and all(m.group(1) != r["uuid"] for r in references):
                references.append({"uuid": m.group(1), "title": "", "relation": "link"})
        title = meta["title"] or listed_titles.get(uuid) or None
        year, status, basis = _record_year_status(title, meta["temporal_year"], pattern)
        if year is None and uuid in configured and configured[uuid].get("year") and configured[uuid].get("status"):
            year, status, basis = int(configured[uuid]["year"]), str(configured[uuid]["status"]), "configured"
        record = {**item, "title": title, "year": year, "status": status, "year_basis": basis or None,
                  "metadata": "ok" if meta_data is not None else f"failed: {meta_error}",
                  "related": "ok" if related_data is not None else f"failed: {related_error}",
                  "resources": [{k: r.get(k) for k in ("url", "class", "protocol", "format", "name")}
                                for r in resources.values()][:40],
                  "references": [r["uuid"] for r in references][:40]}
        if item["kind"] == "series":
            record["children"] = len(related["children"])
        records.append(record)
        reports.append(record)
        if item["depth"] < max_depth:
            for ref in references:
                if ref["uuid"] not in seen:
                    queue.append({"uuid": ref["uuid"], "kind": "child" if ref["relation"] == "children" else "related",
                                  "depth": item["depth"] + 1, "via": uuid})
    choices: dict[int, dict] = {}
    pages: dict[str, list[str]] = {}
    for year in sorted({r["year"] for r in records if r["year"] and r["status"]}):
        status = "F" if any(r["year"] == year and r["status"] == "F" for r in records) else "P"
        holders = [r for r in records if r["year"] == year and r["status"] == status]
        candidates: dict[str, str] = {}
        for record in holders:
            for resource in record["resources"]:
                if resource["class"] == "download":
                    candidates.setdefault(resource["url"], record["uuid"])
        links_seen = [res["url"] for record in holders for res in record["resources"]]
        if not candidates:
            for record in holders:
                for resource in record["resources"]:
                    if resource["class"] != "landing" or "eea.europa.eu" not in urlparse(resource["url"]).netloc:
                        continue
                    if resource["url"] not in pages:
                        pages[resource["url"]] = csv_candidates([resource["url"]], fetch)[0]
                    for found in pages[resource["url"]]:
                        candidates.setdefault(found, record["uuid"])
                        links_seen.append(found)
        files = sorted(u for u in candidates if not any(t in u.lower().rsplit("/", 1)[-1] for t in NOT_CSV)
                       and _allowed_host(u, hosts))
        if csv_cfg.get("file_pattern"):
            files = [u for u in files if re.search(csv_cfg["file_pattern"], u, re.I)]
        choice = {"year": year, "status": status, "records": [r["uuid"] for r in holders],
                  "record": candidates.get(files[0]) if len(files) == 1 else holders[0]["uuid"],
                  "url": files[0] if len(files) == 1 else None, "candidates": files[:20],
                  "links": list(dict.fromkeys(links_seen))[:40]}
        if len(files) != 1:
            choice["reason"] = "no_csv_link" if not files else "ambiguous_download"
        choices[year] = choice
    summary = {"kind": "summary", "metadata_endpoint": METADATA_ENDPOINT, "records_read": len(records),
               "max_depth": max_depth, "max_records": max_records,
               "years": {str(y): {k: c.get(k) for k in ("status", "url", "reason")} for y, c in choices.items()}}
    return choices, reports + [summary]


def csv_header_from_prefix(body: bytes) -> dict:
    """D2: {header, file, compression} of the first bytes of a CSV, a .csv.gz or a zip (its first entry's local header
    and the start of its deflated data; no central directory needed), or {error}."""
    import struct
    import zlib

    name, text = None, b""
    try:
        if body[:4] == b"PK\x03\x04":
            _, _, flag, method, _, _, _, _, _, nlen, xlen = struct.unpack("<IHHHHHIIIHH", body[:30])
            name = body[30:30 + nlen].decode("utf-8", errors="replace")
            data = body[30 + nlen + xlen:]
            if method == 8:
                text = zlib.decompressobj(-15).decompress(data, 1 << 20)
            elif method == 0:
                text = data
            else:
                return {"error": f"zip compression method {method}", "file": name}
        elif body[:2] == b"\x1f\x8b":
            text = zlib.decompressobj(16 + 15).decompress(body, 1 << 20)
        else:
            text = body
    except (zlib.error, struct.error) as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "file": name}
    line = _decode(text.split(b"\n", 1)[0]).strip("\r\ufeff")
    if not line:
        return {"error": "no header line in the first bytes", "file": name}
    try:
        dialect = csv.Sniffer().sniff(line, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return {"header": next(csv.reader([line], dialect)), "file": name}


def _bigint(value: Any) -> int | None:
    """TRY_CAST(... AS bigint): an integer text, else None."""
    text = str(value if value is not None else "").strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def _csv_streams(path: Path):
    """(name, text stream) of every CSV in a zip, or of the file itself."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(n for n in archive.namelist() if n.lower().endswith(".csv")):
                with archive.open(name) as raw:
                    yield name, io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        return
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as raw:
        yield path.name, io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")


def eea_csv_columns(cfg: dict, year: int | None = None) -> dict:
    """The column map with the CSV's `column_aliases` (and the year's `column_aliases_by_year`) added to each key's
    accepted spellings."""
    return eea_year_columns(cfg, year, csv_file=True)


def eea_csv_year(path: Path, year: int, status: str, *, progress: Progress = _noop) -> tuple[list[dict], dict]:
    """(rows, report) of one year's datahub CSV: streamed, filtered to the make values that are alias spellings or
    normalize to a canonical make, grouped exactly like the DISCODATA path (identity + measurement columns with
    SUM(r), then `eea_aggregate`). A header without a required key stops this year (`schema_mismatch`)."""
    from .audit import empty_columns, year_stats
    from .makes import canonical_of, row_filter, spelling

    cfg = ds.datasets()["eea_co2_cars"]
    columns = eea_csv_columns(cfg, year)
    wanted_makes = known_makes()
    keep = row_filter()
    report: dict[str, Any] = {"year": year, "status_used": status, "source": "datahub_csv", "mode": "csv"}
    combos: dict[tuple, list] = {}
    mapping: dict[str, str] = {}
    keys: list[str] = []
    measures: list[str] = []
    read = skipped_year = 0
    allowed: dict[str, bool] = {}
    for name, stream in _csv_streams(path):
        head = stream.read(65536)
        try:
            dialect = csv.Sniffer().sniff(head.split("\n", 1)[0], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(_rejoin(head, stream), dialect)
        header = next(reader, [])
        resolved = resolve_map(header, columns)
        if resolved["missing"] or resolved["ambiguous"]:
            return [], {**report, "status": "stopped", "reason": "schema_mismatch", "file": name,
                        "missing": resolved["missing"], "ambiguous": resolved["ambiguous"],
                        "live_header": header[:200]}
        if not mapping:
            mapping = resolved["mapping"]
            keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
            measures = [k for k in cfg.get("measure_keys") or [] if k in mapping]
            report["absent_columns"] = resolved["absent"]
            report["live_header"] = header[:200]
            report["mapping"] = mapping
        reg = resolve_map(header, {"registrations": cfg["registrations_column"]})["mapping"].get("registrations")
        index = {h: i for i, h in enumerate(header)}
        cols = [index[mapping[k]] for k in keys + measures]
        make_i, year_i = index[mapping["make"]], index[mapping["year"]]
        reg_i = index.get(reg) if reg else None
        for row in reader:
            if len(row) < len(header):
                continue
            read += 1
            if read % 1_000_000 == 0:
                progress(f"year {year} (csv): {read:,} rows read")
            row_year = ds._int(row[year_i])
            if row_year is not None and row_year != year:
                skipped_year += 1
                continue
            make = row[make_i]
            if make not in allowed:
                allowed[make] = bool(make.strip()) and (spelling(make) in wanted_makes or bool(canonical_of(make)))
            if not allowed[make]:
                continue
            values = tuple(None if row[i].strip() == "" else row[i].strip() for i in cols)
            r = _bigint(row[reg_i]) if reg_i is not None else None
            acc = combos.get(values)
            if acc is None:
                combos[values] = [r or 0, r is not None]
            elif r is not None:
                acc[0] += r
                acc[1] = True
    if not mapping:
        return [], {**report, "status": "stopped", "reason": "no_csv_in_download"}
    live = [mapping[k] for k in keys + measures]
    server_rows = [{**dict(zip(live, values)), "r": total if seen else None}
                   for values, (total, seen) in combos.items()]
    combos.clear()
    built = [r for r in eea_aggregate([(measures, server_rows)], mapping, cfg, status)
             if keep(r.get("make"), " ".join(str(r.get(k) or "") for k in ("model", "variant", "version")))]
    by_make: dict[str, int] = {}
    for n, item in enumerate(built):
        item["make"] = spelling(item.get("make"))
        item["year"] = ds._int(item.get("year"))       # CSV text; the shard types every other number
        item["row_id"] = f"eea-{year}-{status}-{n + 1}"
        by_make[item["make"]] = by_make.get(item["make"], 0) + 1
    stats = year_stats(built, list(columns), mapping)
    report.update(status="built", rows=len(built), rows_read=read, other_year_rows=skipped_year,
                  source_rows=read - skipped_year, empty_columns=empty_columns(stats), audit=stats,
                  by_make=dict(sorted(by_make.items())))
    return built, report


def _rejoin(head: str, stream):
    """The lines of a text stream whose first 64 KB were already read (for the dialect sniff)."""
    lines = head.split("\n")
    yield from (line + "\n" for line in lines[:-1])
    yield lines[-1] + stream.readline()               # the line the 64 KB cut (or "" at the end)
    yield from stream


def build_eea_csv(fetch: Fetcher, *, skip_years: set[int] | None = None, download: Downloader | None = None,
                  work: Path | None = None, progress: Progress = _noop, lacking_years: set[int] | None = None,
                  join_years: set[int] | None = None) -> dict:
    """K3: the years from `csv_years.from_year` that DISCODATA did not build, and the earlier `lacking_years` (Y1: the
    years DISCODATA lacks), from the datahub CSV found by `eea_datahub_discover` (D1). {rows, years (one report per
    year), absent_columns, urls, discovery, join}; a year up to `final_only_through_year` only from a final record; a
    year that fails stops only itself; never raises. D2: the `join_years` (built from DISCODATA) are downloaded and
    grouped too, returned in `join` ({year: {rows, report, url, record}}) for the range join, never as rows."""
    import shutil
    import tempfile

    cfg = ds.datasets()["eea_co2_cars"]
    csv_cfg = cfg.get("csv_years") or {}
    out: dict[str, Any] = {"rows": [], "years": [], "absent_columns": {}, "urls": [], "discovery": []}
    if not csv_cfg:
        return out
    out["join"] = {}
    progress("datahub records (record metadata, D1)")
    try:
        by_year, out["discovery"] = eea_datahub_discover(fetch, csv_cfg)
    except Exception as exc:  # noqa: BLE001 - the CSV years never cost the DISCODATA years
        out["discovery"] = [{"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}]
        return out
    floor = int(csv_cfg.get("from_year") or 0)
    max_bytes = int(float(csv_cfg.get("max_download_mb") or 6000) * 1024 * 1024)
    if download is None:
        def download(url: str, target: Path) -> int:          # the injected fetcher (tests): bytes in memory
            body = fetch(url)
            target.write_bytes(body)
            return len(body)
    if work:
        Path(work).mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="eea-csv-", dir=str(work) if work else None))
    try:
        for year, choice in sorted(by_year.items()):
            base = {"year": year, "status_used": choice["status"], "source": "datahub_csv", "record": choice["record"]}
            joining = year in (join_years or set())
            if joining:
                base.update(mode="range_join_source", csv_status=choice["status"])
            elif year in (skip_years or set()) or (year < floor and year not in (lacking_years or set())):
                out["years"].append({**base, "status": "skipped", "reason": "built from DISCODATA"
                                     if year in (skip_years or set()) else f"before csv_years.from_year {floor}"})
                continue
            if accepted_status(cfg, year, {choice["status"]})[0] is None:
                out["years"].append({**base, "status": "skipped", "reason": "no_final_rows"})
                continue
            if not choice.get("url"):
                out["years"].append({**base, "status": "stopped", "reason": choice.get("reason") or "no_csv_link",
                                     "files": choice.get("candidates") or [], "links": choice.get("links") or [],
                                     "records": choice.get("records")})
                continue
            url = choice["url"]
            target = folder / f"{year}{''.join(Path(urlparse(url).path).suffixes[-2:]) or '.zip'}"
            progress(f"year {year} (csv): download {url.rsplit('/', 1)[-1]}")
            try:
                size = download(url, target)
                rows, report = eea_csv_year(target, year, choice["status"], progress=progress)
            except BuildStopped as stop:
                out["years"].append({**base, "status": "stopped", "reason": stop.reason, "url": url, **stop.report})
                continue
            except Exception as exc:  # noqa: BLE001 - one year never costs the others
                out["years"].append({**base, "status": "failed", "url": url,
                                     "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                continue
            finally:
                if target.exists():
                    target.unlink()
            if joining:
                if report.get("status") == "built":
                    out["join"][year] = {"rows": rows, "report": {**base, **report, "url": url,
                                                                  "download_bytes": size}}
                else:
                    out["years"].append({**base, **report, "url": url, "download_bytes": size})
                continue
            out["years"].append({**base, **report, "url": url, "download_bytes": size})
            if report.get("status") == "built":
                out["rows"] += rows
                out["urls"].append(url)
                out["absent_columns"][str(year)] = report.get("absent_columns") or []
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return out


def range_join(disco_rows: list[dict], csv_rows: list[dict], keys: list[str], columns: list[str]) -> dict:
    """D2: the CSV-only `columns` (electric_range_km with its _min / _max) added to the DISCODATA rows of one year from
    the datahub CSV rows of the same year, joined on the full identity key `keys` (normalized: text as stored,
    numbers as numbers). A key without an exact join, or a key two CSV rows share, gets nothing. In place; the join
    statistics."""
    text = set(ds.text_identity_keys())

    def value(key: str, raw: Any) -> Any:
        if raw is None or raw == "":
            return None
        if key in text:
            return ds.norm_text(raw)
        number = ds._number(raw)
        return float(number) if number is not None else ds.norm_text(raw)

    index: dict[tuple, dict | None] = {}
    duplicates = 0
    for row in csv_rows:
        key = tuple(value(k, row.get(k)) for k in keys)
        if key in index:
            duplicates += 1
            index[key] = None
        else:
            index[key] = row
    matched = with_values = 0
    weight = weight_values = 0.0
    for row in disco_rows:
        w = float(ds._number(row.get("registrations")) or 0)
        weight += w
        hit = index.get(tuple(value(k, row.get(k)) for k in keys))
        if not hit:
            continue
        matched += 1
        added = False
        for column in columns:
            for suffix in ("", "_min", "_max"):
                if hit.get(f"{column}{suffix}") is not None:
                    row[f"{column}{suffix}"] = hit[f"{column}{suffix}"]
                    added = True
        if added:
            with_values += 1
            weight_values += w

    def pct(part: float, whole: float) -> float | None:
        return round(100.0 * part / whole, 1) if whole else None
    return {"join_rows": len(disco_rows), "csv_rows": len(csv_rows), "csv_duplicate_keys": duplicates,
            "matched": matched, "join_pct": pct(matched, len(disco_rows)), "with_values": with_values,
            "values_pct": pct(with_values, len(disco_rows)), "values_pct_registrations": pct(weight_values, weight)}


def build_eea_all(fetch: Fetcher, *, download: Downloader | None = None, progress: Progress = _noop) -> dict:
    """The DISCODATA years, then the datahub CSV years DISCODATA does not have (K3, found by D1); stops only when no
    year built. D2: for the `csv_years.range_join_years` built from DISCODATA (it stays the base), the CSV-only
    columns (`range_join_columns`: electric_range_km) are added from the datahub CSV of the same year, joined on the
    full identity key; the year's audit statistics are recomputed and a `range_join` report gives the join %."""
    from .audit import empty_columns, year_stats

    stopped = None
    try:
        built = build_eea(fetch, progress=progress)
    except BuildStopped as stop:
        stopped = stop
        built = {"rows": [], "schema": None, "urls": [], "years": list(stop.report.get("years") or []),
                 "absent_columns": {}, "discodata": {"reason": stop.reason}}
    done = {r["year"] for r in built["years"] if r.get("status") in ("built", "partial")}
    cfg = ds.datasets()["eea_co2_cars"]
    floor = int(cfg.get("years_from") or 0)
    csv_from = int((cfg.get("csv_years") or {}).get("from_year") or floor)
    # Y1: an earlier year DISCODATA lacks (not discovered, or no rows of the accepted status); a year that failed or
    # stopped there is reported, never re-read from the CSV
    handled = {r["year"] for r in built["years"] if r.get("status") != "skipped"}
    lacking = {y for y in range(floor, csv_from) if y not in handled} if floor else set()
    csv_cfg = cfg.get("csv_years") or {}
    join_years = {int(y) for y in csv_cfg.get("range_join_years") or []} & done
    extra = build_eea_csv(fetch, skip_years=done, download=download, work=ds.snapshot_dir(), progress=progress,
                          lacking_years=lacking, join_years=join_years)
    built["rows"] += extra["rows"]
    built["years"] += [y for y in extra["years"] if y.get("year") not in done or y.get("status") != "skipped"]
    join_columns = [str(c) for c in csv_cfg.get("range_join_columns") or ["electric_range_km"]]
    for year, joined in sorted((extra.get("join") or {}).items()):
        disco = next(r for r in built["years"] if r.get("year") == year and r.get("status") in ("built", "partial"))
        source = joined["report"]
        csv_map = source.get("mapping") or {}
        columns = [c for c in join_columns if c in csv_map]
        keys = [k for k in cfg.get("group_by_keys") or [] if k in (disco.get("mapping") or {})]
        entry: dict[str, Any] = {
            "year": year, "source": "datahub_csv", "mode": "range_join", "status_used": disco.get("status_used"),
            "csv_status": source.get("csv_status"), "record": source.get("record"), "url": source.get("url"),
            "csv_rows_read": source.get("rows_read"), "csv_live_header": source.get("live_header"),
            "join_keys": keys, "columns": columns}
        missing = [k for k in keys if k not in csv_map]
        if not columns:
            entry.update(status="no_range_column", reason=f"none of {join_columns} in the CSV header")
        elif missing:
            entry.update(status="join_keys_missing", missing=missing)
        else:
            year_rows = [r for r in built["rows"] if ds._int(r.get("year")) == year]
            entry.update(status="joined", **range_join(year_rows, joined["rows"], keys, columns))
            if entry["with_values"]:
                for column in columns:
                    disco["mapping"][column] = f"{csv_map[column]} (datahub CSV join)"
                disco["absent_columns"] = [c for c in disco.get("absent_columns") or [] if c not in columns]
                built.setdefault("absent_columns", {})[str(year)] = disco["absent_columns"]
                disco["audit"] = year_stats(year_rows, list(eea_year_columns(cfg, year)), disco["mapping"])
                disco["empty_columns"] = empty_columns(disco["audit"])
        built["years"].append(entry)
    built["urls"] = list(built.get("urls") or []) + extra["urls"]
    built["absent_columns"] = {**(built.get("absent_columns") or {}), **extra["absent_columns"]}
    built["csv_discovery"] = extra["discovery"]
    if not any(r.get("status") in ("built", "partial") for r in built["years"]):
        raise BuildStopped(stopped.reason if stopped else "no_year_built", years=built["years"],
                           csv_discovery=extra["discovery"], **({k: v for k, v in stopped.report.items()
                                                                 if k != "years"} if stopped else {}))
    return built


# --- ADEME (data-fair) -----------------------------------------------------------------------------------------------------

def _schema_fields(schema: Any) -> list[dict]:
    if isinstance(schema, dict):
        schema = schema.get("schema") or schema.get("fields") or []
    return [f for f in schema or [] if isinstance(f, dict)]


def _tokens_in(text: str, tokens: list[str]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", text) for t in tokens)


def unit_scales(columns: dict, mapping: dict[str, str], schema: Any) -> tuple[dict[str, float], dict[str, dict]]:
    """({key: scale}, {key: unit info}) of every mapped column with a `unit_from_description` rule, read from the
    schema field whose original name is the live column (its description, x-unit and title: "Puissance en kW",
    "En Kg"). Exactly one accepted unit stated -> that unit and its scale; anything else (no unit stated, another unit,
    several) -> unit `unknown`: the raw values are stored, and the column yields no match key and no offer until the
    operator confirms its unit with a `unit_overrides` entry of data/open_datasets.json. Never assumed, never fatal."""
    from .datasets import UNKNOWN_UNIT

    fields = _schema_fields(schema)
    scales: dict[str, float] = {}
    units: dict[str, dict] = {}
    for key, spec in columns.items():
        rule = spec.get("unit_from_description") if isinstance(spec, dict) else None
        if not rule or key not in mapping:
            continue
        live = _norm(mapping[key])
        field = next((f for f in fields if _norm(f.get("x-originalName") or f.get("title") or f.get("key")) == live),
                     None)
        if field is None:
            units[key] = {"unit": UNKNOWN_UNIT, "reason": "not_in_schema", "column": mapping[key]}
            continue
        stated = " ".join(json.dumps(field.get(k), ensure_ascii=False) for k in ("description", "x-unit", "title")
                          if field.get(k)).lower()
        accepted = [u for u, r in (rule.get("accept") or {}).items() if _tokens_in(stated, r.get("tokens") or [])]
        if len(accepted) == 1:
            scales[key] = float(rule["accept"][accepted[0]].get("scale") or 1)
            units[key] = {"unit": accepted[0], "basis": "schema_description", "column": mapping[key],
                          "stated": field.get("description")}
        else:
            units[key] = {"unit": UNKNOWN_UNIT, "reason": "unit_unstated" if not accepted else "unit_ambiguous",
                          "accepted": accepted, "column": mapping[key],
                          "schema_field": {k: field.get(k) for k in ("key", "x-originalName", "title", "description",
                                                                     "x-unit", "type") if k in field}}
    for key, spec in columns.items():               # a Max column takes its Min column's unit
        if isinstance(spec, dict) and spec.get("unit_of") in units and key in mapping:
            units[key] = {**units[spec["unit_of"]], "unit_of": spec["unit_of"], "column": mapping[key]}
            if spec["unit_of"] in scales:
                scales[key] = scales[spec["unit_of"]]
    return scales, units


def distribution(rows: list[dict], key: str) -> dict | None:
    """{n, p5, p50, p95} of a column's numeric values (the evidence an operator reads before confirming a unit)."""
    values = []
    for row in rows:
        text = str(row.get(key) if row.get(key) is not None else "").strip().replace(",", ".")
        if PLAIN_NUMBER.match(text):
            values.append(float(text))
    if not values:
        return None
    values.sort()

    def pct(p: float) -> float:
        return values[min(len(values) - 1, max(0, int(round(p / 100 * (len(values) - 1)))))]
    return {"n": len(values), "p5": pct(5), "p50": pct(50), "p95": pct(95)}


def build_ademe(fetch: Fetcher, *, progress: Progress = _noop) -> dict:
    from .datasets import UNKNOWN_UNIT

    cfg = ds.datasets()["ademe_car_labelling"]
    columns = cfg["columns"]
    progress("schema")
    schema = json.loads(_decode(fetch(cfg["schema_url"])))
    progress("csv")
    body = fetch(cfg["csv_url"])
    tables = _csv_tables(body)
    header = tables[0][2] if tables else []
    resolved = resolve_map(header, columns)
    if resolved["missing"] or resolved["ambiguous"]:
        raise BuildStopped("schema_mismatch", missing=resolved["missing"], ambiguous=resolved["ambiguous"],
                           absent=resolved["absent"], live_header=header[:200])
    scales, units = unit_scales(columns, resolved["mapping"], schema)
    catalogue = ademe_catalogue_date(fetch, cfg)
    rows, report = _build_file(cfg["csv_url"], fetch, columns, id_prefix="ademe-", scales=scales,
                               decimal_comma=bool(cfg.get("decimal_comma")), body=body,
                               extra={"catalogue_date": catalogue["date"]} if catalogue.get("date") else None)
    if report["status"] != "built":
        raise BuildStopped(report.get("reason") or report["status"], files=[report])
    unknown = {k: distribution(rows, k) for k, info in units.items() if info.get("unit") == UNKNOWN_UNIT}
    report["units"] = {k: v.get("unit") for k, v in units.items()}
    return {"rows": rows, "schema": header, "urls": [cfg["csv_url"]], "files": [report],
            "absent_columns": report.get("absent_columns") or [], "column_units": units,
            "unit_unknown_distribution": unknown, "catalogue_date": catalogue.get("date"), "catalogue": catalogue}


def ademe_catalogue_date(fetch: Fetcher, cfg: dict) -> dict:
    """M4: {date, basis | error} of the ADEME catalogue: the data-fair dataset metadata (`source_url`) field named by
    catalogue_period.metadata_fields (dataUpdatedAt, else updatedAt). The catalogue has no model year: the match uses
    this date to keep a target out of period."""
    fields = (cfg.get("catalogue_period") or {}).get("metadata_fields") or []
    try:
        meta = json.loads(_decode(fetch(cfg["source_url"])))
    except Exception as exc:  # noqa: BLE001 - the match falls back to the snapshot's built_at
        return {"date": None, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    for field in fields:
        if isinstance(meta, dict) and meta.get(field):
            return {"date": str(meta[field]), "basis": field}
    return {"date": None, "error": f"none of {fields} in the dataset metadata"}


# --- CKAN (NRCan, CVS) -----------------------------------------------------------------------------------------------------

def _resources(fetch: Fetcher, api: str) -> list[dict]:
    page = json.loads(_decode(fetch(api)))
    return [r for r in ((page.get("result") or {}).get("resources") or []) if isinstance(r, dict) and r.get("url")]


def _file_name(resource: dict) -> str:
    return str(resource.get("url") or "").rstrip("/").rsplit("/", 1)[-1].lower()


def _languages(resource: dict) -> list[str]:
    lang = resource.get("language")
    return [str(x).lower() for x in (lang if isinstance(lang, list) else [lang] if lang else [])]


def build_ckan_groups(dataset: str, fetch: Fetcher, *, progress: Progress = _noop) -> dict:
    """NRCan: every CSV resource of the dataset's language, read with its group's map; a file stops only itself."""
    cfg = ds.datasets()[dataset]
    progress("package")
    resources = _resources(fetch, cfg["ckan_api"])
    fmt, lang = str(cfg.get("resource_format") or "CSV").upper(), str(cfg.get("resource_language") or "en").lower()
    excluded = [p.lower() for p in (cfg.get("exclude_resources") or {}).get("patterns") or []]
    files, rows = [], []
    for resource in resources:
        name = _file_name(resource)
        if str(resource.get("format") or "").upper() != fmt or (_languages(resource) and lang not in _languages(resource)):
            continue
        text = f"{name} {str(resource.get('name') or '').lower()}"
        if any(p in text for p in excluded):
            files.append({"url": resource["url"], "status": "excluded",
                          "reason": (cfg.get("exclude_resources") or {}).get("reason")})
            continue
        group = next((g for g in cfg.get("resource_groups") or [] if any(p.lower() in name for p in g["patterns"])),
                     None)
        if group is None:
            files.append({"url": resource["url"], "status": "unclassified", "reason": "no resource_groups pattern"})
            continue
        progress(f"{group['group']}: {name}")
        built, report = _build_file(resource["url"], fetch, group.get("columns"), id_prefix=f"{dataset}-{name}-",
                                    decimal_comma=bool(cfg.get("decimal_comma")),
                                    extra={"group": group["group"], **(group.get("constants") or {})})
        files.append({**report, "group": group["group"]})
        rows += built
    if not any(f["status"] == "built" for f in files):
        raise BuildStopped("no_file_built", files=files)
    return {"rows": rows, "schema": next((f.get("live_header") for f in files if f["status"] == "built"), None),
            "urls": [f["url"] for f in files if f["status"] == "built"], "files": files,
            "absent_columns": {f["url"].rsplit("/", 1)[-1]: f.get("absent_columns") or [] for f in files
                               if f["status"] == "built"}}


def _xls_rows(body: bytes) -> list[list[str]]:
    try:
        import xlrd
    except ImportError as exc:
        raise BuildStopped("dictionary_unreadable", note="the xlrd package is not installed") from exc
    try:
        book = xlrd.open_workbook(file_contents=body)
    except Exception as exc:  # noqa: BLE001
        raise BuildStopped("dictionary_unreadable", error=f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    out = []
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            cells = [str(c.value).strip() for c in sheet.row(r)]
            if any(cells):
                out.append(cells)
    return out


def override_units(overrides: dict | None) -> dict[str, str]:
    """{CODE: unit} of the dataset's `unit_overrides` entries that name a source code (CVS: OL / OW / OH / WB / CW)."""
    out = {}
    for key, entry in (overrides or {}).items():
        if isinstance(entry, dict) and not str(key).startswith("_") and entry.get("code") and entry.get("unit"):
            out[str(entry["code"]).strip().upper()] = str(entry["unit"])
    return out


def dictionary_check(rows: list[list[str]], check: dict, overrides: dict | None = None) -> list[dict]:
    """Problems of the CVS data dictionary against the map: every code must be a cell of some row (codes matched
    trimmed: "OH ", "CW ") and that row must name the code's unit (unit_tokens), or a `unit_overrides` entry must
    confirm that unit (recorded evidence). A code with neither is still a problem."""
    confirmed = override_units(overrides)
    problems = []
    for code, unit in (check.get("codes") or {}).items():
        row = next((r for r in rows if any(c.strip().upper() == code.upper() for c in r)), None)
        if row is None:
            problems.append({"code": code, "reason": "not_in_dictionary"})
            continue
        if unit and not _tokens_in(" ".join(row).lower(), (check.get("unit_tokens") or {}).get(unit) or [unit]):
            if confirmed.get(code.upper()) == unit:
                continue
            problems.append({"code": code, "reason": "unit_not_stated", "expected_unit": unit, "dictionary_row": row,
                             "override": confirmed.get(code.upper())})
    return problems


def measured_year(value: Any) -> int | None:
    """The CVS MYR (2-digit measurement year: 2 -> 2002, 14 -> 2014); a 4-digit year stays as stated."""
    year = ds._int(value)
    if year is None or year < 0:
        return None
    return 2000 + year if year < 100 else year


def build_ckan_files(dataset: str, fetch: Fetcher, *, progress: Progress = _noop) -> dict:
    """CVS: the data dictionary first (every mapped code and unit), then every file matching resource_pattern."""
    cfg = ds.datasets()[dataset]
    progress("package")
    resources = _resources(fetch, cfg["ckan_api"])
    files_re, dict_re = re.compile(cfg["resource_pattern"], re.I), re.compile(cfg["dictionary_pattern"], re.I)
    dictionary = next((r for r in resources if dict_re.search(_file_name(r))), None)
    if dictionary is None:
        raise BuildStopped("no_dictionary", resources=[_file_name(r) for r in resources][:80])
    progress("data dictionary")
    dict_rows = _xls_rows(fetch(dictionary["url"]))
    problems = dictionary_check(dict_rows, cfg.get("dictionary_check") or {}, cfg.get("unit_overrides"))
    if problems:
        raise BuildStopped("dictionary_mismatch", problems=problems, dictionary_rows=dict_rows[:120])
    targets = sorted((r for r in resources if files_re.search(_file_name(r))), key=_file_name)
    if not targets:
        raise BuildStopped("no_download_url", resources=[_file_name(r) for r in resources][:80])
    files, rows = [], []
    for resource in targets:
        name = _file_name(resource)
        progress(f"file {name}")
        year = re.search(r"(\d{4})", name)
        # E5: `year` is the file year (2018_en.csv -> 2018); MYR is the 2-digit measurement year, kept apart
        file_year = int(year.group(1)) if year else None
        built, report = _build_file(resource["url"], fetch, cfg.get("columns"), id_prefix=f"{dataset}-{name}-",
                                    decimal_comma=bool(cfg.get("decimal_comma")),
                                    extra={"file_year": file_year, "year": file_year} if year else None)
        for row in built:
            if "measured_year" in row:
                row["measured_year"] = measured_year(row["measured_year"])
        files.append({**report, "file_year": file_year})
        rows += built
    if not any(f["status"] == "built" for f in files):
        raise BuildStopped("no_file_built", files=files)
    file_years = sorted(f["file_year"] for f in files if f.get("file_year"))
    return {"rows": rows, "schema": next((f.get("live_header") for f in files if f["status"] == "built"), None),
            "urls": [f["url"] for f in files if f["status"] == "built"], "files": files,
            "file_years": file_years, "last_file_year": file_years[-1] if file_years else None,
            "dictionary": dictionary["url"],
            "absent_columns": {f["url"].rsplit("/", 1)[-1]: f.get("absent_columns") or [] for f in files
                               if f["status"] == "built"}}


# --- compaction (G2): keep only what matching and offers use -----------------------------------------------------------

def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    half, total = sum(w for _, w in pairs) / 2, 0.0
    for value, weight in pairs:
        total += weight
        if total >= half:
            return value
    return pairs[-1][0]


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def group_configurations(rows: list[dict], group_by: list[str], measures: list[str], weight: str | None,
                         keep: list[str] | None = None) -> list[dict]:
    """One row per configuration (the `group_by` keys): every measure as its median (weighted by `weight`, e.g. the
    registrations) plus `<measure>_min` / `<measure>_max`, the weights summed. Never a per-vehicle row."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(k) for k in group_by)
        group = groups.setdefault(key, {"row": {k: row.get(k) for k in group_by + list(keep or [])},
                                        "measures": {m: [] for m in measures}, "weight": 0.0, "weighted": False})
        w = _as_float(row.get(weight)) if weight else None
        if w is not None:
            group["weighted"] = True
        w = w if w is not None and w > 0 else 1.0
        group["weight"] += w
        for m in measures:
            value = _as_float(row.get(m))
            if value is not None:
                group["measures"][m].append((value, w))
    out = []
    for n, (key, group) in enumerate(sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0]))):
        item = dict(group["row"])
        for m, pairs in group["measures"].items():
            if pairs:
                values = [v for v, _ in pairs]
                item[m], item[f"{m}_min"], item[f"{m}_max"] = _weighted_median(pairs), min(values), max(values)
        if weight:
            item[weight] = group["weight"] if group["weighted"] else None
        out.append(item)
    return out


def make_stats(rows: list[dict]) -> dict:
    """{kept_makes, make_rows, make_spellings}: the canonical makes of the kept rows (data/make_canonical.json), rows
    per make, and the spellings of each (a spelling that normalizes to no canonical make is counted as `_unmapped`)."""
    from .makes import canonical_of, spelling

    of: dict[str, str] = {}
    counts: dict[str, int] = {}
    spellings: dict[str, set[str]] = {}
    for row in rows:
        value = spelling(row.get("make"))
        if value not in of:
            of[value] = canonical_of(value) or "_unmapped"
        make = of[value]
        counts[make] = counts.get(make, 0) + 1
        spellings.setdefault(make, set()).add(value)
    return {"kept_makes": len([m for m in counts if m != "_unmapped"]), "make_rows": dict(sorted(counts.items())),
            "make_spellings": {m: sorted(v) for m, v in sorted(spellings.items()) if m != "_unmapped"}}


def compact(dataset: str, built: dict) -> dict:
    """Apply the dataset's `compaction` rules (data/open_datasets.json) to a built dataset: the make filter (E4: a
    spelling that normalizes to a canonical make of data/make_canonical.json; a model-gated make only with a catalog
    model match), the year window, and the grouping. Row ids are reassigned when grouping."""
    from .makes import row_filter

    rules = (ds.datasets().get(dataset) or {}).get("compaction") or {}
    rows = built["rows"]
    before = len(rows)
    if not ds.shard_spec(dataset):
        # D0: the text identity columns as stored and compared (a sharded dataset is normalized before its own
        # grouping and again by the shard writer)
        keys = ds.text_identity_keys()
        for row in rows:
            ds.normalize_identity(row, keys)
    if rules.get("makes") == "vocabulary":
        keep = row_filter()
        rows = [r for r in rows if keep(r.get("make"), " ".join(str(r.get(k) or "") for k in
                                                               ("model", "base_model", "variant", "version")))]
    year_key = rules.get("year_key") or "year"
    lo, hi = rules.get("min_year"), rules.get("max_year")
    if lo is not None or hi is not None:
        def in_window(row: dict) -> bool:
            year = ds._int(row.get(year_key))
            return year is not None and (lo is None or year >= lo) and (hi is None or year <= hi)
        rows = [r for r in rows if in_window(r)]
    if rules.get("group_by"):
        rows = group_configurations(rows, rules["group_by"], rules.get("measures") or [], rules.get("weight"),
                                    rules.get("keep"))
        prefix = rules.get("row_prefix") or dataset
        for n, row in enumerate(rows):
            row["row_id"] = f"{prefix}-{row.get(year_key)}-{n + 1}"
    stats = make_stats(rows)
    return {**built, "rows": rows, "make_spellings": stats["make_spellings"],
            "compaction": {"rows_before": before, "rows_after": len(rows), "kept_makes": stats["kept_makes"],
                           "make_rows": stats["make_rows"],
                           "rules": {k: v for k, v in rules.items() if not k.startswith("_")}}}


# --- one dataset -----------------------------------------------------------------------------------------------------------

def build_month() -> str:
    """D4: the build month (UTC, 'YYYY-MM') a monthly dataset's snapshot is named by."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_row_id(row_id: Any, month: str) -> str:
    """'ademe-17' -> 'ademe-2026-10-17': a row id unique across the months of a monthly dataset."""
    text = str(row_id or "")
    prefix, _, rest = text.partition("-")
    return f"{prefix}-{month}-{rest}" if rest else f"{month}-{text}"


def build_dataset(dataset: str, fetch: Fetcher | None = None, progress: Progress | None = None,
                  download: Downloader | None = None) -> dict:
    """Build one snapshot. {status: built | stopped | failed, rows, built_at, report, ...}; never raises."""
    from ..source_authority import dataset_policy

    progress = progress or _noop
    started = time.monotonic()
    cfg = ds.datasets().get(dataset)
    if not cfg:
        return {"status": "stopped", "reason": "unknown_dataset"}
    if cfg.get("identity_only"):
        return {"status": "skipped", "reason": "identity_only (on-demand decode, no snapshot)"}
    policy = dataset_policy(cfg.get("policy_id") or dataset)
    if policy["policy"] != "allowed" or not policy.get("bulk_store"):
        return {"status": "stopped", "reason": "policy", "policy": policy["policy"]}
    if fetch is None:
        fetch = default_fetcher()
        if download is None and cfg.get("csv_years"):
            download = default_downloader(int(float((cfg.get("csv_years") or {}).get("max_download_mb") or 6000)
                                              * 1024 * 1024))
    builder = cfg.get("builder") or "csv"
    try:
        if builder == "discodata":
            built = build_eea_all(fetch, download=download, progress=progress)
        elif builder == "data_fair":
            built = build_ademe(fetch, progress=progress)
        elif builder == "ckan_groups":
            built = build_ckan_groups(dataset, fetch, progress=progress)
        elif builder == "ckan_files":
            built = build_ckan_files(dataset, fetch, progress=progress)
        else:
            urls = cfg.get("download_urls") or []
            if not urls:
                raise BuildStopped("no_download_url", note=f"data/open_datasets.json {dataset}.download_urls is empty")
            built = build_csv_dataset(dataset, urls, fetch, progress=progress)
        built = compact(dataset, built)
        progress("writing snapshot")
        duration = round(time.monotonic() - started, 1)
        meta = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dataset": dataset,
                "source_url": cfg.get("source_url"), "urls": built.get("urls"), "schema": built.get("schema"),
                "licence": policy.get("licence"), "attribution": policy.get("attribution"),
                "years": built.get("years"), "files": built.get("files"), "absent_columns": built.get("absent_columns"),
                "file_years": built.get("file_years"), "last_file_year": built.get("last_file_year"),
                "build_duration_s": duration, "config_version": ds.config().get("version"),
                "column_units": built.get("column_units"), "unit_unknown_distribution": built.get(
                    "unit_unknown_distribution"), "compaction": built.get("compaction"),
                "make_spellings": built.get("make_spellings"), "catalogue_date": built.get("catalogue_date"),
                "catalogue": built.get("catalogue")}
        shards = months = None
        if ds.shard_spec(dataset):
            # S1: one shard per year; its meta holds no timestamp (an unchanged year gives the same bytes)
            status_used = {r["year"]: r.get("status_used") for r in built.get("years") or [] if r.get("year")
                           and r.get("status") in ("built", "partial")}
            shard_meta = {"source_url": cfg.get("source_url"), "licence": policy.get("licence"),
                          "attribution": policy.get("attribution")}
            shards = ds.write_shards(dataset, built["rows"], {k: v for k, v in shard_meta.items() if v is not None},
                                     status_used=status_used)
            size = sum(s["path"].stat().st_size for s in shards)
            shards = [{**{k: v for k, v in s.items() if k != "path"}, "path": str(s["path"]),
                       "absent_columns": (built.get("absent_columns") or {}).get(str(s["year"]))
                       if isinstance(built.get("absent_columns"), dict) else None} for s in shards]
        elif ds.history_spec(dataset):
            # D4: one snapshot per build month (<dataset>/<YYYY-MM>.sqlite); its meta holds no timestamp, so the same
            # catalogue rebuilt in the same month gives the same bytes; the row ids carry the month
            month = build_month()
            rows = [{**r, "row_id": _month_row_id(r.get("row_id"), month)} for r in built["rows"]]
            month_meta = {k: v for k, v in meta.items() if v is not None and k not in (
                "built_at", "build_duration_s", "years", "files", "compaction", "make_spellings", "urls")}
            path = ds.write_month_snapshot(dataset, rows, month_meta, month=month)
            size = path.stat().st_size
            catalogue = built.get("catalogue_date") or meta["built_at"]
            months = [{"file": f"{dataset}/{path.name}", "month": month, "catalogue_date": catalogue,
                       "year": ds._int(str(catalogue)[:4]), "part": None, "rows": len(rows), "path": str(path)}]
        else:
            path = ds.write_snapshot(dataset, built["rows"], {k: v for k, v in meta.items() if v is not None})
            size = path.stat().st_size if path and path.exists() else None
        return {"status": "built", "rows": len(built["rows"]), "built_at": meta["built_at"], "duration_s": duration,
                "size_bytes": size, "shards": shards, "years": built.get("years"), "files": built.get("files"),
                **({"months": months} if months is not None else {}),
                "absent_columns": built.get("absent_columns"), "last_file_year": built.get("last_file_year"),
                "column_units": built.get("column_units"),
                "unit_unknown_distribution": built.get("unit_unknown_distribution"),
                "compaction": built.get("compaction"), "make_spellings": built.get("make_spellings"),
                **({"catalogue_date": built.get("catalogue_date"), "catalogue": built["catalogue"]}
                   if built.get("catalogue") else {}),
                **({"csv_discovery": built["csv_discovery"]} if built.get("csv_discovery") is not None else {})}
    except BuildStopped as stop:
        return {"status": "stopped", "reason": stop.reason, "report": stop.report,
                "duration_s": round(time.monotonic() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - a failed build keeps the previous snapshot
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "duration_s": round(time.monotonic() - started, 1)}

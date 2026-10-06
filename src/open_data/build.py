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
                 K3: the years from `csv_years.from_year` that DISCODATA does not have ([latest] ends at 2022) come
                 from the EEA datahub's downloadable CSV (`build_eea_csv`: the records read from the datahub API, the
                 zip streamed to the runner disk and grouped locally exactly like the DISCODATA rows)
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
    fetch.json = fetch_json
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
    registrations; the parts of a split query are joined on the identity key."""
    keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
    merged: dict[tuple, dict] = {}
    for p, (measures, server_rows) in enumerate(parts):
        rows = [{**{k: raw.get(mapping[k]) for k in keys + measures}, "r": raw.get("r")} for raw in server_rows]
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


def build_eea(fetch: Fetcher, *, years: list[int] | None = None, progress: Progress = _noop) -> dict:
    """Year discovery, then per year its own schema, the make spellings present that year, and per (year, make
    spelling) the E1 query (or its two-part split); a query error is an error, never a year without cars, and a failing
    spelling stops only itself."""
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
        status = next((s for s in cfg.get("status_preference") or ["F", "P"] if s in statuses[year]), None)
        if status is None:
            reports.append({"year": year, "status": "skipped", "reason": "no_preferred_status",
                            "statuses": sorted(statuses[year])})
            continue
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
        resolved = resolve_map(year_header, columns)
        if resolved["missing"] or resolved["ambiguous"]:
            reports.append({"year": year, "status": "stopped", "reason": "schema_mismatch", "status_used": status,
                            "missing": resolved["missing"], "ambiguous": resolved["ambiguous"],
                            "live_header": year_header[:200]})
            continue
        mapping = resolved["mapping"]
        registrations = resolve_map(year_header, {"registrations": cfg["registrations_column"]})["mapping"].get(
            "registrations")
        report: dict[str, Any] = {"year": year, "status_used": status, "absent_columns": resolved["absent"],
                                  "registrations_column": registrations, "mode": "per_make"}
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
        queries = 0
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
            if len(plan) > 1:
                report["split"] = True
            found = [r for r in eea_aggregate(parts, mapping, cfg, status)
                     if keep(r.get("make"), " ".join(str(r.get(k) or "") for k in ("model", "variant", "version")))]
            for item in found:
                item["make"] = spelling(item.get("make"))
            built += found
        report.update(queries=queries, spellings=len(values), max_query_bytes=max(sizes) if sizes else None)
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
        reports.append({**report, "status": "partial" if errors else "built", "rows": len(built),
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


def eea_csv_sources(fetch: Fetcher, csv_cfg: dict) -> tuple[dict[int, dict], list[dict]]:
    """({year: {uuid, status, title, links}} preferring final records, the discovery report): the series' children and
    the listed records, read from the datahub API (`api` + uuid + `/related`). A record that cannot be read is
    reported, never fatal."""
    fetch_json = getattr(fetch, "json", fetch)
    api, pattern = str(csv_cfg.get("api") or ""), str(csv_cfg.get("title_pattern") or "")
    report: list[dict] = []
    records: dict[str, dict] = {}

    def related(uuid: str) -> dict | None:
        url = f"{api}{uuid}/related"
        try:
            data = json.loads(_decode(fetch_json(url)))
        except BuildStopped as stop:
            report.append({"uuid": uuid, "url": url, "status": "stopped", "reason": stop.reason})
            return None
        except Exception as exc:  # noqa: BLE001 - one record never costs the others
            report.append({"uuid": uuid, "url": url, "status": "failed", "error": f"{type(exc).__name__}: "
                                                                                f"{str(exc)[:200]}"})
            return None
        return datahub_related(data)

    for series in csv_cfg.get("series") or []:
        found = related(str(series.get("uuid")))
        if found is None:
            continue
        report.append({"uuid": series.get("uuid"), "kind": "series", "children": len(found["children"])})
        for child in found["children"]:
            parsed = title_year_status(child["title"], pattern)
            entry = {"uuid": child["uuid"], "kind": "child", "title": child["title"]}
            if parsed:
                records.setdefault(child["uuid"], {**entry, "year": parsed[0], "status": parsed[1]})
            else:
                report.append({**entry, "status": "skipped", "reason": "title without year / status"})
    for record in csv_cfg.get("records") or []:
        if record.get("uuid") and record.get("year") and record.get("status"):
            records.setdefault(str(record["uuid"]), {"uuid": str(record["uuid"]), "kind": "listed",
                                                     "year": int(record["year"]), "status": str(record["status"]),
                                                     "title": record.get("note")})
    by_year: dict[int, dict] = {}
    for record in sorted(records.values(), key=lambda r: (r["year"], r["status"] != "F")):
        if record["year"] in by_year:
            report.append({**record, "status_note": "superseded (final preferred)"})
            continue
        found = related(record["uuid"])
        if found is None:
            continue
        record["links"] = [o["url"] for o in found["onlines"]]
        report.append({k: record.get(k) for k in ("uuid", "kind", "year", "status", "title")} | {
            "links": record["links"][:20]})
        by_year[record["year"]] = record
    return by_year, report


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


def eea_csv_columns(cfg: dict) -> dict:
    """The column map with the CSV's `column_aliases` added to each key's accepted spellings."""
    aliases = ((cfg.get("csv_years") or {}).get("column_aliases")) or {}
    columns = {}
    for key, spec in (cfg.get("columns") or {}).items():
        spec = dict(spec)
        if key in aliases:
            known = list(spec.get("source") or [])
            spec["source"] = known + [a for a in aliases[key] if a not in known]
        columns[key] = spec
    return columns


def eea_csv_year(path: Path, year: int, status: str, *, progress: Progress = _noop) -> tuple[list[dict], dict]:
    """(rows, report) of one year's datahub CSV: streamed, filtered to the make values that are alias spellings or
    normalize to a canonical make, grouped exactly like the DISCODATA path (identity + measurement columns with
    SUM(r), then `eea_aggregate`). A header without a required key stops this year (`schema_mismatch`)."""
    from .makes import canonical_of, row_filter, spelling

    cfg = ds.datasets()["eea_co2_cars"]
    columns = eea_csv_columns(cfg)
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
    report.update(status="built", rows=len(built), rows_read=read, other_year_rows=skipped_year,
                  by_make=dict(sorted(by_make.items())))
    return built, report


def _rejoin(head: str, stream):
    """The lines of a text stream whose first 64 KB were already read (for the dialect sniff)."""
    lines = head.split("\n")
    yield from (line + "\n" for line in lines[:-1])
    yield lines[-1] + stream.readline()               # the line the 64 KB cut (or "" at the end)
    yield from stream


def build_eea_csv(fetch: Fetcher, *, skip_years: set[int] | None = None, download: Downloader | None = None,
                  work: Path | None = None, progress: Progress = _noop) -> dict:
    """K3: the years from `csv_years.from_year` that DISCODATA did not build, from the datahub CSV. {rows, years
    (one report per year), absent_columns, urls, discovery}; a year that fails stops only itself; never raises."""
    import shutil
    import tempfile

    cfg = ds.datasets()["eea_co2_cars"]
    csv_cfg = cfg.get("csv_years") or {}
    out: dict[str, Any] = {"rows": [], "years": [], "absent_columns": {}, "urls": [], "discovery": []}
    if not csv_cfg:
        return out
    progress("datahub records (2023+)")
    try:
        by_year, out["discovery"] = eea_csv_sources(fetch, csv_cfg)
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
        for year, record in sorted(by_year.items()):
            base = {"year": year, "status_used": record["status"], "source": "datahub_csv", "record": record["uuid"]}
            if year < floor or year in (skip_years or set()):
                out["years"].append({**base, "status": "skipped", "reason": "built from DISCODATA" if year >= floor
                                     else f"before csv_years.from_year {floor}"})
                continue
            files, seen = csv_candidates(record.get("links") or [], fetch, csv_cfg.get("file_pattern"))
            if len(files) != 1:
                out["years"].append({**base, "status": "stopped", "reason": "no_csv_link" if not files
                                     else "ambiguous_download", "files": files[:20], "links": seen[:40]})
                continue
            url = files[0]
            target = folder / f"{year}{''.join(Path(urlparse(url).path).suffixes[-2:]) or '.zip'}"
            progress(f"year {year} (csv): download {url.rsplit('/', 1)[-1]}")
            try:
                size = download(url, target)
                rows, report = eea_csv_year(target, year, record["status"], progress=progress)
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
            out["years"].append({**base, **report, "url": url, "download_bytes": size})
            if report.get("status") == "built":
                out["rows"] += rows
                out["urls"].append(url)
                out["absent_columns"][str(year)] = report.get("absent_columns") or []
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return out


def build_eea_all(fetch: Fetcher, *, download: Downloader | None = None, progress: Progress = _noop) -> dict:
    """The DISCODATA years, then the datahub CSV years DISCODATA does not have (K3); stops only when no year built."""
    stopped = None
    try:
        built = build_eea(fetch, progress=progress)
    except BuildStopped as stop:
        stopped = stop
        built = {"rows": [], "schema": None, "urls": [], "years": list(stop.report.get("years") or []),
                 "absent_columns": {}, "discodata": {"reason": stop.reason}}
    done = {r["year"] for r in built["years"] if r.get("status") in ("built", "partial")}
    extra = build_eea_csv(fetch, skip_years=done, download=download, work=ds.snapshot_dir(), progress=progress)
    built["rows"] += extra["rows"]
    built["years"] += [y for y in extra["years"] if y.get("year") not in done or y.get("status") != "skipped"]
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
    rows, report = _build_file(cfg["csv_url"], fetch, columns, id_prefix="ademe-", scales=scales,
                               decimal_comma=bool(cfg.get("decimal_comma")), body=body)
    if report["status"] != "built":
        raise BuildStopped(report.get("reason") or report["status"], files=[report])
    unknown = {k: distribution(rows, k) for k, info in units.items() if info.get("unit") == UNKNOWN_UNIT}
    report["units"] = {k: v.get("unit") for k, v in units.items()}
    return {"rows": rows, "schema": header, "urls": [cfg["csv_url"]], "files": [report],
            "absent_columns": report.get("absent_columns") or [], "column_units": units,
            "unit_unknown_distribution": unknown}


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
                                    decimal_comma=bool(cfg.get("decimal_comma")), extra={"group": group["group"]})
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
                "make_spellings": built.get("make_spellings")}
        shards = None
        if ds.shard_spec(dataset):
            # S1: one shard per year; its meta holds no timestamp (an unchanged year gives the same bytes)
            status_used = {r["year"]: r.get("status_used") for r in built.get("years") or [] if r.get("year")}
            shard_meta = {"source_url": cfg.get("source_url"), "licence": policy.get("licence"),
                          "attribution": policy.get("attribution")}
            shards = ds.write_shards(dataset, built["rows"], {k: v for k, v in shard_meta.items() if v is not None},
                                     status_used=status_used)
            size = sum(s["path"].stat().st_size for s in shards)
            shards = [{**{k: v for k, v in s.items() if k != "path"}, "path": str(s["path"]),
                       "absent_columns": (built.get("absent_columns") or {}).get(str(s["year"]))
                       if isinstance(built.get("absent_columns"), dict) else None} for s in shards]
        else:
            path = ds.write_snapshot(dataset, built["rows"], {k: v for k, v in meta.items() if v is not None})
            size = path.stat().st_size if path and path.exists() else None
        return {"status": "built", "rows": len(built["rows"]), "built_at": meta["built_at"], "duration_s": duration,
                "size_bytes": size, "shards": shards, "years": built.get("years"), "files": built.get("files"),
                "absent_columns": built.get("absent_columns"), "last_file_year": built.get("last_file_year"),
                "column_units": built.get("column_units"),
                "unit_unknown_distribution": built.get("unit_unknown_distribution"),
                "compaction": built.get("compaction"), "make_spellings": built.get("make_spellings"),
                **({"csv_discovery": built["csv_discovery"]} if built.get("csv_discovery") is not None else {})}
    except BuildStopped as stop:
        return {"status": "stopped", "reason": stop.reason, "report": stop.report,
                "duration_s": round(time.monotonic() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - a failed build keeps the previous snapshot
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "duration_s": round(time.monotonic() - started, 1)}

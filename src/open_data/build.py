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
                 schema (`TOP 1` of that year) and one grouped query (distinct configurations + sum of registrations),
                 only for the makes of identity_vocabulary `open_data_make_aliases`; a year that fails stops only itself
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

After the build, `compact` keeps only what matching and offers use (the dataset's `compaction` rules): the
vocabulary's makes, the year window, and for EEA one row per configuration with the median / min / max of each
measure (weighted by the registrations).

`progress(text)` (optional) is called with the current stage.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from . import datasets as ds

Fetcher = Callable[[str], bytes]
Progress = Callable[[str], None]
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
EEA_PAGE = 50_000
MAX_EEA_PAGES = 200
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


def eea_grouped_query(table: str, mapping: dict[str, str], cfg: dict, registrations: str | None, where: str,
                      makes: list[str]) -> str:
    """H2: grouped server-side by the identity keys only, the measurements aggregated (MIN / MAX / AVG over
    TRY_CAST float), the registrations summed over TRY_CAST bigint, no ORDER BY."""
    keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
    measures = [k for k in cfg.get("measure_keys") or [] if k in mapping]
    select = [f"{_q(mapping[k])} AS {_q(k)}" for k in keys]
    for key in measures:
        value = f"TRY_CAST({_q(mapping[key])} AS float)"
        select += [f"MIN({value}) AS {_q(key + '_min')}", f"MAX({value}) AS {_q(key + '_max')}",
                   f"AVG({value}) AS {_q(key + '_avg')}"]
    if registrations:
        select.append(f"SUM(TRY_CAST({_q(registrations)} AS bigint)) AS [registrations]")
    in_makes = ", ".join("'" + m.replace("'", "''") + "'" for m in makes)
    return (f"SELECT {', '.join(select)} FROM {table} WHERE {where} AND UPPER({_q(mapping['make'])}) IN ({in_makes}) "
            f"GROUP BY {', '.join(_q(mapping[k]) for k in keys)}")


def _eea_rows(chunk: list[dict], cfg: dict, mapping: dict, status: str) -> list[dict]:
    keys = [k for k in cfg.get("group_by_keys") or [] if k in mapping]
    measures = [k for k in cfg.get("measure_keys") or [] if k in mapping]
    out = []
    for raw in chunk:
        item = {key: raw.get(key) for key in keys}
        for key in measures:
            low, high, avg = (_as_float(raw.get(f"{key}_{s}")) for s in ("min", "max", "avg"))
            if low is None and high is None:
                continue
            item[f"{key}_min"], item[f"{key}_max"] = low, high
            item[key] = low if low == high else (round(avg, 3) if avg is not None else None)
        item["registrations"] = ds._int(raw.get("registrations"))
        item["status"] = status
        out.append(item)
    return out


def build_eea(fetch: Fetcher, *, years: list[int] | None = None, progress: Progress = _noop) -> dict:
    """Year discovery, then per year its own schema and one grouped query; a year whose query fails or fills a page
    is split by make (module docstring). A query error is an error, never a year without cars."""
    cfg = ds.datasets()["eea_co2_cars"]
    base, table = cfg["source_url"], cfg["table"]
    columns = cfg["columns"]

    def sql(query: str, page: int = 1) -> list[dict]:
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
        raise BuildStopped("no_makes", note="no open-data make aliases (data/open_data_make_aliases.json)")
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
                                  "registrations_column": registrations}
        built: list[dict] = []
        try:
            chunk = sql(eea_grouped_query(table, mapping, cfg, registrations, where, makes))
            if len(chunk) >= EEA_PAGE:
                raise EeaQueryError(f"a full page ({len(chunk)} rows): split by make")
            built, report["mode"] = _eea_rows(chunk, cfg, mapping, status), "year"
        except EeaQueryError as exc:
            report.update(mode="make_split", year_error=str(exc)[:500])
            errors: dict[str, str] = {}
            for make in makes:
                progress(f"year {year} (status {status}): {make}")
                try:
                    built += _eea_rows(sql_all(eea_grouped_query(table, mapping, cfg, registrations, where, [make])),
                                       cfg, mapping, status)
                except EeaQueryError as make_exc:
                    errors[make] = str(make_exc)[:500]
            if errors:
                report["make_errors"] = errors
            if len(errors) == len(makes):
                reports.append({**report, "status": "failed", "error": report["year_error"]})
                continue
        by_make: dict[str, int] = {}
        for n, item in enumerate(built):
            item["row_id"] = f"eea-{year}-{status}-{n + 1}"
            by_make[str(item.get("make") or "").upper()] = by_make.get(str(item.get("make") or "").upper(), 0) + 1
        rows += built
        absent_by_year[str(year)] = resolved["absent"]
        reports.append({**report, "status": "partial" if report.get("make_errors") else "built", "rows": len(built),
                        "by_make": dict(sorted(by_make.items()))})
    if not any(r["status"] in ("built", "partial") for r in reports):
        raise BuildStopped("no_year_built", years=reports, discovered={str(y): sorted(s) for y, s in statuses.items()})
    return {"rows": rows, "schema": header, "urls": [base], "years": reports,
            "absent_columns": absent_by_year}


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
        built, report = _build_file(resource["url"], fetch, cfg.get("columns"), id_prefix=f"{dataset}-{name}-",
                                    decimal_comma=bool(cfg.get("decimal_comma")),
                                    extra={"file_year": int(year.group(1))} if year else None)
        files.append({**report, "file_year": int(year.group(1)) if year else None})
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


def compact(dataset: str, built: dict) -> dict:
    """Apply the dataset's `compaction` rules (data/open_datasets.json) to a built dataset: the make filter (the
    identity vocabulary's makes), the year window, and the EEA grouping. Row ids are reassigned when grouping."""
    rules = (ds.datasets().get(dataset) or {}).get("compaction") or {}
    rows = built["rows"]
    before = len(rows)
    if rules.get("makes") == "vocabulary":
        makes = known_makes()
        rows = [r for r in rows if str(r.get("make") or "").strip().upper() in makes]
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
    return {**built, "rows": rows, "compaction": {"rows_before": before, "rows_after": len(rows),
                                                  "rules": {k: v for k, v in rules.items() if not k.startswith("_")}}}


# --- one dataset -----------------------------------------------------------------------------------------------------------

def build_dataset(dataset: str, fetch: Fetcher | None = None, progress: Progress | None = None) -> dict:
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
    fetch = fetch or default_fetcher()
    builder = cfg.get("builder") or "csv"
    try:
        if builder == "discodata":
            built = build_eea(fetch, progress=progress)
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
                    "unit_unknown_distribution"), "compaction": built.get("compaction")}
        path = ds.write_snapshot(dataset, built["rows"], {k: v for k, v in meta.items() if v is not None})
        size = path.stat().st_size if path and path.exists() else None
        return {"status": "built", "rows": len(built["rows"]), "built_at": meta["built_at"], "duration_s": duration,
                "size_bytes": size, "years": built.get("years"), "files": built.get("files"),
                "absent_columns": built.get("absent_columns"), "last_file_year": built.get("last_file_year"),
                "column_units": built.get("column_units"),
                "unit_unknown_distribution": built.get("unit_unknown_distribution"),
                "compaction": built.get("compaction")}
    except BuildStopped as stop:
        return {"status": "stopped", "reason": stop.reason, "report": stop.report,
                "duration_s": round(time.monotonic() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - a failed build keeps the previous snapshot
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "duration_s": round(time.monotonic() - started, 1)}

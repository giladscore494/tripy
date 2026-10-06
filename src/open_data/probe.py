"""G1 step 1: the live-schema probe of the build-open-data GitHub Action.

Per dataset (per year / per file), the live header or field schema and its resolution against the map of
data/open_datasets.json: matched / missing required / missing optional (and ambiguous). Plus what an operator needs to
fix a map without guessing:

    ADEME   each mapped column's unit as its schema description states it, and the value distribution (p5 / p50 / p95)
            of every column whose unit is not stated (the evidence for a `unit_overrides` entry)
    NRCan   every English CSV of the package with its group, its live header (the BEV / PHEV maps are written from it)
            and the files excluded (the original 2-cycle ratings)
    CVS     the data dictionary's rows (field names and units), the dictionary check of the mapped codes and the 2018 /
            2023 CSV headers

A dataset whose probe fails (`status: failed` / `stopped`) is not built by the same Action run (the failure stops only
that dataset). Written to data/open/probe.json (committed with the snapshots; MCP open_data_status returns it) and as
Markdown to the step summary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from . import datasets as ds
from .build import (BuildStopped, EeaQueryError, Fetcher, _csv_tables, _decode, _file_name, _languages, _resources,
                    _xls_rows, eea_grouped_query, eea_response, known_makes,
                    dictionary_check, distribution, override_units, resolve_map, unit_scales)

PROBE_VERSION = "open-data-probe-v1"


def _resolution(header: list[str], columns: dict) -> dict:
    out = resolve_map(header, columns)
    required = [k for k, v in columns.items() if isinstance(v, dict) and v.get("required", True)]
    return {"matched": out["mapping"], "missing_required": [m["key"] for m in out["missing"]],
            "missing_optional": out["absent"], "ambiguous": out["ambiguous"],
            "required_keys": required, "ok": not out["missing"] and not out["ambiguous"]}


def _distinct(rows: list[dict], column: str | None, limit: int = 2000) -> list[str]:
    """The distinct values of a source's make column (what scripts/build_make_aliases.py matches against)."""
    if not column:
        return []
    values = {" ".join(str(r.get(column) or "").split()).upper() for r in rows}
    values.discard("")
    return sorted(values)[:limit]


def probe_eea(fetch: Fetcher) -> dict:
    cfg = ds.datasets()["eea_co2_cars"]
    base, table = cfg["source_url"], cfg["table"]

    def raw(query: str, hits: int = 5000) -> bytes:
        return fetch(f"{base}?query={quote(query)}&p=1&nrOfHits={hits}")

    def sql(query: str, hits: int = 5000) -> list[dict]:
        return eea_response(raw(query, hits))

    sample = sql(f"SELECT TOP 1 * FROM {table}")
    header = list(sample[0].keys()) if sample else []
    head = resolve_map(header, {"year": cfg["columns"]["year"], "status": cfg["status_column"],
                                "make": cfg["columns"]["make"]})
    if head["missing"] or head["ambiguous"]:
        return {"status": "stopped", "reason": "schema_mismatch", "live_header": header, **_resolution(header, {
            "year": cfg["columns"]["year"], "status": cfg["status_column"], "make": cfg["columns"]["make"]})}
    year_col, status_col, make_col = head["mapping"]["year"], head["mapping"]["status"], head["mapping"]["make"]
    statuses: dict[int, set[str]] = {}
    for pair in sql(f"SELECT DISTINCT [{year_col}], [{status_col}] FROM {table}"):
        year = ds._int(pair.get(year_col))
        if year is not None:
            statuses.setdefault(year, set()).add(str(pair.get(status_col) or "").strip())
    try:
        makes = _distinct(sql(f"SELECT DISTINCT [{make_col}] FROM {table}", 50_000), make_col)
    except EeaQueryError as exc:
        makes, make_error = [], str(exc)[:500]
    else:
        make_error = None
    years = []
    floor = int(cfg.get("years_from") or 0)
    for year in sorted(y for y in statuses if y >= floor):
        status = next((s for s in cfg.get("status_preference") or ["F", "P"] if s in statuses[year]), None)
        if status is None:
            years.append({"year": year, "status": "skipped", "statuses": sorted(statuses[year])})
            continue
        rows = sql(f"SELECT TOP 1 * FROM {table} WHERE [{year_col}] = {int(year)} AND [{status_col}] = '{status}'")
        year_header = list(rows[0].keys()) if rows else []
        if not year_header:
            years.append({"year": year, "status": "skipped", "reason": "no_rows", "status_used": status})
            continue
        resolved = _resolution(year_header, cfg["columns"])
        years.append({"year": year, "status": "ok" if resolved["ok"] else "stopped", "status_used": status,
                      "live_header": year_header, **resolved})
    ok = any(y["status"] == "ok" for y in years)
    out = {"status": "ok" if ok else "stopped", "reason": None if ok else "no_year_resolves",
           "discovered": {str(y): sorted(s) for y, s in sorted(statuses.items())}, "years": years,
           "distinct_makes": makes, "distinct_makes_error": make_error}
    # H1: one grouped test query (one make, the latest resolving year, the real column list); the raw response head
    # goes to the step summary, so a server-side refusal is visible before the build
    latest = next((y for y in reversed(years) if y["status"] == "ok"), None)
    test_make = next((m for m in sorted(known_makes()) if m in set(makes)), None) or (sorted(known_makes()) or [None])[0]
    if latest and test_make:
        year_header = latest["live_header"]
        mapping = resolve_map(year_header, cfg["columns"])["mapping"]
        registrations = resolve_map(year_header, {"r": cfg["registrations_column"]})["mapping"].get("r")
        where = f"[{year_col}] = {int(latest['year'])} AND [{status_col}] = '{latest['status_used']}'"
        query = eea_grouped_query(table, mapping, cfg, registrations, where, [test_make])
        try:
            body = raw(query)
            head_text = _decode(body)[:1500]
            try:
                rows = eea_response(body)
                test = {"status": "ok", "rows": len(rows)}
            except EeaQueryError as exc:
                test = {"status": "failed", "error": str(exc)[:500]}
        except Exception as exc:  # noqa: BLE001
            head_text, test = "", {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        out["grouped_test"] = {**test, "year": latest["year"], "make": test_make, "query": query[:2000],
                               "response_head": head_text}
    return out


def probe_ademe(fetch: Fetcher) -> dict:
    from .datasets import UNKNOWN_UNIT

    cfg = ds.datasets()["ademe_car_labelling"]
    schema = json.loads(_decode(fetch(cfg["schema_url"])))
    tables = _csv_tables(fetch(cfg["csv_url"]))
    header = tables[0][2] if tables else []
    rows = tables[0][1] if tables else []
    resolved = _resolution(header, cfg["columns"])
    _, units = unit_scales(cfg["columns"], resolved["matched"], schema)
    canonical = [{key: row.get(column) for key, column in resolved["matched"].items()} for row in rows]
    unknown = {k: distribution(canonical, k) for k, info in units.items() if info.get("unit") == UNKNOWN_UNIT}
    fields = [{k: f.get(k) for k in ("key", "x-originalName", "title", "description", "x-unit", "type") if k in f}
              for f in (schema if isinstance(schema, list) else schema.get("schema") or schema.get("fields") or [])
              if isinstance(f, dict)]
    return {"status": "ok" if resolved["ok"] else "stopped", "reason": None if resolved["ok"] else "schema_mismatch",
            "live_header": header, "rows": len(rows), **resolved, "units": units,
            "unit_unknown_distribution": unknown, "schema_fields": fields,
            "distinct_makes": _distinct(rows, resolved["matched"].get("make"))}


def probe_csv(dataset: str, fetch: Fetcher) -> dict:
    cfg = ds.datasets()[dataset]
    files, makes = [], set()
    for url in cfg.get("download_urls") or []:
        tables = _csv_tables(fetch(url))
        for name, rows, header in tables:
            resolved = _resolution(header, cfg["columns"])
            makes.update(_distinct(rows, resolved["matched"].get("make")))
            files.append({"url": url, "table": name, "rows": len(rows), "live_header": header, **resolved})
    ok = bool(files) and all(f["ok"] for f in files)
    return {"status": "ok" if ok else "stopped", "reason": None if ok else
            ("no_download_url" if not files else "schema_mismatch"), "files": files, "distinct_makes": sorted(makes)}


def probe_ckan_groups(dataset: str, fetch: Fetcher) -> dict:
    cfg = ds.datasets()[dataset]
    fmt, lang = str(cfg.get("resource_format") or "CSV").upper(), str(cfg.get("resource_language") or "en").lower()
    excluded = [p.lower() for p in (cfg.get("exclude_resources") or {}).get("patterns") or []]
    files, makes = [], set()
    for resource in _resources(fetch, cfg["ckan_api"]):
        name = _file_name(resource)
        if str(resource.get("format") or "").upper() != fmt or (_languages(resource) and lang not in _languages(resource)):
            continue
        if any(p in f"{name} {str(resource.get('name') or '').lower()}" for p in excluded):
            files.append({"url": resource["url"], "status": "excluded"})
            continue
        group = next((g for g in cfg.get("resource_groups") or [] if any(p.lower() in name for p in g["patterns"])),
                     None)
        if group is None:
            files.append({"url": resource["url"], "status": "unclassified"})
            continue
        tables = _csv_tables(fetch(resource["url"]))
        header = tables[0][2] if tables else []
        rows = tables[0][1] if tables else []
        if group.get("columns") is None:
            files.append({"url": resource["url"], "group": group["group"], "status": "map_pending",
                          "live_header": header})
            continue
        resolved = _resolution(header, group["columns"])
        makes.update(_distinct(rows, resolved["matched"].get("make")))
        files.append({"url": resource["url"], "group": group["group"], "status": "ok" if resolved["ok"] else "stopped",
                      "live_header": header, **resolved})
    ok = any(f["status"] == "ok" for f in files)
    return {"status": "ok" if ok else "stopped", "reason": None if ok else "no_file_resolves", "files": files,
            "distinct_makes": sorted(makes)}


def probe_ckan_files(dataset: str, fetch: Fetcher, header_years: tuple[int, ...] = (2018, 2023)) -> dict:
    cfg = ds.datasets()[dataset]
    resources = _resources(fetch, cfg["ckan_api"])
    files_re, dict_re = re.compile(cfg["resource_pattern"], re.I), re.compile(cfg["dictionary_pattern"], re.I)
    out: dict[str, Any] = {"files_listed": sorted(_file_name(r) for r in resources if files_re.search(_file_name(r)))}
    dictionary = next((r for r in resources if dict_re.search(_file_name(r))), None)
    if dictionary is None:
        return {**out, "status": "stopped", "reason": "no_dictionary",
                "resources": [_file_name(r) for r in resources][:80]}
    try:
        rows = _xls_rows(fetch(dictionary["url"]))
    except BuildStopped as stop:
        return {**out, "status": "stopped", "reason": stop.reason, **stop.report}
    out["dictionary"] = {"url": dictionary["url"], "rows": rows[:200]}
    out["dictionary_problems"] = dictionary_check(rows, cfg.get("dictionary_check") or {}, cfg.get("unit_overrides"))
    out["unit_overrides"] = override_units(cfg.get("unit_overrides"))
    headers, makes = {}, set()
    unit_keys = [k for k, v in (cfg.get("columns") or {}).items() if isinstance(v, dict) and v.get("unit")]
    for resource in resources:
        name = _file_name(resource)
        year = re.search(r"(\d{4})", name)
        if files_re.search(name) and year and int(year.group(1)) in header_years:
            tables = _csv_tables(fetch(resource["url"]))
            header = tables[0][2] if tables else []
            file_rows = tables[0][1] if tables else []
            resolved = _resolution(header, cfg["columns"])
            canonical = [{k: row.get(c) for k, c in resolved["matched"].items()} for row in file_rows]
            makes.update(_distinct(file_rows, resolved["matched"].get("make")))
            # H4: each unit column's distribution, so a wrong unit override is visible
            headers[name] = {"live_header": header, **resolved,
                             "distributions": {k: distribution(canonical, k) for k in unit_keys
                                               if k in resolved["matched"]}}
    out["headers"] = headers
    out["distinct_makes"] = sorted(makes)
    ok = not out["dictionary_problems"] and bool(headers) and all(h["ok"] for h in headers.values())
    return {**out, "status": "ok" if ok else "stopped",
            "reason": None if ok else "dictionary_mismatch" if out["dictionary_problems"] else "schema_mismatch"}


def probe_dataset(dataset: str, fetch: Fetcher) -> dict:
    cfg = ds.datasets().get(dataset) or {}
    builder = cfg.get("builder") or "csv"
    try:
        if builder == "discodata":
            result = probe_eea(fetch)
        elif builder == "data_fair":
            result = probe_ademe(fetch)
        elif builder == "ckan_groups":
            result = probe_ckan_groups(dataset, fetch)
        elif builder == "ckan_files":
            result = probe_ckan_files(dataset, fetch)
        else:
            result = probe_csv(dataset, fetch)
    except BuildStopped as stop:
        result = {"status": "stopped", "reason": stop.reason, **stop.report}
    except Exception as exc:  # noqa: BLE001 - one dataset's probe never stops the others
        result = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"dataset": dataset, **result}


def probe(names: list[str], fetch: Fetcher) -> dict:
    return {"version": PROBE_VERSION, "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config_version": ds.config().get("version"),
            "datasets": {name: probe_dataset(name, fetch) for name in names}}


def _cols(items) -> str:
    return ", ".join(str(i) for i in items) if items else "—"


def markdown(report: dict) -> str:
    """The step-summary view of a probe."""
    lines = ["## Open-data live-schema probe", "", f"Probed at {report.get('probed_at')} "
             f"(map {report.get('config_version')}).", "",
             "| dataset | probe | reason |", "|---|---|---|"]
    for name, item in (report.get("datasets") or {}).items():
        lines.append(f"| {name} | {item.get('status')} | {item.get('reason') or item.get('error') or ''} |")
    for name, item in (report.get("datasets") or {}).items():
        lines += ["", f"### {name}"]
        for part in item.get("years") or []:
            lines.append(f"- {part.get('year')} ({part.get('status_used') or '—'}): {part.get('status')}; missing "
                         f"required: {_cols(part.get('missing_required'))}; missing optional: "
                         f"{_cols(part.get('missing_optional'))}")
        for part in item.get("files") or []:
            lines.append(f"- {str(part.get('url') or '').rsplit('/', 1)[-1]} [{part.get('group') or '—'}]: "
                         f"{part.get('status')}; missing required: {_cols(part.get('missing_required'))}; missing "
                         f"optional: {_cols(part.get('missing_optional'))}")
            if part.get("status") in ("map_pending", "stopped") and part.get("live_header"):
                lines.append(f"  - live header: `{' | '.join(part['live_header'][:80])}`")
        if "matched" in item and not item.get("files") and not item.get("years"):
            lines.append(f"- missing required: {_cols(item.get('missing_required'))}; missing optional: "
                         f"{_cols(item.get('missing_optional'))}")
        for key, info in (item.get("units") or {}).items():
            dist = (item.get("unit_unknown_distribution") or {}).get(key)
            lines.append(f"- unit of {key} ({info.get('column')}): {info.get('unit')}"
                         + (f" — stated: “{info.get('stated')}”" if info.get("stated") else "")
                         + (f" — p5 {dist['p5']}, p50 {dist['p50']}, p95 {dist['p95']} (n {dist['n']})" if dist
                            else ""))
        if item.get("distinct_makes") is not None:
            lines.append(f"- distinct makes: {len(item.get('distinct_makes') or [])}"
                         + (f" (error: {item['distinct_makes_error']})" if item.get("distinct_makes_error") else ""))
        test = item.get("grouped_test")
        if test:
            lines.append(f"- grouped test query ({test.get('make')}, {test.get('year')}): {test.get('status')}"
                         + (f", {test.get('rows')} rows" if test.get("rows") is not None else "")
                         + (f" — {test.get('error')}" if test.get("error") else ""))
            lines.append(f"  - response head: `{(test.get('response_head') or '')[:600].replace('`', chr(39))}`")
        if item.get("unit_overrides"):
            lines.append(f"- unit overrides: {json.dumps(item['unit_overrides'], ensure_ascii=False)}")
        if item.get("dictionary"):
            lines.append(f"- data dictionary problems: {json.dumps(item.get('dictionary_problems'), ensure_ascii=False)}")
            lines.append("- data dictionary rows:")
            lines += [f"  - {' | '.join(row)}" for row in (item["dictionary"].get("rows") or [])[:120]]
        for name_, header in (item.get("headers") or {}).items():
            lines.append(f"- {name_} header: `{' | '.join(header.get('live_header') or [])}`; missing required: "
                         f"{_cols(header.get('missing_required'))}")
            for key, dist in (header.get("distributions") or {}).items():
                if dist:
                    lines.append(f"  - {key}: p5 {dist['p5']}, p50 {dist['p50']}, p95 {dist['p95']} (n {dist['n']})")
    return "\n".join(lines) + "\n"

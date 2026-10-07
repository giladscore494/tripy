"""Y4: the EEA per-year schema audit. It answers which canonical keys are USABLE in a year, not only which exist.

The build computes each year's statistics while the rows still carry their registrations (`year_stats`, recorded in the
year's report: data/open/manifest.json `years[].audit`); the shards do not store registrations. scripts/audit_eea.py
(run by the build-open-data Action after the build) reads the manifest and the shards and writes
data/open/eea_schema_audit.md / .csv:

    per year     rows (the source rows the build read and the compacted rows), the status used, the live header and its
                 mapping to the canonical keys
    per key      type, null % (of the compacted rows), distinct count, coverage % weighted by registrations
    sanity       every shard row's year equals the shard year; no HTML / error body stored as data (DISCODATA bodies are
                 checked by `build.eea_response` before any row is read: H1); a final year has rows
    failures     the per-year failure list (a failed year never stops the others: S3)
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from . import datasets as ds

HTML = re.compile(r"<\s*(?:!doctype|html|head|body|title)\b|\bservice unavailable\b|\bincorrect syntax\b", re.I)
OK_STATUSES = ("built", "partial")


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "INTEGER" if value.is_integer() else "REAL"
    number = ds._number(value)
    if number is None:
        return "TEXT"
    return "INTEGER" if isinstance(number, int) else "REAL"


def _type(kinds: set[str]) -> str:
    if not kinds:
        return "—"
    if kinds == {"INTEGER"}:
        return "INTEGER"
    if kinds <= {"INTEGER", "REAL"}:
        return "REAL"
    return "TEXT" if kinds == {"TEXT"} else "MIXED"


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def year_stats(rows: list[dict], keys: list[str], mapping: dict[str, str], *, weight: str = "registrations") -> dict:
    """{rows, registrations, weighted, keys: {key: {state, column, type, null_pct, distinct, coverage_pct}}} of one
    year's built rows. state: mapped | absent (no live column) | empty (mapped, no row states a value). coverage_pct:
    the share of registrations whose row states the key (every row counts 1 when no row carries registrations)."""
    weights = [ds._number(r.get(weight)) for r in rows]
    weighted = any(w is not None and w > 0 for w in weights)
    weights = [(w if w is not None and w > 0 else 0) if weighted else 1 for w in weights]
    total = sum(weights)
    out: dict[str, dict] = {}
    for key in keys:
        if key not in mapping:
            out[key] = {"state": "absent", "column": None, "type": "—", "null_pct": 100.0 if rows else None,
                        "distinct": 0, "coverage_pct": 0.0 if rows else None}
            continue
        kinds: set[str] = set()
        distinct: set[str] = set()
        nulls, covered = 0, 0.0
        for row, w in zip(rows, weights):
            value = row.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                nulls += 1
                continue
            covered += w
            if len(kinds) < 4:
                kinds.add(_kind(value))
            distinct.add(str(value))
        out[key] = {"state": "mapped" if len(distinct) else "empty", "column": mapping[key], "type": _type(kinds),
                    "null_pct": _pct(nulls, len(rows)), "distinct": len(distinct),
                    "coverage_pct": _pct(covered, total)}
    return {"rows": len(rows), "registrations": int(total) if weighted else None, "weighted": weighted, "keys": out}


def empty_columns(stats: dict) -> list[str]:
    return sorted(k for k, v in (stats.get("keys") or {}).items() if v.get("state") == "empty")


# --- the report (scripts/audit_eea.py) ---------------------------------------------------------------------------------------

def shard_checks(path, year: int) -> dict:
    """{rows, other_year_rows, html_rows} of one decompressed shard: every row's year must equal the shard year; no
    text column may hold an HTML / error body."""
    import sqlite3

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(rows)")]
        rows = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        other = conn.execute("SELECT COUNT(*) FROM rows WHERE year IS NULL OR year != ?", (int(year),)).fetchone()[0]
        text = [c for c in columns if c in ("make", "model", "type_approval", "variant", "version", "fuel", "fuel_mode")]
        html = 0
        if text:
            for values in conn.execute(f"SELECT {', '.join(f'[{c}]' for c in text)} FROM rows"):
                if any(isinstance(v, str) and HTML.search(v) for v in values):
                    html += 1
    return {"rows": rows, "other_year_rows": other, "html_rows": html}


def failures(years: list[dict], shards: dict[int, dict], final_through: int | None) -> list[dict]:
    """The per-year failure list: a year report that is not built, a final year without rows, a shard with another
    year's rows or an HTML / error body."""
    out = []
    for report in years:
        year, status = report.get("year"), report.get("status")
        if status not in OK_STATUSES:
            out.append({"year": year, "problem": status, "reason": report.get("reason") or report.get("error"),
                        "source": report.get("source") or "discodata"})
            continue
        if status == "partial":
            out.append({"year": year, "problem": "partial", "reason": f"{len(report.get('make_errors') or {})} make "
                        "spelling(s) failed"})
        check = shards.get(year) or {}
        final = report.get("status_used") == "F" or (final_through is not None and year is not None
                                                      and int(year) <= int(final_through))
        if final and not (check.get("rows") or report.get("rows")):
            out.append({"year": year, "problem": "final_year_without_rows"})
        if check.get("other_year_rows"):
            out.append({"year": year, "problem": "other_year_rows", "rows": check["other_year_rows"]})
        if check.get("html_rows"):
            out.append({"year": year, "problem": "html_or_error_body", "rows": check["html_rows"]})
        if check.get("missing"):
            out.append({"year": year, "problem": "shard_missing", "reason": check["missing"]})
    return out


def _cell(value: Any) -> str:
    return "—" if value is None else str(value)


def _mb(size: Any) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if isinstance(size, (int, float)) else "—"


def latest_reports(years: list[dict]) -> dict[int, dict]:
    """One report per year: a built one (the datahub CSV report after the DISCODATA one) over a skipped one."""
    out: dict[int, dict] = {}
    for report in years or []:
        year = ds._int(report.get("year"))
        if year is None:
            continue
        if year not in out or report.get("status") in OK_STATUSES or out[year].get("status") not in OK_STATUSES:
            out[year] = report
    return dict(sorted(out.items()))


def markdown(years: dict[int, dict], shards: dict[int, dict], coverage_keys: list[str], problems: list[dict],
             run_url: str | None = None, detail: bool = True) -> str:
    """The audit as Markdown; `detail` adds each year's live header and per-key table (the file; the pull-request body
    carries the summary only)."""
    lines = ["## EEA schema audit (per year)", ""]
    if run_url:
        lines += [f"Build: {run_url}", ""]
    head = ["year", "status", "status used", "source rows", "rows", "registrations", "shard"] + coverage_keys
    lines += ["Coverage % weighted by registrations (`absent`: no live column; `0.0`: mapped, no value):", "",
              "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for year, report in years.items():
        audit = report.get("audit") or {}
        keys = audit.get("keys") or {}
        shard = shards.get(year) or {}
        cells = [str(year), _cell(report.get("status")) + (f" ({report.get('reason')})" if report.get("reason") else ""),
                 _cell(report.get("status_used")), _cell(report.get("source_rows")), _cell(report.get("rows")),
                 _cell(audit.get("registrations")), _mb(shard.get("bytes"))]
        for key in coverage_keys:
            info = keys.get(key)
            cells.append("—" if not info else "absent" if info["state"] == "absent" else _cell(info["coverage_pct"]))
        lines.append("| " + " | ".join(cells) + " |")
    for year, report in years.items() if detail else ():
        audit = report.get("audit")
        if not audit:
            continue
        lines += ["", f"### {year} ({report.get('status_used') or '—'}, {report.get('source') or 'discodata'})", "",
                  f"Live header: `{', '.join(map(str, report.get('live_header') or []))}`", "",
                  "| key | live column | state | type | null % | distinct | coverage % (reg.) |",
                  "|---|---|---|---|---|---|---|"]
        for key, info in (audit.get("keys") or {}).items():
            lines.append(f"| {key} | {_cell(info.get('column'))} | {info.get('state')} | {info.get('type')} | "
                         f"{_cell(info.get('null_pct'))} | {_cell(info.get('distinct'))} | "
                         f"{_cell(info.get('coverage_pct'))} |")
        if not audit.get("weighted"):
            lines.append("")
            lines.append("_No registrations in this year's rows: coverage counts rows._")
    lines += ["", "### Sanity checks", "",
              "| year | shard rows | rows of another year | HTML / error bodies |", "|---|---|---|---|"]
    for year in years:
        check = shards.get(year) or {}
        lines.append(f"| {year} | {_cell(check.get('rows'))} | {_cell(check.get('other_year_rows'))} | "
                     f"{_cell(check.get('html_rows'))} |")
    lines += ["", "DISCODATA bodies are parsed only through `eea_response` (H1): an HTML page, an error message or a body "
              "without `results` is a query error, never a year without cars.", "", "### Per-year failures", ""]
    lines += [f"- {p['year']}: {p['problem']}" + (f" ({p['reason']})" if p.get("reason") else "")
              + (f", {p['rows']} rows" if p.get("rows") else "") for p in problems] or ["- none"]
    return "\n".join(lines) + "\n"


CSV_HEADER = ["year", "status", "status_used", "source", "source_rows", "rows", "registrations", "key", "live_column",
              "state", "type", "null_pct", "distinct", "coverage_pct_weighted"]


def csv_text(years: dict[int, dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for year, report in years.items():
        audit = report.get("audit") or {}
        base = [year, report.get("status"), report.get("status_used"), report.get("source") or "discodata",
                report.get("source_rows"), report.get("rows"), audit.get("registrations")]
        keys = audit.get("keys") or {}
        if not keys:
            writer.writerow(base + [""] * 7)
        for key, info in keys.items():
            writer.writerow(base + [key, info.get("column"), info.get("state"), info.get("type"), info.get("null_pct"),
                                    info.get("distinct"), info.get("coverage_pct")])
    return buffer.getvalue()

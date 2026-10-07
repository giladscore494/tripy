"""Step 2-3 of the build-open-data GitHub Action: build each dataset with the builders of src/open_data/build.py
(compacted: src/open_data/build.compact) in a work folder on the runner's disk, then compress each snapshot to
data/open/<dataset>.sqlite.gz and record it in data/open/manifest.json (file, sha256, bytes, rows, years / files,
absent columns, units, built_at, the Action run URL). Never run on the server: the server reads the committed files.

A sharded dataset (EEA, `snapshot.shard_by: year`) is compressed per year to data/open/<dataset>/<year>.sqlite.gz and
its manifest entry lists the shards (file, year, part, status_used, rows, bytes, sha256). Every file must be under
--max-mb (50 MB): a year over it is first split by make initial (<year>-A-L, <year>-M-Z); a file still over it is not
written (`too_large`; the previous file of that year / dataset stays). Y1 size gate (the dataset's `size_gate`): when the
compressed shards of 2010-2016 together exceed 60 MB they are still written and the summary prints their sizes with a
warning. The summary ends with the per-year failure list (a year that was not built, and why).

D3: per sharded year, the row-count delta per canonical make against the shards currently in --out (data/open on
main); a changed count in a final year (status F) whose source rows are unchanged is a ::warning:: (after D0, two runs
of the same source give zero deltas).

A dataset whose probe (data/open/probe.json, step 1) did not resolve is not built; a dataset whose build stops or fails
keeps its previous file and manifest entry (its `last_attempt` records the failure, `build_status` is failed). One
dataset never costs the others: too-large or failed items are listed in the summary and the manifest and printed as
::warning::; the step exits 0 when at least one dataset or shard was written, 1 only when nothing was.

    python scripts/build_open_data.py [--datasets all|eea_co2_cars,...] [--out data/open] [--probe data/open/probe.json]
                                      [--work DIR] [--summary open-data-build.md] [--max-mb 50]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.open_data import datasets as ds  # noqa: E402
from src.open_data.build import build_dataset  # noqa: E402

MANIFEST_VERSION = "open-data-manifest-v1"
ENTRY_KEYS = ("rows", "years", "files", "absent_columns", "column_units", "unit_unknown_distribution", "csv_discovery",
              "determinism",
              "last_file_year", "compaction", "make_spellings", "catalogue_date", "catalogue")
EXPECTED_MAKES = {"epa_fueleconomy": 40, "tc_cvs": 35}      # E4: reported when lower, never a failure


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gzip_file(source: Path, target: Path) -> None:
    """Deterministic gzip (mtime 0, a fixed file name: the target's name without .gz, never a path): the same snapshot
    gives the same sha256."""
    tmp = target.with_name(f".{target.name}.tmp")
    with open(source, "rb") as src, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename=target.name.removesuffix(".gz"), mode="wb", fileobj=raw, mtime=0,
                           compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    os.replace(tmp, target)


def kept_rows(name: str, result: dict, probe: dict) -> tuple[Any, Any, str]:
    """(rows kept, rows total, basis) of a built dataset: the compaction's rows after / before; for EEA (only the alias
    spellings are queried) the probe's raw rows of the kept spellings / of every spelling."""
    compaction = result.get("compaction") or {}
    counts = ((probe.get("datasets") or {}).get(name) or {}).get("make_rows")
    if name == "eea_co2_cars" and isinstance(counts, dict) and counts:
        kept = {s for values in (result.get("make_spellings") or {}).values() for s in values}
        return sum(n for s, n in counts.items() if s in kept), sum(counts.values()), "raw rows (probe)"
    return compaction.get("rows_after"), compaction.get("rows_before"), "rows"


def makes_table(results: dict, probe: dict) -> list[str]:
    """E4: per dataset, rows kept / rows total and the kept canonical makes (lower than expected: reported only)."""
    lines = ["", "Makes kept per dataset (data/make_canonical.json):", "",
             "| dataset | rows kept / total | kept makes | expected | makes |", "|---|---|---|---|---|"]
    for name, r in results.items():
        if r.get("status") != "built":
            continue
        kept, total, basis = kept_rows(name, r, probe)
        makes = sorted(r.get("make_spellings") or {})
        expected = EXPECTED_MAKES.get(name)
        flag = f">= {expected}" + (" (**lower**)" if expected and len(makes) < expected else "") if expected else "—"
        lines.append(f"| {name} | {kept if kept is not None else '—'} / {total if total is not None else '—'} "
                     f"({basis}) | {len(makes)} | {flag} | {', '.join(makes)} |")
    return lines


def shard_table(results: dict) -> list[str]:
    """S1: per sharded dataset and year, the shard's size (compressed / SQLite) and rows."""
    lines = []
    for name, r in results.items():
        items = r.get("shard_items") or []
        if not items:
            continue
        lines += ["", f"Shards of {name} (data/open/{name}/):", "",
                  "| year | part | status | status_used | rows | compressed | sqlite |", "|---|---|---|---|---|---|---|"]
        for i in items:
            split = f" (year split from {_mb(i['split_from_bytes'])})" if i.get("split_from_bytes") else ""
            lines.append(f"| {i.get('year')} | {i.get('part') or '—'} | {i.get('status')}{split} | "
                         f"{i.get('status_used') or '—'} | {i.get('rows', '—')} | {_mb(i.get('bytes'))} | "
                         f"{_mb(i.get('raw_bytes'))} |")
    return lines


def size_gate(results: dict) -> list[str]:
    """Y1: the compressed size of the gated years' shards (data/open_datasets.json `size_gate`), with a warning
    over the limit (the shards are written either way)."""
    lines = []
    for name, r in results.items():
        gate = (ds.datasets().get(name) or {}).get("size_gate")
        items = [i for i in r.get("shard_items") or [] if i.get("status") == "ok"]
        if not gate or not items:
            continue
        low, high = (int(y) for y in gate["years"])
        gated = [i for i in items if low <= int(i.get("year") or 0) <= high]
        if not gated:
            continue
        total = sum(int(i.get("bytes") or 0) for i in gated)
        limit = float(gate.get("max_mb") or 0) * 1024 * 1024
        per_year = ", ".join(f"{i['year']}{'-' + i['part'] if i.get('part') else ''} {_mb(i.get('bytes'))}"
                             for i in gated)
        lines += ["", f"Size gate {name} {low}-{high}: {_mb(total)} compressed in {len(gated)} shard(s) ({per_year}); "
                      f"limit {gate.get('max_mb')} MB."]
        if total > limit:
            lines.append(f"**Warning:** the {low}-{high} shards of {name} exceed {gate.get('max_mb')} MB together "
                         f"({_mb(total)}); written anyway: the reviewer decides about moving them to a release asset.")
    return lines


def year_failures(results: dict) -> list[str]:
    """The per-year failure list: every year report of the run that was not built (S3: it never stopped the others)."""
    lines = ["", "Per-year failures:"]
    found = []
    for name, r in results.items():
        built = {p.get("year") for p in r.get("years") or [] if p.get("status") in ("built", "partial")}
        for part in r.get("years") or []:
            status = part.get("status")
            if status == "partial":
                found.append(f"- {name} {part.get('year')}: partial ({len(part.get('make_errors') or {})} make "
                             f"spelling(s) failed)")
            elif status not in ("built", "partial") and part.get("year") not in built:
                found.append(f"- {name} {part.get('year')}: {status} ({part.get('reason') or part.get('error') or '—'})"
                             f"{' [' + str(part['source']) + ']' if part.get('source') else ''}")
    return lines + (found or ["- none"])


def summary(results: dict, manifest: dict, probe: dict | None = None) -> str:
    lines = ["## Open-data build", "", "| dataset | status | rows | file | size | build time |", "|---|---|---|---|---|---|"]
    for name, r in results.items():
        entry = (manifest.get("datasets") or {}).get(name) or {}
        built = r.get("status") == "built"
        size = _mb(entry.get("bytes")) if built and entry.get("bytes") else "—"
        file = (f"{name}/ ({len(entry.get('shards') or [])} shards)" if entry.get("shards") else entry.get("file")) \
            if built else "—"
        lines.append(f"| {name} | {r.get('status')}{' (' + str(r.get('reason')) + ')' if r.get('reason') else ''} | "
                     f"{r.get('rows', '—')} | {file} | {size} | {r.get('duration_s', '—')} s |")
    lines += shard_table(results)
    lines += size_gate(results)
    lines += determinism_table(results)
    lines += datahub_section(results)
    lines += makes_table(results, probe or {})
    lines += ["", "Per year / file:"]
    for name, r in results.items():
        for part in r.get("years") or []:
            lines.append(f"- {name} {part.get('year')}: {part.get('status')} status={part.get('status_used')} "
                         f"rows={part.get('rows', '—')} {part.get('reason') or ''} "
                         f"queries={part.get('queries', '—')} max_query_bytes={part.get('max_query_bytes', '—')}"
                         f"{' split' if part.get('split') else ''} absent={part.get('absent_columns') or []}"
                         + (f" empty={part['empty_columns']}" if part.get("empty_columns") else "")
                         + (f" errors={len(part['make_errors'])}" if part.get("make_errors") else ""))
        for part in r.get("files") or []:
            lines.append(f"- {name} {str(part.get('url') or '').rsplit('/', 1)[-1]}: {part.get('status')} "
                         f"group={part.get('group') or '—'} rows={part.get('rows', '—')} {part.get('reason') or ''} "
                         f"absent={part.get('absent_columns') or []}")
        if r.get("compaction"):
            c = r["compaction"]
            lines.append(f"- {name} compaction: {c.get('rows_before')} -> {c.get('rows_after')} rows, "
                         f"{c.get('kept_makes')} makes")
        if r.get("status") != "built" and (r.get("report") or r.get("error")):
            lines.append(f"- {name} report: {json.dumps(r.get('report') or r.get('error'), ensure_ascii=False)[:3000]}")
    lines += year_failures(results)
    return "\n".join(lines) + "\n"


def _mb(size: Any) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if isinstance(size, (int, float)) else "—"


def compress_shard(name: str, shard: dict, staged_dir: Path, max_bytes: int) -> list[dict]:
    """The compressed shard of one year ({file, year, part, status_used, rows, bytes, raw_bytes, gz, status ok |
    too_large}); a year over max_bytes is first split by make initial (<year>-A-L, <year>-M-Z) and each part checked."""
    raw = Path(shard["path"])
    staged = staged_dir / f"{raw.name}.gz"
    gzip_file(raw, staged)
    size = staged.stat().st_size
    keep = ("file", "year", "part", "status_used", "rows", "absent_columns")
    if size <= max_bytes:
        return [{**{k: shard.get(k) for k in keep}, "file": f"{shard['file']}.gz", "bytes": size,
                 "raw_bytes": raw.stat().st_size, "gz": staged, "status": "ok"}]
    staged.unlink()
    out = []
    for part in ds.split_shard(name, raw):
        target = staged_dir / f"{part['path'].name}.gz"
        gzip_file(part["path"], target)
        part_size = target.stat().st_size
        item = {**{k: part.get(k) for k in keep}, "absent_columns": shard.get("absent_columns"),
                "file": f"{part['file']}.gz", "bytes": part_size, "raw_bytes": part["path"].stat().st_size,
                "gz": target, "split_from_bytes": size, "status": "ok"}
        if part_size > max_bytes:
            target.unlink()
            item.update(status="too_large", gz=None)
        out.append(item)
    return out


def make_counts(path: Path) -> dict[str, int]:
    """{canonical make: rows} of a plain shard (one GROUP BY; the canonical make per distinct spelling)."""
    import sqlite3

    from src.open_data.makes import canonical_of

    out: dict[str, int] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for make, count in conn.execute("SELECT make, COUNT(*) FROM rows GROUP BY make"):
            key = canonical_of(make) or str(make or "")
            out[key] = out.get(key, 0) + int(count)
    return out


def determinism(name: str, result: dict, out: Path, previous: dict, work: Path) -> list[dict]:
    """D3: per built year, {year, status_used, source_rows (previous, new), rows (previous, new), deltas {make: [prev,
    new]}, warning}: the new shard against the shard(s) of that year in `out` (before they are replaced)."""
    reports = {r.get("year"): r for r in result.get("years") or [] if r.get("status") in ("built", "partial")}
    before = {r.get("year"): r for r in previous.get("years") or [] if isinstance(r, dict)
              and r.get("status") in ("built", "partial")}
    old_shards: dict[int, list[dict]] = {}
    for shard in previous.get("shards") or []:
        if isinstance(shard, dict) and shard.get("year") is not None:
            old_shards.setdefault(int(shard["year"]), []).append(shard)
    folder = work / "d3"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for shard in result.get("shards") or []:
        year = int(shard["year"])
        new = make_counts(Path(shard["path"]))
        old: dict[str, int] | None = {} if old_shards.get(year) else None
        for item in old_shards.get(year) or []:
            source = out / str(item.get("file") or "")
            if not source.is_file():
                old = None
                break
            plain = folder / f"{year}-{item.get('part') or 'all'}.sqlite"
            with gzip.open(source, "rb") as src, open(plain, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            for make, count in make_counts(plain).items():
                old[make] = old.get(make, 0) + count
            plain.unlink()
        report = reports.get(year) or {}
        src_new = report.get("source_rows")
        src_old = (before.get(year) or {}).get("source_rows")
        entry = {"year": year, "status_used": shard.get("status_used"), "source_rows": [src_old, src_new],
                 "rows": [sum(old.values()) if old is not None else None, sum(new.values())]}
        if old is None:
            entry.update(deltas={}, note="no previous shard")
        else:
            entry["deltas"] = {m: [old.get(m, 0), new.get(m, 0)] for m in sorted(set(old) | set(new))
                               if old.get(m, 0) != new.get(m, 0)}
            entry["warning"] = bool(entry["deltas"] and shard.get("status_used") == "F" and src_old is not None
                                    and src_old == src_new)
            if src_old is None:
                entry["note"] = "previous source rows unknown (built before they were recorded)"
        rows.append(entry)
    return rows


def determinism_table(results: dict) -> list[str]:
    lines = []
    for name, r in results.items():
        items = r.get("determinism") or []
        if not items:
            continue
        lines += ["", f"Determinism (D3) of {name}: rows per canonical make against the shards on main:", "",
                  "| year | status | source rows (main -> new) | rows (main -> new) | makes changed | largest deltas | "
                  "warning |", "|---|---|---|---|---|---|---|"]
        for i in items:
            deltas = sorted((i.get("deltas") or {}).items(), key=lambda kv: -abs(kv[1][1] - kv[1][0]))
            shown = ", ".join(f"{m} {a}->{b}" for m, (a, b) in deltas[:5]) or i.get("note") or "none"
            src, rows = i.get("source_rows") or [None, None], i.get("rows") or [None, None]
            lines.append(f"| {i['year']} | {i.get('status_used') or '—'} | {src[0] if src[0] is not None else '—'} -> "
                         f"{src[1] if src[1] is not None else '—'} | {rows[0] if rows[0] is not None else '—'} -> "
                         f"{rows[1]} | {len(i.get('deltas') or {})} | {shown} | {'**yes**' if i.get('warning') else ''} |")
    return lines


def datahub_section(results: dict) -> list[str]:
    """D1 / D2: per datahub record its resources (class) and the file chosen per year; the range join per year."""
    lines = []
    for name, r in results.items():
        discovery = r.get("csv_discovery") or []
        if not discovery:
            continue
        lines += ["", f"Datahub records (D1) of {name}:", ""]
        for record in discovery:
            if record.get("kind") == "summary":
                lines.append(f"- {record.get('records_read')} record(s) read via {record.get('metadata_endpoint')} "
                             f"(depth <= {record.get('max_depth')}, <= {record.get('max_records')} records); per year: "
                             + json.dumps(record.get("years"), ensure_ascii=False))
                continue
            if record.get("status") == "failed":
                lines.append(f"- discovery failed: {record.get('error')}")
                continue
            lines.append(f"- {record.get('uuid')} [{record.get('kind')}, depth {record.get('depth')}] "
                         f"{record.get('title') or '—'}: year {record.get('year') or '—'} {record.get('status') or ''} "
                         f"({record.get('year_basis') or record.get('status_note') or 'no year'}); metadata "
                         f"{record.get('metadata') or '—'}, related {record.get('related') or '—'}")
            for resource in (record.get("resources") or [])[:20]:
                lines.append(f"  - {resource.get('class')}: {resource.get('url')}"
                             + (f" ({resource.get('protocol')})" if resource.get("protocol") else ""))
        for part in r.get("years") or []:
            if part.get("source") == "datahub_csv":
                lines.append(f"- {part.get('year')} [{part.get('mode') or 'csv'}]: {part.get('status')} "
                             + (f"{part.get('reason')} " if part.get("reason") else "")
                             + (f"url {part.get('url')} " if part.get("url") else "")
                             + (f"candidates {part.get('files')} " if part.get("files") else "")
                             + (f"join {part.get('join_pct')} % of {part.get('join_rows')} rows, range on "
                                f"{part.get('values_pct')} % ({part.get('values_pct_registrations')} % of "
                                f"registrations) " if part.get("mode") == "range_join" and part.get("join_rows")
                                else ""))
                if part.get("csv_live_header") or (part.get("live_header") and part.get("mode") != "range_join"):
                    header = part.get("csv_live_header") or part.get("live_header")
                    lines.append(f"  - CSV header: `{' | '.join(map(str, header))}`")
    return lines


def write_shards(name: str, result: dict, out: Path, work: Path, previous: dict, max_bytes: int,
                 run_url: str | None) -> tuple[dict, list[dict]]:
    """(the dataset's manifest entry, the shard items) of a sharded build: each year compressed (split when over the
    limit) and moved to data/open/<dataset>/; a year with a written shard replaces its previous shards, a year not built
    or too large keeps them."""
    staged_dir = work / "gz" / name
    staged_dir.mkdir(parents=True, exist_ok=True)
    items = [i for shard in result.get("shards") or [] for i in compress_shard(name, shard, staged_dir, max_bytes)]
    ok = [i for i in items if i["status"] == "ok"]
    if not ok:
        return previous, items
    replaced = {i["year"] for i in ok}
    kept = [s for s in previous.get("shards") or [] if isinstance(s, dict) and s.get("year") not in replaced
            and (out / str(s.get("file") or "")).is_file()]
    shards = []
    for item in ok:
        target = out / item["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item.pop("gz")), target)
        shards.append({**{k: item.get(k) for k in ("file", "year", "part", "status_used", "rows", "bytes")},
                       "sha256": ds.sha256_file(target)})
    shards = sorted(kept + shards, key=lambda s: (int(s.get("year") or 0), str(s.get("part") or "")))
    live = {str(s["file"]) for s in shards}
    stale = [str(s.get("file") or "") for s in previous.get("shards") or [] if isinstance(s, dict)]
    stale += [str(previous["file"])] if previous.get("file") else []      # the former single-file snapshot
    for file in stale:
        if file and file not in live and (out / file).is_file():
            (out / file).unlink()
    too_large = [{k: i.get(k) for k in ("year", "part", "rows", "bytes")} for i in items if i["status"] == "too_large"]
    entry = {"sharded": True, "shards": shards, "rows": sum(int(s.get("rows") or 0) for s in shards),
             "bytes": sum(int(s.get("bytes") or 0) for s in shards), "built_at": result.get("built_at"),
             "run_url": run_url, "build_status": "too_large" if too_large else "built",
             **{k: result.get(k) for k in ENTRY_KEYS if k != "rows" and result.get(k) is not None}}
    if too_large:
        entry["too_large"] = too_large
    return entry, items


def run(names: list[str], out: Path, work: Path, probe: dict, max_bytes: int, run_url: str | None,
        fetch=None) -> tuple[dict, dict, list[str]]:
    """(results per dataset, the new manifest, warnings: the too-large or failed items). Each result records how many
    files it `written` (a dataset file or its shards)."""
    out.mkdir(parents=True, exist_ok=True)
    manifest = ds._read_json(out / ds.MANIFEST_NAME) or {}
    entries = dict(manifest.get("datasets") or {})
    results, warnings = {}, []
    limit = f"{max_bytes / 1024 / 1024:.0f} MB"
    ds.set_snapshot_dir(work)
    try:
        for name in names:
            probed = (probe.get("datasets") or {}).get(name)
            if probed is not None and probed.get("status") != "ok":
                result = {"status": "skipped", "reason": f"probe {probed.get('status')}: "
                                                         f"{probed.get('reason') or probed.get('error')}"}
            else:
                result = build_dataset(name, fetch)
            results[name] = result
            result["written"] = 0
            entry = dict(entries.get(name) or {})
            attempt = {"at": _utc(), "status": result.get("status"), "reason": result.get("reason")
                       or result.get("error"), "run_url": run_url}
            if result.get("status") == "built" and result.get("shards") is not None:
                try:
                    result["determinism"] = determinism(name, result, out, entry, work)
                except Exception as exc:  # noqa: BLE001 - the check never costs the build
                    result["determinism"] = []
                    warnings.append(f"{name}: determinism check failed ({type(exc).__name__}: {str(exc)[:200]})")
                for item in result["determinism"]:
                    if item.get("warning"):
                        warnings.append(f"{name} {item['year']}: final year, same source rows "
                                        f"({item['source_rows'][1]}), but {len(item['deltas'])} make(s) changed row "
                                        f"count (D3)")
                entry, items = write_shards(name, result, out, work, entry, max_bytes, run_url)
                entry = dict(entry)
                result["shard_items"] = [{k: v for k, v in i.items() if k != "gz"} for i in items]
                result["written"] = sum(1 for i in items if i["status"] == "ok")
                too_large = [i for i in items if i["status"] == "too_large"]
                for item in too_large:
                    warnings.append(f"{name} {item['year']}{'-' + item['part'] if item.get('part') else ''}: "
                                    f"{_mb(item['bytes'])} compressed (split by make initial), over the {limit} "
                                    f"limit (not written)")
                if not result["written"]:
                    result["status"] = entry["build_status"] = "too_large" if too_large else "failed"
                    result["reason"] = "every shard over the limit" if too_large else "no shard (no rows)"
                    if not too_large:
                        warnings.append(f"{name}: built without rows, nothing written; the previous snapshot stays")
                attempt.update(status=result["status"],
                               reason=f"{len(too_large)} shard(s) over {limit}" if too_large else None)
            elif result.get("status") == "built":
                target = out / f"{name}.sqlite.gz"
                staged = work / f"{name}.sqlite.gz"
                gzip_file(work / f"{name}.sqlite", staged)
                size = staged.stat().st_size
                if size > max_bytes:
                    warnings.append(f"{name}: {_mb(size)} compressed, over the {limit} limit (not written)")
                    attempt.update(status="too_large", reason=f"{size} bytes")
                    result["status"], result["reason"] = "too_large", f"{size} bytes"
                    entry["build_status"] = "too_large"
                else:
                    shutil.move(str(staged), target)
                    for shard in entry.get("shards") or []:                 # a formerly sharded dataset
                        if isinstance(shard, dict) and shard.get("file") and (out / str(shard["file"])).is_file():
                            (out / str(shard["file"])).unlink()
                    entry = {"file": target.name, "sha256": ds.sha256_file(target), "bytes": size,
                             "built_at": result.get("built_at"), "run_url": run_url, "build_status": "built",
                             **{k: result.get(k) for k in ENTRY_KEYS if result.get(k) is not None}}
                    result["written"] = 1
            elif result.get("status") in ("stopped", "failed"):
                warnings.append(f"{name}: {result['status']} ({result.get('reason') or result.get('error')}); the "
                                f"previous snapshot stays")
                if entry:
                    entry["build_status"] = "failed"
            entry["last_attempt"] = attempt
            entries[name] = entry
    finally:
        ds.set_snapshot_dir(None)
    manifest = {"version": MANIFEST_VERSION, "built_at": _utc(), "run_url": run_url,
                "config_version": ds.config().get("version"), "datasets": entries}
    (out / ds.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str) + "\n", "utf-8")
    return results, manifest, warnings


def main(argv: list[str] | None = None) -> int:
    from probe_open_data import selected

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datasets", default="all")
    parser.add_argument("--out", default=str(ROOT / "data" / "open"))
    parser.add_argument("--probe", default=str(ROOT / "data" / "open" / "probe.json"))
    parser.add_argument("--work", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--max-mb", type=float, default=50.0)
    args = parser.parse_args(argv)
    probe = ds._read_json(Path(args.probe)) if args.probe else {}
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="open-data-"))
    work.mkdir(parents=True, exist_ok=True)
    run_url = os.environ.get("OPEN_DATA_RUN_URL") or None
    results, manifest, warnings = run(selected(args.datasets), Path(args.out), work, probe,
                                      int(args.max_mb * 1024 * 1024), run_url)
    written = sum(int(r.get("written") or 0) for r in results.values())
    text = summary(results, manifest, probe) + "".join(f"\n**Not written:** {w}\n" for w in warnings)
    if not written:
        text += "\n**Nothing was written** (no dataset or shard built under the limit).\n"
    if args.summary:
        Path(args.summary).write_text(text, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    for warning in warnings:
        print(f"::warning::{warning}", file=sys.stderr)
    if not written:
        print("::error::nothing was written: no dataset or shard built under the limit", file=sys.stderr)
    return 0 if written else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())

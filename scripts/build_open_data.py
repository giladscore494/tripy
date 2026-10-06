"""Step 2-3 of the build-open-data GitHub Action: build each dataset with the builders of src/open_data/build.py
(compacted: src/open_data/build.compact) in a work folder on the runner's disk, then compress each snapshot to
data/open/<dataset>.sqlite.gz and record it in data/open/manifest.json (file, sha256, bytes, rows, years / files,
absent columns, units, built_at, the Action run URL). Never run on the server: the server reads the committed files.

A dataset whose probe (data/open/probe.json, step 1) did not resolve is not built; a dataset whose build stops or fails
keeps its previous file and manifest entry (its `last_attempt` records the failure). Every file must be under
--max-mb (50 MB): a larger one is not written and the step fails with its size.

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
ENTRY_KEYS = ("rows", "years", "files", "absent_columns", "column_units", "unit_unknown_distribution",
              "last_file_year", "compaction", "make_spellings")
EXPECTED_MAKES = {"epa_fueleconomy": 40, "tc_cvs": 35}      # E4: reported when lower, never a failure


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gzip_file(source: Path, target: Path) -> None:
    """Deterministic gzip (no name / mtime in the header): the same snapshot gives the same sha256."""
    tmp = target.with_name(f".{target.name}.tmp")
    with open(source, "rb") as src, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as dst:
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


def summary(results: dict, manifest: dict, probe: dict | None = None) -> str:
    lines = ["## Open-data build", "", "| dataset | status | rows | file | size | build time |", "|---|---|---|---|---|---|"]
    for name, r in results.items():
        entry = (manifest.get("datasets") or {}).get(name) or {}
        size = f"{entry['bytes'] / 1024 / 1024:.1f} MB" if r.get("status") == "built" and entry.get("bytes") else "—"
        lines.append(f"| {name} | {r.get('status')}{' (' + str(r.get('reason')) + ')' if r.get('reason') else ''} | "
                     f"{r.get('rows', '—')} | {entry.get('file') if r.get('status') == 'built' else '—'} | {size} | "
                     f"{r.get('duration_s', '—')} s |")
    lines += makes_table(results, probe or {})
    lines += ["", "Per year / file:"]
    for name, r in results.items():
        for part in r.get("years") or []:
            lines.append(f"- {name} {part.get('year')}: {part.get('status')} status={part.get('status_used')} "
                         f"rows={part.get('rows', '—')} {part.get('reason') or ''} "
                         f"queries={part.get('queries', '—')} max_query_bytes={part.get('max_query_bytes', '—')}"
                         f"{' split' if part.get('split') else ''} absent={part.get('absent_columns') or []}"
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
    return "\n".join(lines) + "\n"


def run(names: list[str], out: Path, work: Path, probe: dict, max_bytes: int, run_url: str | None,
        fetch=None) -> tuple[dict, dict, list[str]]:
    """(results per dataset, the new manifest, errors that fail the step)."""
    out.mkdir(parents=True, exist_ok=True)
    manifest = ds._read_json(out / ds.MANIFEST_NAME) or {}
    entries = dict(manifest.get("datasets") or {})
    results, errors = {}, []
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
            entry = dict(entries.get(name) or {})
            attempt = {"at": _utc(), "status": result.get("status"), "reason": result.get("reason")
                       or result.get("error"), "run_url": run_url}
            if result.get("status") == "built":
                target = out / f"{name}.sqlite.gz"
                staged = work / f"{name}.sqlite.gz"
                gzip_file(work / f"{name}.sqlite", staged)
                size = staged.stat().st_size
                if size > max_bytes:
                    errors.append(f"{name}: {size / 1024 / 1024:.1f} MB compressed, over the {max_bytes / 1024 / 1024:.0f}"
                                  f" MB limit (not written)")
                    attempt.update(status="too_large", reason=f"{size} bytes")
                    result["status"], result["reason"] = "too_large", f"{size} bytes"
                else:
                    shutil.move(str(staged), target)
                    entry = {"file": target.name, "sha256": ds.sha256_file(target), "bytes": size,
                             "built_at": result.get("built_at"), "run_url": run_url, "build_status": "built",
                             **{k: result.get(k) for k in ENTRY_KEYS if result.get(k) is not None}}
            entry["last_attempt"] = attempt
            entries[name] = entry
    finally:
        ds.set_snapshot_dir(None)
    manifest = {"version": MANIFEST_VERSION, "built_at": _utc(), "run_url": run_url,
                "config_version": ds.config().get("version"), "datasets": entries}
    (out / ds.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str) + "\n", "utf-8")
    return results, manifest, errors


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
    results, manifest, errors = run(selected(args.datasets), Path(args.out), work, probe,
                                    int(args.max_mb * 1024 * 1024), run_url)
    text = summary(results, manifest, probe) + ("".join(f"\n**Failed:** {e}\n" for e in errors))
    if args.summary:
        Path(args.summary).write_text(text, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())

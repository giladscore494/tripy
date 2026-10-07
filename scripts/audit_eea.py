"""Y4: the EEA per-year schema audit, run by the build-open-data Action after the build (src/open_data/audit.py).

Reads data/open/manifest.json (the year reports of the build: status used, rows, live header, mapping and the per-key
statistics the build computed while the registrations were known) and the committed shards (each verified by its
sha256 and decompressed exactly as the engine reads them) and writes:

    data/open/eea_schema_audit.md    per year: rows, status, the live header and its mapping; per key: type, null %,
                                     distinct count, coverage % weighted by registrations; the sanity checks (every
                                     row's year equals the shard year, no HTML / error body as data, a final year has
                                     rows) and, last, the per-year failure list
    data/open/eea_schema_audit.csv   one row per (year, key)

and appends the Markdown to $GITHUB_STEP_SUMMARY when set. A failed year never stops the others; the script exits 0 and
prints a ::warning:: per failure.

    python scripts/audit_eea.py [--open data/open] [--md data/open/eea_schema_audit.md]
                                [--csv data/open/eea_schema_audit.csv] [--summary eea-audit-summary.md]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.open_data import audit  # noqa: E402
from src.open_data import datasets as ds  # noqa: E402

DATASET = "eea_co2_cars"


def shard_checks(entry: dict) -> dict[int, dict]:
    """{year: {rows, other_year_rows, html_rows, bytes, missing}} over the year's shards (parts summed)."""
    out: dict[int, dict] = {}
    for shard in entry.get("shards") or []:
        if not isinstance(shard, dict) or ds._int(shard.get("year")) is None:
            continue
        year = int(shard["year"])
        item = out.setdefault(year, {"rows": 0, "other_year_rows": 0, "html_rows": 0, "bytes": 0})
        item["bytes"] += int(shard.get("bytes") or 0)
        path, problem = (ds._materialize_file(DATASET, str(shard["file"]), str(shard["sha256"]))
                         if shard.get("file") and shard.get("sha256") else (None, "no_manifest_entry"))
        if path is None:
            item["missing"] = f"{shard.get('file')}: {problem}"
            continue
        for key, value in audit.shard_checks(path, year).items():
            item[key] += value
    return out


def run(open_dir: Path, temp_dir: Path | None = None) -> tuple[str, str, list[dict], str]:
    """(markdown, csv, failures, the markdown without the per-year detail) of the committed EEA snapshot."""
    ds.set_repo_dir(open_dir, temp_dir or Path(tempfile.mkdtemp(prefix="eea-audit-")))
    try:
        manifest = ds.manifest()
        entry = (manifest.get("datasets") or {}).get(DATASET) or {}
        cfg = ds.datasets().get(DATASET) or {}
        reports = audit.latest_reports(entry.get("years") or [])
        checks = shard_checks(entry)
        for year in checks:                       # a shard of a year this build did not report (kept from before)
            reports.setdefault(year, {"year": year, "status": "built", "note": "shard kept from an earlier build"})
        reports = dict(sorted(reports.items()))
        problems = audit.failures(list(reports.values()), checks, cfg.get("final_only_through_year"))
        keys = cfg.get("audit_coverage_keys") or []
        run_url = entry.get("run_url") or manifest.get("run_url")
        text = audit.markdown(reports, checks, keys, problems, run_url)
        short = audit.markdown(reports, checks, keys, problems, run_url, detail=False)
        return text, audit.csv_text(reports), problems, short
    finally:
        ds.set_repo_dir(None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--open", default=str(ROOT / "data" / "open"))
    parser.add_argument("--md", default=str(ROOT / "data" / "open" / "eea_schema_audit.md"))
    parser.add_argument("--csv", default=str(ROOT / "data" / "open" / "eea_schema_audit.csv"))
    parser.add_argument("--summary", default="", help="the audit without the per-year detail (the pull-request body)")
    args = parser.parse_args(argv)
    text, table, problems, short = run(Path(args.open))
    if args.summary:
        Path(args.summary).write_text(short, "utf-8")
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text(text, "utf-8")
    Path(args.csv).write_text(table, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    for problem in problems:
        print(f"::warning::EEA {problem['year']}: {problem['problem']} {problem.get('reason') or ''}".rstrip(),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

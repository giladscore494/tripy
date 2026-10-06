"""Step 4 of the build-open-data GitHub Action (BEFORE the pull-request step: create-pull-request resets the checkout,
so a later step would read main's files): the coverage of the newly built snapshots in data/open/.

    1. rows per dataset / year / file and the absent columns, from data/open/manifest.json (+ the probe's status)
    2. for the 50 benchmark records (data/benchmark_v1_level15_snapshot.json) plus 22010
       (data/open_data_coverage_records.json; 85095, 23678, 38626 and 101136 are benchmark records): the approval
       route and the match status per source, read from the committed snapshots exactly as the engine reads them
       (sha256 verified, decompressed to the temp dir)

Prints Markdown, appends it to $GITHUB_STEP_SUMMARY when set, and writes the pull-request body (--body).

    python scripts/open_data_coverage.py [--open data/open] [--body open-data-pr-body.md] [--build open-data-build.md]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import build_level15_payload  # noqa: E402
from src.gov_registry import identity_fingerprint  # noqa: E402
from src.open_data import datasets as ds  # noqa: E402
from src.open_data.match import match  # noqa: E402
from src.open_data.offers import field_offers  # noqa: E402

FOCUS = ("85095", "23678", "38626", "101136", "22010")


def records() -> list[dict]:
    rows = json.loads((ROOT / "data" / "benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]
    extra = ROOT / "data" / "open_data_coverage_records.json"
    if extra.is_file():
        known = {r["upstream_record_id"] for r in rows}
        rows += [r for r in json.loads(extra.read_text("utf-8"))["rows"] if r["upstream_record_id"] not in known]
    return rows


def snapshot_section(manifest: dict, probe: dict) -> list[str]:
    lines = ["### Snapshots", "", f"Manifest built {manifest.get('built_at') or '—'}"
             + (f" by {manifest['run_url']}" if manifest.get("run_url") else "") + ".", "",
             "| dataset | probe | build | rows | size | absent columns |", "|---|---|---|---|---|---|"]
    for name, cfg in ds.datasets().items():
        if cfg.get("identity_only"):
            continue
        entry = (manifest.get("datasets") or {}).get(name) or {}
        attempt = entry.get("last_attempt") or {}
        probed = ((probe.get("datasets") or {}).get(name) or {}).get("status") or "—"
        size = f"{entry['bytes'] / 1024 / 1024:.1f} MB" if entry.get("bytes") else "—"
        absent = json.dumps(entry.get("absent_columns"), ensure_ascii=False) if entry.get("absent_columns") else "—"
        lines.append(f"| {name} | {probed} | {attempt.get('status') or entry.get('build_status') or 'never built'}"
                     f"{' (' + str(attempt.get('reason')) + ')' if attempt.get('reason') else ''} | "
                     f"{entry.get('rows', '—')} | {size} | {absent} |")
    for name, entry in (manifest.get("datasets") or {}).items():
        for part in entry.get("years") or []:
            if isinstance(part, dict):
                lines.append(f"- {name} {part.get('year')}: {part.get('status')} ({part.get('status_used') or '—'}) "
                             f"rows {part.get('rows', '—')}")
        for shard in entry.get("shards") or []:
            if isinstance(shard, dict):
                lines.append(f"- {name} shard {shard.get('file')}: {shard.get('status_used') or '—'} rows "
                             f"{shard.get('rows', '—')}, {(shard.get('bytes') or 0) / 1024 / 1024:.1f} MB")
        for part in entry.get("too_large") or []:
            if isinstance(part, dict):
                lines.append(f"- {name} {part.get('year')}{'-' + part['part'] if part.get('part') else ''}: too large "
                             f"({(part.get('bytes') or 0) / 1024 / 1024:.1f} MB), not written")
        for part in entry.get("files") or []:
            if isinstance(part, dict):
                lines.append(f"- {name} {str(part.get('url') or '').rsplit('/', 1)[-1]}: {part.get('status')} "
                             f"[{part.get('group') or '—'}] rows {part.get('rows', '—')}")
        for key, dist in (entry.get("unit_unknown_distribution") or {}).items():
            if dist:
                lines.append(f"- {name} {key}: unit unknown (no offers until unit_overrides) — p5 {dist['p5']}, "
                             f"p50 {dist['p50']}, p95 {dist['p95']} (n {dist['n']})")
    return lines


def coverage_section(rows: list[dict]) -> tuple[list[str], dict]:
    sources = [n for n, c in ds.datasets().items() if not c.get("identity_only")]
    lines = ["### Route and match per record", "", "| record | vehicle | route | level | "
             + " | ".join(sources) + " | offered |", "|" + "---|" * (len(sources) + 5)]
    by_route: Counter = Counter()
    matched: Counter = Counter()
    order = sorted(rows, key=lambda r: (r["upstream_record_id"] not in FOCUS, r["upstream_record_id"]))
    for row in order:
        payload = build_level15_payload(row)
        result = match(identity_fingerprint(payload), payload)
        route = result.get("route_detail") or result.get("route")
        by_route[route] += 1
        if result.get("level"):
            matched[route] += 1
        offered = sum(1 for o in field_offers(result) if o.get("status") == "offered")
        ident = payload.get("identity") or {}
        mark = "**" if row["upstream_record_id"] in FOCUS else ""
        lines.append(f"| {mark}{row['upstream_record_id']}{mark} | {ident.get('manufacturer')} "
                     f"{ident.get('commercial_name')} {ident.get('year')} | {route} | {result.get('level') or '—'} | "
                     + " | ".join(str((result['sources'].get(s) or {}).get('status')) for s in sources)
                     + f" | {offered} |")
    summary = {"records": len(order), "by_route": dict(by_route), "with_level": dict(matched)}
    return lines, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--open", default=str(ROOT / "data" / "open"))
    parser.add_argument("--body", default="")
    parser.add_argument("--build", default="", help="the build step's summary, included in the PR body")
    parser.add_argument("--makes", default="", help="the make-alias review list, included in the PR body")
    args = parser.parse_args(argv)
    ds.set_repo_dir(Path(args.open))
    manifest, probe = ds.manifest(), ds.probe_summary()
    lines = ["## Open-data coverage", ""] + snapshot_section(manifest, probe) + [""]
    table, summary = coverage_section(records())
    lines += [f"Records: {summary['records']}; by route: {summary['by_route']}; with a match level: "
              f"{summary['with_level']}", ""] + table
    text = "\n".join(lines) + "\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    if args.body:
        build = Path(args.build).read_text("utf-8") if args.build and Path(args.build).is_file() else ""
        build += ("\n" + Path(args.makes).read_text("utf-8")) if args.makes and Path(args.makes).is_file() else ""
        body = ("Regenerated by the build-open-data workflow (scripts/probe_open_data.py, scripts/build_open_data.py). "
                "The snapshots in data/open/ are read by the server from the deploy image; nothing is built or written "
                "on the data volume.\n\n" + build + "\n" + text)
        Path(args.body).write_text(body[:60_000], "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

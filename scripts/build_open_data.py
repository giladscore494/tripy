"""Build the open-data snapshots now (the same build as the Data page's "Rebuild now" and the monthly job).

Each build reads the dataset's live header first and stops with a report when a mapped column of
data/open_datasets.json is absent; a stopped or failed build keeps the previous snapshot. Only datasets that
data/source_policy.json lists as `allowed` with bulk_store are downloaded.

    python scripts/build_open_data.py [dataset ...] [--dir <TRIPY_DATA_DIR>/derived/open] [--summary report.md]

--summary writes the table the snapshot-builds PR asks for before merge: rows per dataset and per year / file,
absent_columns, build time and snapshot size (plus each stopped table / file and its reason).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import source_authority  # noqa: E402
from src.open_data import datasets as ds  # noqa: E402
from src.open_data.job import OpenDataJob  # noqa: E402


def summary(results: dict) -> str:
    lines = ["| dataset | status | rows | build time | snapshot size | absent columns |", "|---|---|---|---|---|---|"]
    detail = []
    for name, r in results.items():
        size = f"{r['size_bytes'] / (1024 * 1024):.1f} MB" if r.get("size_bytes") else "—"
        absent = r.get("absent_columns")
        absent_text = json.dumps(absent, ensure_ascii=False) if absent else "—"
        lines.append(f"| {name} | {r.get('status')}{' (' + str(r.get('reason')) + ')' if r.get('reason') else ''} | "
                     f"{r.get('rows', '—')} | {r.get('duration_s', '—')} s | {size} | {absent_text} |")
        for part in r.get("years") or []:
            detail.append(f"- {name} {part.get('year')}: {part.get('status')} status={part.get('status_used')} "
                          f"rows={part.get('rows', '—')} {part.get('reason') or ''} "
                          f"absent={part.get('absent_columns') or []}")
        for part in r.get("files") or []:
            detail.append(f"- {name} {str(part.get('url') or '').rsplit('/', 1)[-1]}: {part.get('status')} "
                          f"group={part.get('group') or '—'} rows={part.get('rows', '—')} {part.get('reason') or ''} "
                          f"absent={part.get('absent_columns') or []}")
        if r.get("status") != "built" and r.get("report"):
            detail.append(f"- {name} report: {json.dumps(r['report'], ensure_ascii=False)[:2000]}")
    return "\n".join(lines + ["", "Per year / file:"] + detail) + "\n"


def main(argv: list[str] | None = None) -> int:
    from src.storage.paths import resolve_paths

    paths = resolve_paths()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="*", help="default: every dataset with a snapshot")
    parser.add_argument("--dir", default=str(paths.data_dir / "derived" / "open"))
    parser.add_argument("--summary", default="", help="write the per-dataset / per-year / per-file table here")
    args = parser.parse_args(argv)
    source_authority.set_policy_overlay_dir(paths.data_dir / "derived")
    job = OpenDataJob(Path(args.dir))
    names = args.datasets or [n for n, c in ds.datasets().items() if not c.get("identity_only")]
    results = {name: job.build(name, reason="cli") for name in names}
    print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    if args.summary:
        Path(args.summary).write_text(summary(results), "utf-8")
    return 0 if all(r.get("status") == "built" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.open_data import datasets as ds  # noqa: E402
from src.open_data.build import build_dataset  # noqa: E402

MANIFEST_VERSION = "open-data-manifest-v1"
ENTRY_KEYS = ("rows", "years", "files", "absent_columns", "column_units", "unit_unknown_distribution",
              "last_file_year", "compaction")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gzip_file(source: Path, target: Path) -> None:
    """Deterministic gzip (no name / mtime in the header): the same snapshot gives the same sha256."""
    tmp = target.with_name(f".{target.name}.tmp")
    with open(source, "rb") as src, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    os.replace(tmp, target)


def summary(results: dict, manifest: dict) -> str:
    lines = ["## Open-data build", "", "| dataset | status | rows | file | size | build time |", "|---|---|---|---|---|---|"]
    for name, r in results.items():
        entry = (manifest.get("datasets") or {}).get(name) or {}
        size = f"{entry['bytes'] / 1024 / 1024:.1f} MB" if r.get("status") == "built" and entry.get("bytes") else "—"
        lines.append(f"| {name} | {r.get('status')}{' (' + str(r.get('reason')) + ')' if r.get('reason') else ''} | "
                     f"{r.get('rows', '—')} | {entry.get('file') if r.get('status') == 'built' else '—'} | {size} | "
                     f"{r.get('duration_s', '—')} s |")
    lines += ["", "Per year / file:"]
    for name, r in results.items():
        for part in r.get("years") or []:
            lines.append(f"- {name} {part.get('year')}: {part.get('status')} status={part.get('status_used')} "
                         f"rows={part.get('rows', '—')} {part.get('reason') or ''} "
                         f"absent={part.get('absent_columns') or []}")
        for part in r.get("files") or []:
            lines.append(f"- {name} {str(part.get('url') or '').rsplit('/', 1)[-1]}: {part.get('status')} "
                         f"group={part.get('group') or '—'} rows={part.get('rows', '—')} {part.get('reason') or ''} "
                         f"absent={part.get('absent_columns') or []}")
        if r.get("compaction"):
            c = r["compaction"]
            lines.append(f"- {name} compaction: {c.get('rows_before')} -> {c.get('rows_after')} rows")
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
    text = summary(results, manifest) + ("".join(f"\n**Failed:** {e}\n" for e in errors))
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

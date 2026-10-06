"""Generate data/open_data_make_aliases.json: open-data make aliases for every catalog manufacturer (H3).

Manufacturers (the government tozar): data/catalog_manufacturers.json (the MILO catalog's DISTINCT tozar), the makes
of data/catalog_trim_index.json, and the tozar / tozeret_nm of the benchmark snapshot and the coverage records (a
tozeret_nm such as "ב מ וו גרמניה" without its country suffix); with --dsn (read-only) the live catalog's DISTINCT tozar,
tozeret_nm as well (--write-catalog refreshes data/catalog_manufacturers.json from it).

Source makes, the only spellings an alias can take: the distinct make columns the build-open-data probe recorded
(data/open/probe.json: EEA `SELECT DISTINCT Mk`, ADEME / EPA / NRCan / CVS make columns) and the make column of the
committed snapshots in data/open/. Matching is deterministic (src/open_data/makes.propose with
data/make_transliteration.json); the reviewed entries of identity_vocabulary `open_data_make_aliases` are kept as is.
Ambiguous and unmatched manufacturers go to the review list (--review, the pull request body), never into the aliases.

    python scripts/build_make_aliases.py [--probe data/open/probe.json] [--open data/open]
                                         [--out data/open_data_make_aliases.json] [--review open-data-makes.md]
                                         [--dsn URL] [--write-catalog]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.open_data import datasets as ds  # noqa: E402
from src.open_data import makes as mk  # noqa: E402

OUT = ROOT / "data" / "open_data_make_aliases.json"


def catalog_manufacturers(dsn: str | None = None) -> tuple[set[str], list[dict]]:
    """(tozar names, live rows) from the repository's catalog files (+ the live catalog with a DSN)."""
    known = set(mk.catalog_manufacturers())
    try:
        index = json.loads((ROOT / "data" / "catalog_trim_index.json").read_text("utf-8"))
        known |= {k.split("|", 1)[0] for k in index.get("entries") or {}}
    except (OSError, ValueError):
        pass
    rows: list[dict] = []
    for name in ("benchmark_v1_level15_snapshot.json", "open_data_coverage_records.json"):
        try:
            rows += json.loads((ROOT / "data" / name).read_text("utf-8")).get("rows") or []
        except (OSError, ValueError):
            continue
    live: list[dict] = []
    if dsn:
        from src.catalog import database_query

        query = database_query(dsn)
        live = [dict(r) for r in query("SELECT DISTINCT tozar, tozeret_nm FROM public.catalog_variants_current", {})]
        rows += live
    for row in rows:
        if row.get("tozar"):
            known.add(str(row["tozar"]).strip())
    for row in rows:
        if row.get("tozeret_nm") and not row.get("tozar"):
            known.add(mk.strip_country(str(row["tozeret_nm"]), known))
    known.discard("")
    return known, live


def observed_makes(probe: dict, open_dir: Path) -> dict[str, set[str]]:
    """{dataset: makes}: the probe's distinct make columns and the committed snapshots' make column."""
    out: dict[str, set[str]] = {}
    for name, item in (probe.get("datasets") or {}).items():
        values = item.get("distinct_makes") if isinstance(item, dict) else None
        if values:
            out.setdefault(name, set()).update(str(v) for v in values)
    ds.set_repo_dir(open_dir)
    try:
        for name, cfg in ds.datasets().items():
            if cfg.get("identity_only"):
                continue
            path, _ = ds.materialize(name)
            if path is None:
                continue
            try:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                    out.setdefault(name, set()).update(m for (m,) in conn.execute("SELECT DISTINCT make FROM rows") if m)
            except sqlite3.Error:
                continue
    finally:
        ds.set_repo_dir(None)
    return out


def review_markdown(result: dict, sources: dict[str, int]) -> str:
    lines = ["## Open-data make aliases", "",
             f"Source makes per dataset: {', '.join(f'{k} {v}' for k, v in sorted(sources.items())) or 'none'}.", "",
             f"Proposed ({len(result['aliases'])}), every alias a source spelling:", ""]
    lines += [f"- {tozar}: {', '.join(makes)}" for tozar, makes in sorted(result["aliases"].items())] or ["- none"]
    lines += ["", f"Reviewed (identity_vocabulary, kept as is): {', '.join(sorted(result['reviewed'])) or 'none'}", "",
              f"**Review: ambiguous ({len(result['ambiguous'])})** — not aliased:", ""]
    lines += [f"- {tozar}: {json.dumps(found, ensure_ascii=False)}" for tozar, found in sorted(result["ambiguous"].items())] \
        or ["- none"]
    lines += ["", f"**Review: unmatched ({len(result['unmatched'])})** — no source spelling matched; add reviewed "
              "entries to identity_vocabulary `open_data_make_aliases` if a source does carry the make:", ""]
    lines += [f"- {tozar}" for tozar in result["unmatched"]] or ["- none"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from src.document_binding import vocabulary

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe", default=str(ROOT / "data" / "open" / "probe.json"))
    parser.add_argument("--open", default=str(ROOT / "data" / "open"))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--review", default="")
    parser.add_argument("--dsn", default=os.environ.get("CATALOG_DSN") or "")
    parser.add_argument("--write-catalog", action="store_true")
    args = parser.parse_args(argv)
    manufacturers, live = catalog_manufacturers(args.dsn or None)
    if args.write_catalog and live:
        path = ROOT / "data" / "catalog_manufacturers.json"
        current = json.loads(path.read_text("utf-8"))
        current.update(captured_at=datetime.now(timezone.utc).date().isoformat(),
                       tozar=sorted({str(r["tozar"]) for r in live if r.get("tozar")}))
        path.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", "utf-8")
    probe = ds._read_json(Path(args.probe)) if args.probe else {}
    observed = observed_makes(probe, Path(args.open))
    result = mk.propose(manufacturers, observed, vocabulary().get("open_data_make_aliases") or {})
    sources = {name: len(values) for name, values in observed.items()}
    out = {"_about": "Generated by scripts/build_make_aliases.py (build-open-data Action): tozar -> the make "
                     "spellings the open datasets contain, matched deterministically (data/make_transliteration.json). "
                     "Merged at read time with identity_vocabulary `open_data_make_aliases` (reviewed, wins). Do not "
                     "edit by hand: add reviewed entries to the vocabulary instead.",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "manufacturers": len(manufacturers), "sources": sources, "aliases": result["aliases"],
           "review": {"ambiguous": result["ambiguous"], "unmatched": result["unmatched"]}}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", "utf-8")
    text = review_markdown(result, sources)
    if args.review:
        Path(args.review).write_text(text, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

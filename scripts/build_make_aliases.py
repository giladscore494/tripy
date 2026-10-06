"""Generate data/open_data_make_aliases.json from data/make_canonical.json and the open datasets' make spellings (E4).

Manufacturers (the government tozar): data/make_canonical.json, the reviewed table tozar -> canonical Latin make(s).
The catalog's tozar (data/catalog_manufacturers.json, or the live catalog with --dsn, read-only) are checked against it:
a tozar the table does not map is listed for review.

Source spellings, the only spellings an alias can take: the distinct make columns the build-open-data probe recorded
(data/open/probe.json: EEA `SELECT DISTINCT Mk`, ADEME / EPA / NRCan / CVS make columns) and the make column of the
committed snapshots in data/open/. A spelling belongs to a canonical make when both normalize to the same key
(src/open_data/makes.normalize_make: upper case, punctuation stripped, legal-form words and address tails dropped,
spaces collapsed); nothing else, never fuzzy. Unmatched spellings stay out.

The report (--review, the pull request body and the step summary): per make the spellings found per dataset, rows kept
vs total per dataset (the probe's rows per spelling), the makes a dataset has no spelling of, and the unmatched
spellings with the most rows.

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
UNMATCHED_LISTED = 25
SPELLINGS_SHOWN = 4


def catalog_manufacturers(dsn: str | None = None) -> tuple[set[str], list[dict]]:
    """(tozar names, live rows): data/catalog_manufacturers.json (+ the live catalog's DISTINCT tozar with a DSN)."""
    known = set(mk.catalog_manufacturers())
    live: list[dict] = []
    if dsn:
        from src.catalog import database_query

        query = database_query(dsn)
        live = [dict(r) for r in query("SELECT DISTINCT tozar, tozeret_nm FROM public.catalog_variants_current", {})]
        known |= {str(r["tozar"]).strip() for r in live if r.get("tozar")}
    known.discard("")
    return known, live


def observed_makes(probe: dict, open_dir: Path) -> tuple[dict[str, set[str]], dict[str, dict[str, int]]]:
    """({dataset: spellings}, {dataset: {spelling: rows}}): the probe's distinct make columns and rows per spelling,
    and the committed snapshots' make column."""
    out: dict[str, set[str]] = {}
    rows: dict[str, dict[str, int]] = {}
    for name, item in (probe.get("datasets") or {}).items():
        if not isinstance(item, dict):
            continue
        values = item.get("distinct_makes")
        if values:
            out.setdefault(name, set()).update(mk.spelling(v) for v in values if mk.spelling(v))
        if isinstance(item.get("make_rows"), dict) and item["make_rows"]:
            rows[name] = {mk.spelling(k): int(v or 0) for k, v in item["make_rows"].items() if mk.spelling(k)}
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
                    out.setdefault(name, set()).update(mk.spelling(m) for (m,) in
                                                       conn.execute("SELECT DISTINCT make FROM rows") if m)
            except sqlite3.Error:
                continue
    finally:
        ds.set_repo_dir(None)
    return out, rows


def generate(manufacturers: set[str], observed: dict[str, set[str]], make_rows: dict[str, dict[str, int]],
             table: dict | None = None) -> dict:
    """The generated file's content (module docstring)."""
    table = mk.canonical() if table is None else table
    per_dataset = {name: mk.spellings_of(values, table) for name, values in sorted(observed.items())}
    spellings: dict[str, dict[str, list[str]]] = {}
    for name, found in per_dataset.items():
        for make, values in found.items():
            spellings.setdefault(make, {})[name] = values
    aliases, gated = {}, {}
    for tozar in sorted(table.get("tozar") or {}):
        plain, gated_makes = mk.tozar_makes(tozar, table)
        found = sorted({s for make in plain + gated_makes for values in (spellings.get(make) or {}).values()
                        for s in values} | {mk.spelling(m) for m in plain + gated_makes})
        aliases[tozar] = found
        if gated_makes:
            gated[tozar] = {"makes": gated_makes,
                            "spellings": sorted({s for make in gated_makes for values in
                                                 (spellings.get(make) or {}).values() for s in values}),
                            "models": mk.gate_families(mk.catalog_families(tozar), plain + gated_makes, table)}
    rows = {}
    for name, counts in sorted(make_rows.items()):
        matched = {s for values in per_dataset.get(name, {}).values() for s in values}
        rows[name] = {"kept": sum(n for s, n in counts.items() if s in matched), "total": sum(counts.values()),
                      "spellings_kept": len(matched & set(counts)), "spellings_total": len(counts)}
    all_makes = mk.canonical_makes(table)
    missing = {name: [m for m in all_makes if m not in found] for name, found in per_dataset.items()}
    unmatched = {}
    for name, values in per_dataset.items():
        matched = {s for found in values.values() for s in found}
        counts = make_rows.get(name) or {}
        rest = sorted((s for s in observed.get(name) or set() if s not in matched), key=lambda s: (-counts.get(s, 0), s))
        unmatched[name] = [[s, counts.get(s)] for s in rest[:UNMATCHED_LISTED]]
    unmapped = [str(t) for t in ((table.get("unmapped") or {}).get("tozar") or [])]
    return {"aliases": aliases, "model_gated": gated, "spellings": dict(sorted(spellings.items())), "rows": rows,
            "makes_per_dataset": {name: len(found) for name, found in per_dataset.items()},
            "review": {"unmapped": sorted(t for t in unmapped if t in manufacturers or not manufacturers),
                       "not_in_canonical": sorted(t for t in manufacturers
                                                  if t not in (table.get("tozar") or {}) and t not in unmapped),
                       "makes_without_spelling": missing, "unmatched_spellings": unmatched}}


def review_markdown(result: dict, sources: dict[str, int]) -> str:
    lines = ["## Open-data make aliases", "",
             f"Source spellings per dataset: {', '.join(f'{k} {v}' for k, v in sorted(sources.items())) or 'none'}; "
             "canonical makes found per dataset: "
             f"{', '.join(f'{k} {v}' for k, v in sorted(result['makes_per_dataset'].items())) or 'none'}.", ""]
    if result["rows"]:
        lines += ["| dataset | rows kept / total (probe) | spellings kept / total |", "|---|---|---|"]
        lines += [f"| {name} | {r['kept']} / {r['total']} | {r['spellings_kept']} / {r['spellings_total']} |"
                  for name, r in result["rows"].items()]
        lines.append("")
    lines += ["Spellings per canonical make (normalization only; data/make_canonical.json; every spelling is in "
              "data/open_data_make_aliases.json `spellings`):", ""]

    def short(values: list[str]) -> str:
        shown = sorted(values, key=lambda v: (len(v), v))[:SPELLINGS_SHOWN]
        return ", ".join(shown) + (f" (+{len(values) - len(shown)} more)" if len(values) > len(shown) else "")
    for make, found in result["spellings"].items():
        lines.append(f"- {make}: " + "; ".join(f"{name}: {short(values)}" for name, values in found.items()))
    lines += ["", "Model-gated makes (a row counts only when its model matches a catalog model family of the tozar):",
              ""]
    lines += [f"- {tozar}: {', '.join(g['makes'])} ({len(g['spellings'])} spellings, {len(g['models'])} catalog "
              "families)" for tozar, g in result["model_gated"].items()] or ["- none"]
    review = result["review"]
    lines += ["", f"**Review: unmapped tozar ({len(review['unmapped'])})** — unknown brand, never guessed: "
              f"{', '.join(review['unmapped']) or 'none'}", "",
              f"**Review: catalog tozar missing from data/make_canonical.json ({len(review['not_in_canonical'])})**: "
              f"{', '.join(review['not_in_canonical']) or 'none'}", "",
              "Makes without a spelling per dataset:", ""]
    lines += [f"- {name}: {', '.join(found) or 'none'}" for name, found in review["makes_without_spelling"].items()]
    lines += ["", f"Unmatched spellings with the most rows (first {UNMATCHED_LISTED}; they stay out):", ""]
    lines += [f"- {name}: " + ", ".join(f"{s}" + (f" ({n})" if n is not None else "") for s, n in values)
              for name, values in review["unmatched_spellings"].items() if values]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
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
    observed, make_rows = observed_makes(probe, Path(args.open))
    result = generate(manufacturers, observed, make_rows)
    sources = {name: len(values) for name, values in observed.items()}
    out = {"_about": "Generated by scripts/build_make_aliases.py (build-open-data Action) from data/make_canonical.json: "
                     "tozar -> the make spellings of the open datasets that normalize to its canonical make(s) "
                     "(src/open_data/makes.normalize_make; never fuzzy). `model_gated`: the makes a row of which counts "
                     "only with a catalog model match. Merged at read time with the reviewed extra spellings of "
                     "identity_vocabulary `open_data_make_aliases`. Do not edit by hand: edit make_canonical.json.",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "canonical_version": mk.canonical().get("version"), "manufacturers": len(manufacturers),
           "sources": sources, **result}
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

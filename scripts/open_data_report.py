"""Open-data shadow report (Part D proof gate). Nothing is admitted and nothing is fetched: the report reads the local
snapshots (the committed data/open/, or a folder of plain <dataset>.sqlite files with --snapshots) and the earlier
runs' results.

Per record (default: the 50 benchmark records of data/benchmark_v1_level15_snapshot.json; 85095, 23678, 38626 and
101136 are among them): the approval route, the match status per source, the designation and match level, every field
offer with its status, and per offer whether it agrees / disagrees with the value an earlier run resolved `ok` for the
same field (the newest result.json of the record in the runs folder that states a value; a field no run resolved is
"no earlier value"). Per make (K1): the government type codes (degem_nm) of the records, how many the make's EEA type-code rule
(data/open_datasets.json `eea_type_code_rules`) matched, per rule, and unmatched examples; with --dsn also every
European-approval type code of the catalog for --type-code-years against the EEA shards of those years.
Summary: coverage by route, median offers per record, the disagreements, and the proposed
data/open_data_admission.json triples: (source, field, route) with at least --min-agree agreements and 0
disagreements. The proposal is printed (and written with --proposal); the allowlist file is never changed here.

    python scripts/open_data_report.py [--records 23678,85095] [--snapshots DIR] [--runs DIR] [--dsn URL]
                                       [--min-agree 5] [--out report.md] [--proposal proposal.json]
                                       [--type-code-years 2020,2021]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import build_level15_payload  # noqa: E402
from src.fields import normalize_field_name  # noqa: E402
from src.gov_registry import run_fingerprint  # noqa: E402
from src.open_data import datasets as ds  # noqa: E402
from src.open_data import makes as mk  # noqa: E402
from src.open_data.match import match, type_code_coverage  # noqa: E402
from src.open_data.offers import field_offers  # noqa: E402
from src.schemas import iter_fields  # noqa: E402

SNAPSHOT = ROOT / "data/benchmark_v1_level15_snapshot.json"
COVERAGE_RECORDS = ROOT / "data/open_data_coverage_records.json"
EEA = "eea_co2_cars"


def earlier_values(runs_dir: Path, record: str) -> dict[str, object]:
    """{field: value} of the newest result.json of the record that states the field (deterministic final assembly
    states a value only for an `ok` field)."""
    out: dict[str, object] = {}
    if not runs_dir.is_dir():
        return out
    for run in sorted((p for p in runs_dir.iterdir() if (p / record / "result.json").is_file()), reverse=True):
        try:
            result = json.loads((run / record / "result.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        for name, entry in iter_fields(result.get("output")):
            value = entry.get("value") if isinstance(entry, dict) else None
            name = normalize_field_name(name)
            if value not in (None, "", []) and name not in out:
                out[name] = value
    return out


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def analyse(records: list[str], rows: dict, runs_dir: Path, dsn: str | None, folder: Path | None) -> list[dict]:
    out = []
    for record in records:
        if record not in rows:
            out.append({"record": record, "error": "not in the benchmark snapshot"})
            continue
        payload = build_level15_payload(rows[record])
        fingerprint = run_fingerprint(payload, dsn)
        result = match(fingerprint, payload, folder=folder)
        offers = field_offers(result)
        earlier = earlier_values(runs_dir, record)
        for offer in offers:
            if offer.get("value") is None:
                offer["comparison"] = "no value"
            elif offer["field"] not in earlier:
                offer["comparison"] = "no earlier value"
            else:
                offer["comparison"] = "agree" if _same(offer["value"], earlier[offer["field"]]) else "disagree"
                offer["earlier"] = earlier[offer["field"]]
        ident = payload.get("identity") or {}
        plain, gated = mk.tozar_makes(str(ident.get("manufacturer") or ""))
        out.append({"record": record, "make": (plain or gated or [None])[0],
                    "type_code": (result.get("keys") or {}).get("type_code"),
                    "type_code_match": ((result.get("sources") or {}).get(EEA) or {}).get("type_code"),
                    "label": " ".join(str(x) for x in (
            (payload.get("identity") or {}).get("manufacturer"), (payload.get("identity") or {}).get("commercial_name"),
            (payload.get("identity") or {}).get("year")) if x), "route": result.get("route_detail"),
            "level": result.get("level"), "designation": result.get("designation"),
            "sources": {s: v.get("status") for s, v in (result.get("sources") or {}).items()},
            "offers": offers, "catalog": fingerprint.get("catalog")})
    return out


def proposal(analysed: list[dict], min_agree: int) -> list[dict]:
    tally: dict[tuple, Counter] = defaultdict(Counter)
    for row in analysed:
        for offer in row.get("offers") or []:
            if offer.get("status") == "offered" and offer.get("comparison") in ("agree", "disagree"):
                tally[(offer["source"], offer["field"], row["route"])][offer["comparison"]] += 1
    return [{"source": s, "field": f, "route": r, "agreements": c["agree"]}
            for (s, f, r), c in sorted(tally.items()) if c["agree"] >= min_agree and not c["disagree"]]


def type_code_section(analysed: list[dict], catalog: dict | None = None) -> list[str]:
    """K1 per make: the records' type codes and their EEA type-code match (rule / no_match / no_rule), and with a
    catalog ({make: coverage} of match.type_code_coverage) every catalog type code of the make."""
    by_make: dict[str, dict] = defaultdict(lambda: {"codes": set(), "rules": Counter(), "unmatched": []})
    for row in analysed:
        if row.get("error") or not row.get("type_code") or not row.get("make"):
            continue
        item = by_make[row["make"]]
        item["codes"].add(row["type_code"])
        found = row.get("type_code_match") or {}
        if found.get("status") == "match":
            item["rules"][found.get("rule")] += 1
        else:
            item["unmatched"].append(f"{row['type_code']} ({found.get('status') or 'no EEA rows'})")
    lines = ["", "## EEA type code per make (K1)", "", "Records:", "",
             "| make | degem_nm | matched per rule | unmatched examples |", "|---|---|---|---|"]
    for make, item in sorted(by_make.items()):
        lines.append(f"| {make} | {len(item['codes'])} | "
                     f"{', '.join(f'{r}: {n}' for r, n in sorted(item['rules'].items())) or '—'} | "
                     f"{', '.join(item['unmatched'][:6]) or '—'} |")
    if catalog:
        lines += ["", "Catalog (European approval, every type code of the years):", "",
                  "| make | degem_nm | rules | matched per rule | matched | unmatched examples |",
                  "|---|---|---|---|---|---|"]
        for make, cov in sorted(catalog.items()):
            share = f" ({100 * cov['matched'] / cov['degem_nm']:.0f} %)" if cov["degem_nm"] else ""
            lines.append(f"| {make} | {cov['degem_nm']} | {', '.join(cov['rules']) or 'none (key unknown)'} | "
                         f"{', '.join(f'{r}: {n}' for r, n in sorted(cov['per_rule'].items())) or '—'} | "
                         f"{cov['matched']}{share} | {', '.join(cov['unmatched_examples']) or '—'} |")
    return lines


def catalog_type_codes(dsn: str, years: list[int]) -> dict[str, list[str]]:
    """{canonical make: [degem_nm]} of the catalog's European-approval variants of the years (read-only)."""
    from src.catalog import database_query
    from src.gov_registry import CATALOG_VIEW

    rows = database_query(dsn)(f"SELECT DISTINCT tozar, degem_nm FROM {CATALOG_VIEW} WHERE shnat_yitzur = ANY(%(y)s) "
                               "AND (sug_tkina_cd::text = '1' OR sug_tkina_nm = 'אירופאית')", {"y": years})
    out: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        plain, _ = mk.tozar_makes(str(row.get("tozar") or ""))
        if plain and row.get("degem_nm"):
            out[plain[0]].append(str(row["degem_nm"]))
    return dict(out)


def catalog_coverage(codes_by_make: dict[str, list[str]], years: list[int], folder: Path | None) -> dict:
    """match.type_code_coverage against the EEA rows of each make (its spellings from the manifest) and years."""
    spellings = ((ds.manifest().get("datasets") or {}).get(EEA) or {}).get("make_spellings") or {}
    rows = {make: ds.query_rows(EEA, makes=spellings.get(make) or [make], years=years, folder=folder)
            for make in codes_by_make}
    return type_code_coverage(codes_by_make, rows)


def report(analysed: list[dict], snapshots: dict, min_agree: int, catalog: dict | None = None) -> str:
    lines = ["# Open-data shadow report", "", "Snapshots:"]
    for name, meta in snapshots.items():
        lines.append(f"- {name}: {meta.get('built_at') or 'not built'}"
                     f"{', ' + str(meta.get('rows')) + ' rows' if meta.get('rows') else ''}")
    lines += ["", "| record | vehicle | route | level | designation | sources | offers (status: n) | agree / disagree |",
              "|---|---|---|---|---|---|---|---|"]
    for row in analysed:
        if row.get("error"):
            lines.append(f"| {row['record']} | {row['error']} | | | | | | |")
            continue
        statuses = Counter(o["status"] for o in row["offers"])
        comp = Counter(o.get("comparison") for o in row["offers"])
        lines.append(f"| {row['record']} | {row['label']} | {row['route']} | {row['level'] or '—'} | "
                     f"{row['designation'] or '—'} | {', '.join(f'{s}:{v}' for s, v in row['sources'].items())} | "
                     f"{', '.join(f'{k}: {v}' for k, v in sorted(statuses.items())) or '—'} | "
                     f"{comp.get('agree', 0)} / {comp.get('disagree', 0)} |")
    ok_rows = [r for r in analysed if not r.get("error")]
    by_route: dict[str, list[dict]] = defaultdict(list)
    for row in ok_rows:
        by_route[row["route"]].append(row)
    lines += ["", "## Summary", "", "| route | records | with a match level | with an `offered` offer |", "|---|---|---|---|"]
    for route, rows in sorted(by_route.items()):
        lines.append(f"| {route} | {len(rows)} | {sum(1 for r in rows if r['level'])} | "
                     f"{sum(1 for r in rows if any(o['status'] == 'offered' for o in r['offers']))} |")
    counts = [len(r["offers"]) for r in ok_rows]
    lines.append(f"\nMedian offers per record: {statistics.median(counts) if counts else 0}")
    disagreements = [(r["record"], o) for r in ok_rows for o in r["offers"] if o.get("comparison") == "disagree"]
    lines.append(f"Disagreements with an earlier ok value: {len(disagreements)}")
    for record, o in disagreements:
        lines.append(f"- {record} {o['field']}: {o['source']} {o.get('value')} vs earlier {o.get('earlier')} "
                     f"({o['status']})")
    lines += type_code_section(analysed, catalog)
    proposed = proposal(analysed, min_agree)
    lines += ["", f"## Proposed data/open_data_admission.json triples (>= {min_agree} agreements, 0 disagreements)", ""]
    lines += [f"- ({p['source']}, {p['field']}, {p['route']}): {p['agreements']} agreements" for p in proposed] or \
        ["None qualify."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from src.storage.paths import resolve_paths

    paths = resolve_paths()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", default="")
    parser.add_argument("--snapshots", default="", help="a folder of <dataset>.sqlite (default: the committed data/open/)")
    parser.add_argument("--runs", default=str(paths.runs_dir))
    parser.add_argument("--dsn", default=None, help="read-only catalog for the code family (default: payload only)")
    parser.add_argument("--min-agree", type=int, default=5)
    parser.add_argument("--out", default="")
    parser.add_argument("--proposal", default="")
    parser.add_argument("--type-code-years", default="2020,2021",
                        help="with --dsn: the catalog years whose type codes are checked against the EEA shards")
    args = parser.parse_args(argv)
    rows = {r["upstream_record_id"]: r for r in json.loads(SNAPSHOT.read_text("utf-8"))["rows"]}
    if COVERAGE_RECORDS.is_file():                 # 22010 (BMW 530e) and the other coverage records
        for row in json.loads(COVERAGE_RECORDS.read_text("utf-8"))["rows"]:
            rows.setdefault(row["upstream_record_id"], row)
    records = [r.strip() for r in args.records.split(",") if r.strip()] or list(rows)
    folder = Path(args.snapshots) if args.snapshots else None
    ds.set_snapshot_dir(folder)
    analysed = analyse(records, rows, Path(args.runs), args.dsn, folder)
    catalog = None
    if args.dsn:
        years = [int(y) for y in args.type_code_years.split(",") if y.strip()]
        catalog = catalog_coverage(catalog_type_codes(args.dsn, years), years, folder)
    text = report(analysed, {name: ds.snapshot_meta(name, folder) for name in ds.datasets()
                             if not ds.datasets()[name].get("identity_only")}, args.min_agree, catalog)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    if args.proposal:
        Path(args.proposal).write_text(json.dumps({"triples": proposal(analysed, args.min_agree)}, indent=1) + "\n",
                                       "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

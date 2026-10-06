"""Open-data shadow report (Part D proof gate). Nothing is admitted and nothing is fetched: the report reads the local
snapshots (the committed data/open/, or a folder of plain <dataset>.sqlite files with --snapshots) and the earlier
runs' results.

Per record (default: the 50 benchmark records of data/benchmark_v1_level15_snapshot.json; 85095, 23678, 38626 and
101136 are among them): the approval route, the match status per source, the designation and match level, every field
offer with its status, and per offer whether it agrees / disagrees with the value an earlier run resolved `ok` for the
same field (the newest result.json of the record in the runs folder that states a value; a field no run resolved is
"no earlier value"). Summary: coverage by route, median offers per record, the disagreements, and the proposed
data/open_data_admission.json triples: (source, field, route) with at least --min-agree agreements and 0
disagreements. The proposal is printed (and written with --proposal); the allowlist file is never changed here.

    python scripts/open_data_report.py [--records 23678,85095] [--snapshots DIR] [--runs DIR] [--dsn URL]
                                       [--min-agree 5] [--out report.md] [--proposal proposal.json]
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
from src.open_data.match import match  # noqa: E402
from src.open_data.offers import field_offers  # noqa: E402
from src.schemas import iter_fields  # noqa: E402

SNAPSHOT = ROOT / "data/benchmark_v1_level15_snapshot.json"


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
        out.append({"record": record, "label": " ".join(str(x) for x in (
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


def report(analysed: list[dict], snapshots: dict, min_agree: int) -> str:
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
    args = parser.parse_args(argv)
    rows = {r["upstream_record_id"]: r for r in json.loads(SNAPSHOT.read_text("utf-8"))["rows"]}
    records = [r.strip() for r in args.records.split(",") if r.strip()] or list(rows)
    folder = Path(args.snapshots) if args.snapshots else None
    ds.set_snapshot_dir(folder)
    analysed = analyse(records, rows, Path(args.runs), args.dsn, folder)
    text = report(analysed, {name: ds.snapshot_meta(name, folder) for name in ds.datasets()
                             if not ds.datasets()[name].get("identity_only")}, args.min_agree)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    if args.proposal:
        Path(args.proposal).write_text(json.dumps({"triples": proposal(analysed, args.min_agree)}, indent=1) + "\n",
                                       "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

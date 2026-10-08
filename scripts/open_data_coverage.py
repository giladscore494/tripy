"""Step 4 of the build-open-data GitHub Action (BEFORE the pull-request step: create-pull-request resets the checkout,
so a later step would read main's files): the coverage of the newly built snapshots in data/open/.

    1. rows per dataset / year / file and the absent columns, from data/open/manifest.json (+ the probe's status)
    2. for the 50 benchmark records (data/benchmark_v1_level15_snapshot.json) plus 22010, 19931 and 59987
       (data/open_data_coverage_records.json; 85095, 23678, 38626 and 101136 are benchmark records): the approval
       route and the match status per source, read from the committed snapshots exactly as the engine reads them
       (sha256 verified, decompressed to the temp dir)
    3. Y5: the 2010-2016 records (19931 BMW 320I 2015: the type-code path; 59987 SKODA OCTAVIA 2015: a make without a
       type-code rule): route, level, the co2 key and every offer

Prints Markdown, appends it to $GITHUB_STEP_SUMMARY when set, and writes the pull-request body (--body). F4: the body
is at most 55,000 characters: its sections are cut from the bottom up (make aliases, snapshots, audit, build summary,
intro), each with a marker; the records section (route and match per record, the 2010-2016 records) is never cut.

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

BODY_CAP = 55_000
GITHUB_BODY_LIMIT = 65_536
FOCUS = ("85095", "23678", "38626", "101136", "22010", "19931", "59987")
NEDC_RECORDS = ("19931", "59987")                    # Y5: 2010-2016 private records (BMW type code; SKODA without one)


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


def nedc_section(rows: list[dict]) -> list[str]:
    """Y5: route, level and offers of the 2010-2016 benchmark records."""
    lines = ["### 2010-2016 records (NEDC years)", ""]
    for row in [r for r in rows if r["upstream_record_id"] in NEDC_RECORDS]:
        payload = build_level15_payload(row)
        result = match(identity_fingerprint(payload), payload)
        ident = payload.get("identity") or {}
        eea = result["sources"].get("eea_co2_cars") or {}
        code = eea.get("type_code") or {}
        lines += [f"**{row['upstream_record_id']}** {ident.get('manufacturer')} {ident.get('commercial_name')} "
                  f"{ident.get('year')} (degem_nm {row.get('degem_nm')}): route {result.get('route_detail')}, level "
                  f"{result.get('level') or '—'} ({result.get('level_basis')}), co2_key {result.get('co2_key')}, EEA "
                  f"{eea.get('status')} ({eea.get('candidates', 0)} candidates; type code "
                  f"{code.get('status') or '—'}{' ' + code['rule'] if code.get('rule') else ''}), designation "
                  f"{result.get('designation') or '—'}", ""]
        offers = field_offers(result)
        if not offers:
            lines += ["- no offer", ""]
            continue
        lines += ["| field | source | status | value | rows | note |", "|---|---|---|---|---|---|"]
        for offer in offers:
            note = offer.get("reason") or (f"values {offer['values']}" if offer.get("values") else "")
            lines.append(f"| {offer['field']} | {offer['source']} | {offer.get('status')} | "
                         f"{offer.get('value', '—')} | {len(offer.get('row_ids') or [])} | {note} |")
        lines.append("")
    return lines


def _marker(name: str, cut: int, cap: int) -> str:
    return (f"\n\n_[{name}: {cut:,} characters cut to keep the pull-request body under {cap:,} characters; the full "
            "text is in the workflow step summary]_\n")


def pr_body(sections: list[tuple[str, str, bool]], cap: int = BODY_CAP) -> str:
    """F4: the pull-request body of (name, text, protected) sections, at most `cap` characters: the unprotected sections
    are cut from the bottom up (at a line end, with a marker naming the section and how much was cut) until the body
    fits; a protected section (the records) is never cut. Only when the protected sections alone exceed GitHub's
    limit is the body cut there, with a marker."""
    texts = [text for _, text, _ in sections]
    for index in range(len(sections) - 1, -1, -1):
        excess = sum(map(len, texts)) - cap
        if excess <= 0:
            break
        name, text, protected = sections[index]
        if protected or not text:
            continue
        marker = _marker(name, 0, cap)
        keep = len(text) - excess - len(marker) - 8                    # 8: room for the cut count's digits
        kept = text[:max(0, text.rfind("\n", 0, keep) if keep > 0 else 0)]
        cut = kept + _marker(name, len(text) - len(kept), cap)
        if len(cut) < len(text):                                       # a marker longer than the text gains nothing
            texts[index] = cut
    body = "".join(texts)
    if len(body) > GITHUB_BODY_LIMIT:
        marker = _marker("body", len(body) - GITHUB_BODY_LIMIT + 300, GITHUB_BODY_LIMIT)
        body = body[:GITHUB_BODY_LIMIT - 300] + marker
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--open", default=str(ROOT / "data" / "open"))
    parser.add_argument("--body", default="")
    parser.add_argument("--build", default="", help="the build step's summary, included in the PR body")
    parser.add_argument("--makes", default="", help="the make-alias review list, included in the PR body")
    parser.add_argument("--audit", default="", help="the EEA schema audit (scripts/audit_eea.py), included in the PR body")
    args = parser.parse_args(argv)
    ds.set_repo_dir(Path(args.open))
    manifest, probe = ds.manifest(), ds.probe_summary()
    snapshots = "\n".join(["## Open-data coverage", ""] + snapshot_section(manifest, probe) + [""]) + "\n"
    table, summary = coverage_section(records())
    per_record = "\n".join([f"Records: {summary['records']}; by route: {summary['by_route']}; with a match level: "
                            f"{summary['with_level']}", ""] + table + [""] + nedc_section(records())) + "\n"
    text = snapshots + per_record
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    if args.body:
        def read(path: str) -> str:
            return Path(path).read_text("utf-8") if path and Path(path).is_file() else ""
        audit, makes = read(args.audit), read(args.makes)
        intro = ("Regenerated by the build-open-data workflow (scripts/probe_open_data.py, scripts/build_open_data.py, "
                 "scripts/audit_eea.py). The snapshots in data/open/ are read by the server from the deploy image; "
                 "nothing is built or written on the data volume.\n\n")
        body = pr_body([("intro", intro, False), ("build summary", read(args.build), False),
                        ("EEA schema audit", "\n" + audit if audit else "", False), ("snapshots", "\n" + snapshots, False),
                        ("records", per_record, True), ("make aliases", "\n" + makes if makes else "", False)])
        Path(args.body).write_text(body, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

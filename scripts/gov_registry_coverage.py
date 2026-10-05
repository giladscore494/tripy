"""Coverage of data/gov_registry_index.json (PR #45, R7): which records the registry layer can serve.

    benchmark   the Level 1.5 records of data/benchmark_v1_level15_snapshot.json (or --level15-json): the record's own
                registry key (src/gov_registry.payload_key: tozeret_cd | degem_cd | year | normalized trim), whether the
                index has an entry for it, and what src/gov_registry.facts would emit from it (tyre sizes, rim)
    catalog     the records of data/catalog_trim_index.json. That index carries no government codes, so its line is an
                UPPER BOUND: records whose (model year, normalized trim) appears under ANY registry key

Read-only, no network. Printed at the end of the build-gov-registry-index Action (after the index is written).

    python scripts/gov_registry_coverage.py [--index data/gov_registry_index.json] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import build_level15_payload  # noqa: E402
from src.gov_registry import entry_for, facts, index as load_index, normalize_key, normalize_trim, payload_key  # noqa: E402

SNAPSHOT_PATH = ROOT / "data" / "benchmark_v1_level15_snapshot.json"
CATALOG_PATH = ROOT / "data" / "catalog_trim_index.json"
TYRE_FIELDS = ("tire_size_front", "tire_size_rear")


def level15_coverage(rows: list[dict], data: dict) -> dict:
    """Per Level 1.5 row: its registry key, entry or not, and the fields the registry layer would emit."""
    out = {"records": 0, "with_key": 0, "with_entry": 0, "with_tyre": 0, "with_rim": 0, "missing": [], "items": []}
    for row in rows:
        out["records"] += 1
        payload = build_level15_payload(row)
        key = payload_key(payload)
        entry = entry_for(key, data) if key else None
        fields = sorted({f["field"] for f in facts(entry)}) if entry else []
        out["with_key"] += key is not None
        out["with_entry"] += entry is not None
        out["with_tyre"] += any(f in TYRE_FIELDS for f in fields)
        out["with_rim"] += "rim_diameter_in" in fields
        item = {"record": str(row.get("upstream_record_id") or row.get("id") or ""),
                "name": " ".join(str(row.get(k) or "") for k in ("tozar", "kinuy_mishari", "shnat_yitzur")).strip(),
                "key": key, "entry": entry is not None, "fields": fields}
        out["items"].append(item)
        if entry is None:
            out["missing"].append(item)
    return out


def catalog_coverage(catalog: dict, data: dict) -> dict:
    """Upper bound for the catalog records: (model year, normalized trim) present under any registry key."""
    present = set()
    for raw in (data.get("entries") or {}) if data.get("complete") is True else {}:
        key = normalize_key(raw)
        if key:
            _, _, year, trim = key.split("|")
            present.add((year, trim))
    out = {"records": 0, "year_trim_in_registry": 0, "note": "upper bound: the catalog index has no government codes"}
    for name, entry in (catalog.get("entries") or {}).items():
        parts = name.split("|")
        records = [str(r) for r in (entry or {}).get("records") or []]
        out["records"] += len(records)
        year = parts[2] if len(parts) > 2 else ""
        if any((year, normalize_trim(t)) in present for t in (entry or {}).get("trims") or [] if t):
            out["year_trim_in_registry"] += len(records)
    return out


def coverage(data: dict, rows: list[dict], catalog: dict | None) -> dict:
    return {"index": {"complete": data.get("complete") is True, "generated_at": data.get("generated_at"),
                      "entries": len(data.get("entries") or {}), "stats": {k: v for k, v in (data.get("stats") or {})
                                                                           .items() if k not in ("manufacturers",
                                                                                                 "unparsed_top")}},
            "benchmark": level15_coverage(rows, data),
            "catalog": catalog_coverage(catalog, data) if catalog is not None else None}


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({round(100 * part / whole) if whole else 0}%)"


def render(report: dict) -> str:
    idx, bench, cat = report["index"], report["benchmark"], report["catalog"]
    lines = [f"registry index: complete={idx['complete']} entries={idx['entries']} generated_at={idx['generated_at']}",
             f"benchmark Level 1.5 records: entry {_pct(bench['with_entry'], bench['records'])}, tyre evidence "
             f"{_pct(bench['with_tyre'], bench['records'])}, rim evidence {_pct(bench['with_rim'], bench['records'])}"]
    lines += [f"  no entry: {m['record']} {m['name']} key={m['key']}" for m in bench["missing"]]
    if cat is not None:
        share = _pct(cat["year_trim_in_registry"], cat["records"])
        lines.append(f"catalog records: (year, trim) under some registry key {share} [{cat['note']}]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--index", default=None, help="the registry index (default data/gov_registry_index.json)")
    ap.add_argument("--level15-json", default=str(SNAPSHOT_PATH), help="Level 1.5 rows ({rows: [...]} or a list)")
    ap.add_argument("--catalog", default=str(CATALOG_PATH))
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    data = load_index(args.index)
    raw = json.loads(Path(args.level15_json).read_text("utf-8"))
    rows = raw.get("rows", []) if isinstance(raw, dict) else list(raw)
    catalog = json.loads(Path(args.catalog).read_text("utf-8")) if args.catalog and Path(args.catalog).exists() else None
    report = coverage(data, rows, catalog)
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

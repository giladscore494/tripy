"""Coverage of data/gov_registry_index.json (PR #45, R7): which records the registry layer can serve.

    benchmark   the Level 1.5 records of data/benchmark_v1_level15_snapshot.json (or --level15-json): the record's own
                registry key (src/gov_registry.payload_key: tozeret_cd | degem_cd | year | normalized trim), whether the
                index has an entry for it, and what src/gov_registry.facts would emit from it (tyre sizes, rim)
    catalog     the records of data/catalog_trim_index.json. That index carries no government codes, so its line is an
                UPPER BOUND: records whose (model year, normalized trim) appears under ANY registry key

Every benchmark record without a level A entry gets a reason, from the index's `model_years`:

    no_government_codes   the record has no tozeret_cd / degem_cd / year: no key can be formed
    no_key                no registered vehicle under the key at all (its model year's trims are listed)
    small                 n < 20 vehicles under the key (the n is printed)
    unparsed              n < 20 parsed, but >= 20 with the tyre cells the parser rejected
    level_b               the model-year pool (level B) emits for it, with the fields the record's own trim confirms
                          (exact_market_trim, they fill); else the level B refusal is printed
    index_without_model_years   an index built before the model-year pools: the reason cannot be told

Read-only, no network. Printed at the end of the build-gov-registry-index Action, also when an earlier step failed.

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
from src.gov_registry import (LEVEL_B, MIN_VEHICLES, lookup, model_year_for, normalize_key,  # noqa: E402
                              normalize_trim, payload_key, target_trim_check)
from src.gov_registry import index as load_index  # noqa: E402

SNAPSHOT_PATH = ROOT / "data" / "benchmark_v1_level15_snapshot.json"
CATALOG_PATH = ROOT / "data" / "catalog_trim_index.json"
TYRE_FIELDS = ("tire_size_front", "tire_size_rear")


def missing_reason(key: str | None, found: dict, data: dict, trim: str | None = None) -> dict:
    """Why a record has no level A entry: {reason, n?, unparsed?, trims?, level_b?} (see the module doc). level_b
    names the fields the record's own trim confirms (`market_trim`: bound at exact_market_trim, they fill)."""
    if key is None:
        return {"reason": "no_government_codes"}
    if not isinstance(data.get("model_years"), dict):
        return {"reason": "index_without_model_years"}
    pool = model_year_for(key, data) or {}
    trims = pool.get("trims") or {}
    info = trims.get(normalize_key(key).rsplit("|", 1)[1])
    out: dict = {}
    if info is None:
        out = {"reason": "no_key", "trims": sorted(trims)}
    else:
        parsed = [int((info.get(axle) or [0, 0, 0])[2]) for axle in ("front", "rear")]
        unparsed = list(info.get("unparsed") or [0, 0])
        n, with_unparsed = max(parsed), max(p + int(u) for p, u in zip(parsed, unparsed))
        out = {"reason": "unparsed" if with_unparsed >= MIN_VEHICLES else "small", "n": n, "rows": info.get("rows"),
               "unparsed": unparsed}
    if found["level"] == "B" and any(r["field"] != "alternative_tire_sizes" for r in found["rows"]):
        out = {**out, "under": out["reason"], "reason": "level_b"}
    fields = sorted({r["field"] for r in found["rows"]})
    words = [w for w in str(trim or "").lower().split() if w]
    out["level_b"] = {"used": found["level"] == LEVEL_B, "refusal": found["reason"], "fields": fields,
                      "market_trim": [f for f in fields if found["level"] == LEVEL_B
                                      and target_trim_check(found["entry"], words, f)["confirmed"]]}
    return out


def level15_coverage(rows: list[dict], data: dict) -> dict:
    """Per Level 1.5 row: its registry key, entry or not, the fields the registry layer would emit and the level they
    come from (A: the trim's own entry, B: the model-year pool), and for a row without a level A entry the reason."""
    out = {"records": 0, "with_key": 0, "with_entry": 0, "with_level_b": 0, "with_tyre": 0, "with_rim": 0,
           "reasons": {}, "missing": [], "items": []}
    for row in rows:
        out["records"] += 1
        payload = build_level15_payload(row)
        key = payload_key(payload)
        found = lookup(key, data) if key else {"level": None, "rows": [], "reason": "no_key"}
        fields = sorted({f["field"] for f in found["rows"]})
        entry = found["level"] == "A"
        out["with_key"] += key is not None
        out["with_entry"] += entry
        out["with_tyre"] += any(f in TYRE_FIELDS for f in fields)
        out["with_rim"] += "rim_diameter_in" in fields
        item = {"record": str(row.get("upstream_record_id") or row.get("id") or ""),
                "name": " ".join(str(row.get(k) or "") for k in ("tozar", "kinuy_mishari", "shnat_yitzur")).strip(),
                "key": key, "entry": entry, "level": found["level"], "fields": fields}
        if not entry:
            item.update(missing_reason(key, found, data, (payload.get("identity") or {}).get("trim")))
            out["reasons"][item["reason"]] = out["reasons"].get(item["reason"], 0) + 1
            out["with_level_b"] += item["reason"] == "level_b"
            out["missing"].append(item)
        out["items"].append(item)
    return out


def _reason_text(m: dict) -> str:
    reason = m["reason"]
    if reason == "level_b":
        detail = f"level B used: {', '.join(m['level_b']['fields'])}"
        under = m.get("under")
        detail += f" (trim n={m.get('n')})" if under == "small" else f" (trim: {under})" if under else ""
        confirmed = m["level_b"].get("market_trim") or []
        return detail + (f"; confirmed by the trim (exact_market_trim): {', '.join(confirmed)}" if confirmed
                         else "; not confirmed by the trim (exact_technical_variant, fields stay unfilled)")
    detail = {"no_government_codes": "no government codes on the record",
              "index_without_model_years": "index built before the model-year pools (no model_years): rebuild for "
                                           "the reason",
              "no_key": f"no key at all (model year trims: {', '.join(m.get('trims') or []) or 'none'})",
              "small": f"n<20 (n={m.get('n')})",
              "unparsed": f"unparsed (n={m.get('n')} parsed, unparsed cells front/rear={m.get('unparsed')})",
              }.get(reason, reason)
    if m.get("level_b"):
        lb = m["level_b"]
        refusal = (lb.get("refusal") or "").removeprefix("level_b_") or "none"
        detail += f"; level B refused: {refusal}"
        if lb.get("fields"):
            detail += f" (evidence: {', '.join(lb['fields'])})"
    return detail


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
             f"benchmark Level 1.5 records: entry {_pct(bench['with_entry'], bench['records'])}, level B "
             f"{_pct(bench['with_level_b'], bench['records'])}, tyre evidence "
             f"{_pct(bench['with_tyre'], bench['records'])}, rim evidence {_pct(bench['with_rim'], bench['records'])}"]
    if bench["reasons"]:
        lines.append("  no entry, by reason: " + ", ".join(f"{k} {v}" for k, v in sorted(bench["reasons"].items())))
    lines += [f"  no entry: {m['record']} {m['name']} key={m['key']}: {_reason_text(m)}" for m in bench["missing"]]
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

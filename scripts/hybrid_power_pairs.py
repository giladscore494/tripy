"""Report only (PR #47): hybrid / plug-in catalog groups with more than one government power.

The government catalog records some hybrids twice: with the engine's power (Corolla 2024 wagon 1.8: 98 hp) and with the
system power (the same model code, 140 hp). Binding treats every power as its own technical variant (fail-closed).
This script lists every (manufacturer, family, year, body, propulsion in hybrid / plug_in, drivetrain, displacement)
group with two or more distinct powers, with record counts and model codes, as input for a LATER, human-verified
engine -> system power table. It changes no binding and writes nothing the engine reads.

Sources (`--source auto`): the live catalog over DATABASE_URL (public.catalog_variants_current, read-only), else the
repository's data/catalog_trim_index.json (records and powers per entry; no model codes there).

    python scripts/hybrid_power_pairs.py                         # CSV to stdout
    python scripts/hybrid_power_pairs.py --out data/derived/hybrid_power_pairs.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.document_binding import parse_catalog_key, power_bucket, target_identity, trim_index  # noqa: E402

HYBRIDS = ("hybrid", "plug_in")
COLUMNS = ("manufacturer", "family", "year", "body", "propulsion", "drivetrain", "displacement_l", "powers",
           "records_per_power", "model_codes_per_power", "records_total")
SQL = ("SELECT upstream_record_id, tozar, kinuy_mishari, shnat_yitzur, ramat_gimur, norm_body_style, "
       "norm_propulsion_technology, norm_drivetrain, koah_sus, nefah_manoa, degem_nm "
       "FROM public.catalog_variants_current WHERE norm_propulsion_technology IN ('hybrid', 'plug_in')")


def groups_from_rows(rows: list[dict]) -> list[dict]:
    """The report rows from catalog rows (keys built with the same target_identity() a run uses)."""
    out: dict[tuple, dict] = defaultdict(lambda: defaultdict(lambda: {"records": set(), "codes": set()}))
    for row in rows:
        ident = target_identity({"identity": {"manufacturer": row.get("tozar"), "commercial_name": row.get("kinuy_mishari"),
                                              "year": row.get("shnat_yitzur"), "trim": row.get("ramat_gimur")},
                                 "engine_drivetrain": {"engine_cc": row.get("nefah_manoa"), "power_hp": row.get("koah_sus"),
                                                       "propulsion_normalized": row.get("norm_propulsion_technology"),
                                                       "drivetrain_normalized": row.get("norm_drivetrain")},
                                 "structure": {"body_normalized": row.get("norm_body_style")}})
        if ident.propulsion not in HYBRIDS or ident.power_hp is None or not ident.family:
            continue
        key = (ident.manufacturer, ident.family, ident.year, ident.body or "", ident.propulsion,
               ident.drivetrain or "", "" if ident.displacement_l is None else f"{ident.displacement_l:.1f}")
        cell = out[key][power_bucket(ident.power_hp)]
        cell["records"].add(str(row.get("upstream_record_id")))
        if row.get("degem_nm"):
            cell["codes"].add(" ".join(str(row["degem_nm"]).split()))
    return _rows(out)


def groups_from_index(index: dict) -> list[dict]:
    """The report rows from the catalog trim index (no model codes)."""
    out: dict[tuple, dict] = defaultdict(lambda: defaultdict(lambda: {"records": set(), "codes": set()}))
    for name, entry in (index.get("entries") or {}).items():
        parts = parse_catalog_key(name)
        if not parts or parts["propulsion"] not in HYBRIDS or parts["power"] is None:
            continue
        key = tuple(parts[k] for k in ("manufacturer", "family", "year", "body", "propulsion", "drivetrain",
                                       "displacement_l"))
        out[key][parts["power"]]["records"] |= {str(r) for r in entry.get("records") or []}
    return _rows(out)


def _rows(groups: dict) -> list[dict]:
    rows = []
    for key, powers in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        if len(powers) < 2:
            continue
        ordered = sorted(powers)
        rows.append({**dict(zip(COLUMNS[:7], key)), "powers": "|".join(str(p) for p in ordered),
                     "records_per_power": "|".join(str(len(powers[p]["records"])) for p in ordered),
                     "model_codes_per_power": "|".join(";".join(sorted(powers[p]["codes"])) for p in ordered),
                     "records_total": sum(len(powers[p]["records"]) for p in ordered)})
    return rows


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=["auto", "database", "index"], default="auto")
    ap.add_argument("--out", help="write the CSV here (e.g. <TRIPY_DATA_DIR>/derived/hybrid_power_pairs.csv)")
    args = ap.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or ""
    source = args.source if args.source != "auto" else ("database" if dsn else "index")
    if source == "database":
        if not dsn:
            raise SystemExit("DATABASE_URL is not set")
        from src.catalog import database_query

        rows = groups_from_rows(database_query(dsn)(SQL, {}))
    else:
        rows = groups_from_index(trim_index())
    text = to_csv(rows)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, "utf-8")
        print(f"{args.out}: {len(rows)} hybrid / plug-in groups with two or more powers ({source})", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

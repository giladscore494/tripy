"""Build data/catalog_trim_index.json: the government catalog's distinct trims per technical variant.

    key    manufacturer | model family | model year | body | propulsion | drivetrain | power (hp, nearest 5) |
           displacement (l)            (src/document_binding.catalog_key, from the SAME target_identity() a run uses)
    value  {"trims": [distinct government trims], "records": [government record ids]}

src/document_binding.single_catalog_trim reads it for the `single_trim_catalog` binding rule: a fact may bind at
exact_market_trim without naming the trim only when its technical variant has exactly ONE trim in that model year.
Everything is fail-closed: an entry marked `"complete": false` never applies, and an entry shares the catalog rows
whose body / propulsion / drivetrain / power / displacement the mapper left unnormalized (those rows could be
siblings of any variant of the same manufacturer, family and year), so such entries are marked incomplete too.

Sources, in this order (`--source auto`):

    database   DATABASE_URL (or SUPABASE_DB_URL): public.catalog_variants_current (src/db.py), read-only
    ckan       the Israeli government vehicle catalog on data.gov.il (CKAN datastore_search, paged), the dataset the
               snapshot mapper gov.wltp.variant-mapper.1 reads; raw Hebrew values are normalized with RAW_NORMALIZATION
               (taken from the mapper's own output in catalog_variants_current; an unknown raw value stays unnormalized
               and marks its siblings incomplete). Needs --resource-id / GOV_CATALOG_RESOURCE_ID.
    rows-json  a JSON export of catalog_variants_current rows ({"rows": [...]}: objects, or arrays in ROW_COLUMNS order)
    benchmark  only the vehicles of data/benchmark_v1_level15_snapshot.json, every entry `"complete": false`
               (keeps the file valid where no catalog source is reachable; the rule then never applies)

    python scripts/build_trim_index.py                       # auto: database, else ckan (never benchmark)
    python scripts/build_trim_index.py --source rows-json --rows-json export.json --source-label "..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.document_binding import (catalog_key, normalize_catalog_trim, power_bucket,  # noqa: E402
                                  target_identity)

INDEX_VERSION = "catalog-trim-index-v1"
OUT_PATH = ROOT / "data" / "catalog_trim_index.json"
SNAPSHOT_PATH = ROOT / "data" / "benchmark_v1_level15_snapshot.json"
ROW_COLUMNS = ("upstream_record_id", "tozar", "kinuy_mishari", "shnat_yitzur", "ramat_gimur", "norm_body_style",
               "norm_propulsion_technology", "norm_drivetrain", "koah_sus", "nefah_manoa")
SQL = f"SELECT {', '.join(ROW_COLUMNS)} FROM public.catalog_variants_current"
KEY_PARTS = ("manufacturer", "family", "year", "body", "propulsion", "drivetrain", "power", "displacement_l")
CKAN_URL = "https://data.gov.il/api/3/action/datastore_search"
CKAN_PAGE = 5000
# raw government value -> the mapper's normalized value, as gov.wltp.variant-mapper.1 maps them in
# catalog_variants_current (DISTINCT raw / normalized pairs). Values the mapper leaves unnormalized are absent.
RAW_NORMALIZATION = {
    "merkav": {"MPV": "mpv", "הצ'בק": "hatchback", "סדאן": "sedan", "סטיישן": "wagon", "פנאי-שטח": "suv",
               "קבריולט": "convertible", "קופה": "coupe", "תא בודד": "pickup_single_cab", "תא כפול": "pickup_double_cab"},
    "hanaa_nm": {"4X2": "two_wheel_drive", "4X4": "awd"},
    "technologiat_hanaa_nm": {"PLUG IN": "plug_in", "היברידי רגיל": "hybrid", "הנעה רגילה": "conventional",
                              "רכב חשמלי": "battery_electric"},
}


def _payload(row: dict) -> dict:
    """The parts of src/db.build_level15_payload that target_identity() reads."""
    return {"identity": {"manufacturer": row.get("tozar"), "commercial_name": row.get("kinuy_mishari"),
                         "year": row.get("shnat_yitzur"), "trim": row.get("ramat_gimur")},
            "engine_drivetrain": {"engine_cc": row.get("nefah_manoa"), "power_hp": row.get("koah_sus"),
                                  "propulsion_normalized": row.get("norm_propulsion_technology"),
                                  "drivetrain_normalized": row.get("norm_drivetrain")},
            "structure": {"body_normalized": row.get("norm_body_style")}}


def _parts(row: dict) -> dict:
    ident = target_identity(_payload(row))
    return {"manufacturer": ident.manufacturer, "family": ident.family, "year": ident.year, "body": ident.body,
            "propulsion": ident.propulsion, "drivetrain": ident.drivetrain, "power": power_bucket(ident.power_hp),
            "displacement_l": ident.displacement_l}


def _unknown(parts: dict) -> set[str]:
    """Key parts the catalog row leaves unknown (it could then be a sibling of any value of that part)."""
    unknown = {k for k in ("body", "propulsion", "drivetrain", "power") if parts[k] in (None, "")}
    if parts["displacement_l"] is None and parts["propulsion"] != "battery_electric":
        unknown.add("displacement_l")
    return unknown


def build_index(rows: list[dict], *, source: str, complete: bool = True) -> dict:
    entries: dict[str, dict] = {}
    uncertain: list[dict] = []
    for row in rows:
        parts = _parts(row)
        if parts["manufacturer"] in (None, "") or parts["family"] in (None, "") or parts["year"] is None:
            continue
        if _unknown(parts):
            uncertain.append(parts)
        entry = entries.setdefault(catalog_key(**parts), {"trims": set(), "records": set()})
        entry["trims"].add(normalize_catalog_trim(row.get("ramat_gimur")))
        entry["records"].add(str(row.get("upstream_record_id")))
    # a row with an unknown part may be a sibling of every entry that agrees on its known parts
    by_model: dict[tuple, list[tuple[str, dict]]] = {}
    for key in entries:
        named = dict(zip(KEY_PARTS, key.split("|")))
        by_model.setdefault((named["manufacturer"], named["family"], named["year"]), []).append((key, named))
    for parts in uncertain:
        unknown = _unknown(parts)
        known = {k: v for k, v in zip(KEY_PARTS, catalog_key(**parts).split("|")) if k not in unknown}
        for key, named in by_model.get((known["manufacturer"], known["family"], known["year"]), []):
            if all(named[k] == v for k, v in known.items()):
                entries[key]["complete"] = False
    out = {}
    for key in sorted(entries):
        entry = entries[key]
        item = {"trims": sorted(entry["trims"]), "records": sorted(entry["records"], key=lambda r: (len(r), r))}
        if entry.get("complete") is False or not complete:
            item["complete"] = False
        out[key] = item
    return {
        "_about": "Government catalog trims per technical variant (scripts/build_trim_index.py; read by "
                  "src/document_binding.single_catalog_trim). An entry with \"complete\": false never applies.",
        "version": INDEX_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "complete": complete,
        "key_fields": ["manufacturer", "family", "year", "body", "propulsion", "drivetrain", "power_hp_nearest_5",
                       "displacement_l"],
        "rows": len(rows),
        "entries": out,
    }


def dumps(index: dict) -> str:
    """Compact JSON with one entry per line (reviewable diffs when the index is regenerated)."""
    head = json.dumps({k: v for k, v in index.items() if k != "entries"}, ensure_ascii=False)
    lines = [json.dumps(k, ensure_ascii=False) + ":" + json.dumps(v, ensure_ascii=False, separators=(",", ":"))
             for k, v in index["entries"].items()]
    return head[:-1] + ', "entries": {\n' + ",\n".join(lines) + "\n}}\n"


def rows_from_database(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(SQL)
            return [dict(r) for r in cur.fetchall()]


def _normalize_raw(record: dict) -> dict:
    row = {k: record.get(k) for k in ("tozar", "kinuy_mishari", "shnat_yitzur", "ramat_gimur", "koah_sus",
                                      "nefah_manoa")}
    row["upstream_record_id"] = record.get("_id")
    row["norm_body_style"] = RAW_NORMALIZATION["merkav"].get(str(record.get("merkav") or "").strip())
    row["norm_drivetrain"] = RAW_NORMALIZATION["hanaa_nm"].get(str(record.get("hanaa_nm") or "").strip())
    row["norm_propulsion_technology"] = RAW_NORMALIZATION["technologiat_hanaa_nm"].get(
        str(record.get("technologiat_hanaa_nm") or "").strip())
    for key in ("tozar", "kinuy_mishari", "ramat_gimur"):
        row[key] = str(row[key]).strip() if row[key] not in (None, "") else None
    for key in ("shnat_yitzur", "koah_sus", "nefah_manoa"):
        try:
            row[key] = int(float(row[key])) if row[key] not in (None, "") else None
        except (TypeError, ValueError):
            row[key] = None
    return row


def rows_from_ckan(resource_id: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        query = urllib.parse.urlencode({"resource_id": resource_id, "limit": CKAN_PAGE, "offset": offset})
        with urllib.request.urlopen(f"{CKAN_URL}?{query}", timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))["result"]
        records = result.get("records") or []
        if offset == 0:
            fields = {f.get("id") for f in result.get("fields") or []}
            missing = {"tozar", "kinuy_mishari", "shnat_yitzur", "ramat_gimur", "koah_sus", "merkav", "hanaa_nm",
                       "technologiat_hanaa_nm"} - fields
            if missing:
                raise SystemExit(f"resource {resource_id} lacks catalog columns: {sorted(missing)}")
        rows += [_normalize_raw(r) for r in records]
        offset += len(records)
        if not records or offset >= int(result.get("total") or 0):
            return rows


def rows_from_json(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text("utf-8"))
    raw = data.get("rows") if isinstance(data, dict) else data
    return [r if isinstance(r, dict) else dict(zip(ROW_COLUMNS, r)) for r in raw]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=["auto", "database", "ckan", "rows-json", "benchmark"], default="auto")
    ap.add_argument("--rows-json", help="rows-json source: the exported rows")
    ap.add_argument("--source-label", help="rows-json source: where the export came from (written to `source`)")
    ap.add_argument("--resource-id", default=os.environ.get("GOV_CATALOG_RESOURCE_ID"),
                    help="ckan source: the data.gov.il datastore resource id")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or ""
    source = args.source
    if source == "auto":           # never silently the incomplete benchmark-only index
        source = "database" if dsn else "ckan"
    complete = True
    if source == "database":
        if not dsn:
            raise SystemExit("DATABASE_URL is not set")
        rows, label = rows_from_database(dsn), "database: public.catalog_variants_current"
    elif source == "ckan":
        if not args.resource_id:
            raise SystemExit("--resource-id (or GOV_CATALOG_RESOURCE_ID) is required for the ckan source")
        rows, label = rows_from_ckan(args.resource_id), f"ckan: data.gov.il datastore {args.resource_id}"
    elif source == "rows-json":
        if not args.rows_json:
            raise SystemExit("--rows-json is required for the rows-json source")
        rows = rows_from_json(args.rows_json)
        label = args.source_label or f"rows-json: {Path(args.rows_json).name}"
    else:
        rows = json.loads(SNAPSHOT_PATH.read_text("utf-8"))["rows"]
        label, complete = f"benchmark snapshot only: {SNAPSHOT_PATH.name} (incomplete; the rule never applies)", False
    index = build_index(rows, source=label, complete=complete)
    Path(args.out).write_text(dumps(index), "utf-8")
    entries = index["entries"].values()
    print(f"{args.out}: {len(rows)} rows, {len(index['entries'])} keys, "
          f"{sum(1 for e in entries if len(e['trims']) == 1 and e.get('complete', complete))} single-trim complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

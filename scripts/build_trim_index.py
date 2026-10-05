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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.catalog_trim_index import (INDEX_VERSION, KEY_PARTS, ROW_COLUMNS, SQL, build_index,  # noqa: E402,F401
                                    dumps, rows_from_database)

OUT_PATH = ROOT / "data" / "catalog_trim_index.json"
SNAPSHOT_PATH = ROOT / "data" / "benchmark_v1_level15_snapshot.json"
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

"""The government catalog trim index (data/catalog_trim_index.json): built from catalog rows (PR #47 B2: shared by
scripts/build_trim_index.py and the server's weekly derived-index job, src/jobs/derived_index.py).

    key    manufacturer | model family | model year | body | propulsion | drivetrain | power (hp, nearest 5) |
           displacement (l)            (src/document_binding.catalog_key, from the SAME target_identity() a run uses)
    value  {"trims": [distinct government trims], "records": [government record ids]}

An entry shares the catalog rows whose body / propulsion / drivetrain / power / displacement the mapper left
unnormalized (those rows could be siblings of any variant of the same manufacturer, family and year): such entries are
marked `"complete": false` and never apply.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .document_binding import catalog_key, normalize_catalog_trim, power_bucket, target_identity

INDEX_VERSION = "catalog-trim-index-v1"
ROW_COLUMNS = ("upstream_record_id", "tozar", "kinuy_mishari", "shnat_yitzur", "ramat_gimur", "norm_body_style",
               "norm_propulsion_technology", "norm_drivetrain", "koah_sus", "nefah_manoa")
SQL = f"SELECT {', '.join(ROW_COLUMNS)} FROM public.catalog_variants_current"
KEY_PARTS = ("manufacturer", "family", "year", "body", "propulsion", "drivetrain", "power", "displacement_l")


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

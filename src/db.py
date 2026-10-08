"""Level 1.5 loader.

Authority: the spec's Appendix D query against Supabase Postgres
(`public.catalog_variants_current` + `public.catalog_variant_equipment`). That
view is granted to a read-only database role, not to the REST roles, so the
loader connects with a Postgres URL (DATABASE_URL) in a read-only transaction.

When no DATABASE_URL is configured the frozen snapshot in
data/benchmark_v1_level15_snapshot.json (captured with the same query) is used,
and every run records which source it came from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_PATH = DATA_DIR / "benchmark_v1_level15_snapshot.json"

LEVEL15_SQL = """
SELECT
  v.*,
  public.catalog_variant_equipment(
    v.equipment_stated,
    v.equipment_on,
    v.equipment_sources
  ) AS equipment
FROM public.catalog_variants_current AS v
WHERE v.upstream_record_id = ANY(%(ids)s::text[])
ORDER BY array_position(%(ids)s::text[], v.upstream_record_id)
"""

# The vehicle facts API (src/facts): the Level 1.5 rows of variant identity keys (catalog_variants_identity_idx), the
# private segment only. One short read-only query per call.
LEVEL15_BY_KEY_SQL = """
SELECT
  v.*,
  public.catalog_variant_equipment(
    v.equipment_stated,
    v.equipment_on,
    v.equipment_sources
  ) AS equipment
FROM public.catalog_variants_current AS v
WHERE v.variant_identity_key = ANY(%(keys)s::text[])
  AND v.vehicle_segment = 'private'
"""

# Order matches public.catalog_variant_equipment_keys(): 19 indicator bits, then
# the 5 installation-source fields.
EQUIPMENT_BITS: list[tuple[str, str, str]] = [
    ("bakarat_mehirut_isa", "בקרת מהירות נסיעה", "speed control / intelligent speed assist"),
    ("bakarat_shyut_adaptivit_ind", "בקרת שיוט אדפטיבית", "adaptive cruise control"),
    ("bakarat_stiya_activ_s", "בקרת סטייה אקטיבית", "active lane keeping"),
    ("bakarat_stiya_menativ_ind", "בקרת סטייה מנתיב", "lane departure warning"),
    ("blima_otomatit_nesia_leahor", "בלימה אוטומטית בנסיעה לאחור", "automatic reverse braking"),
    ("blimat_hirum_lifnei_holhei_regel_ofanaim", "בלימת חירום לפני הולכי רגל/אופניים",
     "emergency braking for pedestrians/cyclists"),
    ("hayshaney_hagorot_ind", "חיישני חגורות", "seatbelt sensors"),
    ("hayshaney_lahatz_avir_batzmigim_ind", "חיישני לחץ אוויר בצמיגים", "tire pressure sensors"),
    ("hitnagshut_cad_shetah_met", "התנגשות צד/שטח מת", "side collision / blind spot"),
    ("maarechet_ezer_labalam_ind", "מערכת עזר לבלם", "brake assist"),
    ("matzlemat_reverse_ind", "מצלמת רוורס", "reversing camera"),
    ("nitur_merhak_milfanim_ind", "ניטור מרחק מלפנים", "forward distance monitoring"),
    ("shlita_automatit_beorot_gvohim_ind", "שליטה אוטומטית באורות גבוהים", "automatic high beam"),
    ("teura_automatit_benesiya_kadima_ind", "תאורה אוטומטית בנסיעה קדימה", "automatic headlights"),
    ("zihuy_beshetah_nistar_ind", "זיהוי בשטח נסתר", "hidden-area detection"),
    ("zihuy_holchey_regel_ind", "זיהוי הולכי רגל", "pedestrian detection"),
    ("zihuy_matzav_hitkarvut_mesukenet_ind", "זיהוי מצב התקרבות מסוכנת", "dangerous approach detection"),
    ("zihuy_rechev_do_galgali", "זיהוי רכב דו-גלגלי", "two-wheeler detection"),
    ("zihuy_tamrurey_tnua_ind", "זיהוי תמרורי תנועה", "traffic sign recognition"),
]
EQUIPMENT_SOURCE_KEYS = [
    "bakarat_stiya_menativ_makor_hatkana",
    "nitur_merhak_milfanim_makor_hatkana",
    "shlita_automatit_beorot_gvohim_makor_hatkana",
    "zihuy_holchey_regel_makor_hatkana",
    "zihuy_tamrurey_tnua_makor_hatkana",
]


class Level15Error(RuntimeError):
    pass


@dataclass
class LoadResult:
    rows: list[dict]
    source: str                       # "database" | "snapshot"
    missing: list[str] = field(default_factory=list)
    note: str = ""


def decode_equipment(stated: int | None, on: int | None, sources: list | None) -> dict:
    """Python port of public.catalog_variant_equipment (used for display and tests)."""
    stated, on, sources = stated or 0, on or 0, sources or []
    out: dict[str, Any] = {}
    for i, (key, _, _) in enumerate(EQUIPMENT_BITS):
        if stated & (1 << i):
            out[key] = 1 if on & (1 << i) else 0
    for i, key in enumerate(EQUIPMENT_SOURCE_KEYS):
        if i < len(sources) and sources[i] is not None:
            out[key] = sources[i]
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or ""


def load_from_database(ids: list[str], dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(LEVEL15_SQL, {"ids": list(ids)})
            return [_jsonable(dict(row)) for row in cur.fetchall()]


def load_level15_by_keys(keys: list[str], query) -> dict[str, dict]:
    """{variant_identity_key: Level 1.5 row} through a read-only query function (src/catalog.database_query); unknown
    keys are simply absent. Never the snapshot: the 50 benchmark records are not the catalogue."""
    keys = [str(k) for k in keys]
    rows = query(LEVEL15_BY_KEY_SQL, {"keys": keys}) if keys else []
    return {str(r["variant_identity_key"]): _jsonable(dict(r)) for r in rows if r.get("variant_identity_key")}


def load_snapshot(ids: list[str], path: Path = SNAPSHOT_PATH) -> list[dict]:
    data = json.loads(Path(path).read_text("utf-8"))
    by_id = {str(row["upstream_record_id"]): row for row in data["rows"]}
    return [by_id[i] for i in ids if i in by_id]


def load_level15(ids: list[str], source: str = "auto", dsn: str | None = None,
                 snapshot_path: Path = SNAPSHOT_PATH) -> LoadResult:
    """Load Level 1.5 rows in the given order.

    source="auto" uses the database when a DSN is configured and the snapshot
    otherwise. A configured database that fails is an error, never a silent
    switch to the snapshot.
    """
    ids = [str(i) for i in ids]
    dsn = dsn if dsn is not None else database_url()
    if source == "database" or (source == "auto" and dsn):
        if not dsn:
            raise Level15Error("DATABASE_URL is not set")
        try:
            rows = load_from_database(ids, dsn)
        except Exception as exc:
            raise Level15Error(f"Level 1.5 query failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        used, note = "database", "Appendix D query on public.catalog_variants_current"
    elif source in ("snapshot", "auto"):
        rows = load_snapshot(ids, snapshot_path)
        used, note = "snapshot", f"Frozen snapshot {Path(snapshot_path).name} (same query, captured 2026-10-01)"
    else:
        raise Level15Error(f"Unknown source {source!r}")
    found = {str(r["upstream_record_id"]) for r in rows}
    return LoadResult(rows=rows, source=used, missing=[i for i in ids if i not in found], note=note)


def build_level15_payload(row: dict) -> dict:
    """Arrange a Level 1.5 row for the model. `raw_row` keeps every column untouched."""
    equipment = row.get("equipment")
    if equipment is None:
        equipment = decode_equipment(row.get("equipment_stated"), row.get("equipment_on"),
                                     row.get("equipment_sources"))
    systems = []
    for key, he, en in EQUIPMENT_BITS:
        if key in equipment:
            systems.append({"key": key, "name_he": he, "gloss_en": en, "installed": equipment[key]})
    sources = {key: equipment[key] for key in EQUIPMENT_SOURCE_KEYS if key in equipment}
    g = row.get
    return {
        "identity": {
            "manufacturer": g("tozar"), "manufacturer_entity": g("tozeret_nm"), "commercial_name": g("kinuy_mishari"),
            "year": g("shnat_yitzur"), "trim": g("ramat_gimur"), "model_code": g("degem_nm"),
            "segment": g("vehicle_segment"), "government_record_id": g("upstream_record_id"),
            "government_codes": {"tozeret_cd": g("tozeret_cd"), "degem_cd": g("degem_cd"), "sug_degem": g("sug_degem")},
        },
        "engine_drivetrain": {
            "engine_cc": g("nefah_manoa"), "power_hp": g("koah_sus"), "fuel": g("delek_nm"),
            "fuel_normalized": g("norm_fuel_type"), "propulsion_technology": g("technologiat_hanaa_nm"),
            "propulsion_normalized": g("norm_propulsion_technology"), "drive": g("hanaa_nm"),
            "drivetrain_normalized": g("norm_drivetrain"), "automatic": g("automatic_ind"),
        },
        "structure": {
            "body": g("merkav"), "body_normalized": g("norm_body_style"), "doors": g("mispar_dlatot"),
            "seats": g("mispar_moshavim"), "gross_weight_kg": g("mishkal_kolel"),
            "towing_braked_kg": g("kosher_grira_im_blamim"), "towing_unbraked_kg": g("kosher_grira_bli_blamim"),
            "country_of_manufacture": g("tozeret_eretz_nm"), "standard": g("sug_tkina_nm"),
            "converter_type": g("sug_mamir_nm"),
        },
        "environment": {
            "co2_city": g("kamut_co2_city"), "co2_highway": g("kamut_co2_hway"), "co2_wltp": g("co2_wltp"),
            "nox_wltp": g("nox_wltp"), "co_wltp": g("co_wltp"), "hc_wltp": g("hc_wltp"),
            "pollution_group": g("kvutzat_zihum"), "green_index": g("madad_yarok"),
        },
        "safety": {
            "safety_score": g("nikud_betihut"), "safety_equipment_level": g("ramat_eivzur_betihuty"),
            "airbags": g("mispar_kariot_avir"), "abs": g("abs_ind"), "esc": g("bakarat_yatzivut_ind"),
            "assist_systems": systems, "installation_sources": sources,
        },
        "raw_row": row,
    }

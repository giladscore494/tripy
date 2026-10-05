"""The live MILO catalog browser (PR #47, B1): read-only, always current.

Every query reads `public.catalog_variants_current` through DATABASE_URL in a read-only transaction (the same view and
connection src/db.py loads Level 1.5 rows from); nothing here writes, and nothing creates anything on the MILO side.

Filters use the view's indexed columns only (EXPLAIN on production, 2026-10-05):

    manufacturer + model [+ year]       catalog_variants_tree_idx (snapshot_id, mapper_version, kinuy_mishari,
                                        shnat_yitzur); the manufacturer through the view's per-manufacturer build
    manufacturer + year (+ degem_cd /   catalog_variants_reading_idx (mapper_version, ..., shnat_yitzur): bitmap scan
    trim text)                          per model year. degem_cd and trim search exist ONLY with manufacturer + year
    upstream record id                  catalog_variants_record_uidx (snapshot_id, upstream_record_id, mapper_version)

The manufacturer list and a manufacturer's model list have no index on the MILO side (catalog_variants has none on
tozar: a sequential scan of ~99k rows); they are cached in memory for CACHE_TTL_S like the year lists, and the missing
index is reported to MILO (PR #47), never created from here.

Without DATABASE_URL the browser is unavailable (CatalogUnavailable): the UI shows the snapshot (the 50 benchmark
records) and disables the browser; runs keep recording `level15_source`.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

VIEW = "public.catalog_variants_current"
COLUMNS = ("upstream_record_id", "tozar", "kinuy_mishari", "shnat_yitzur", "ramat_gimur", "degem_nm", "degem_cd",
           "norm_body_style", "norm_propulsion_technology", "norm_drivetrain", "koah_sus", "nefah_manoa", "hanaa_nm",
           "vehicle_segment", "tozeret_eretz_nm")
MAX_LIMIT = 200
MAX_SET = 50
CACHE_TTL_S = 600.0
SNAPSHOT_LABEL = "snapshot (50 benchmark records)"


class CatalogUnavailable(RuntimeError):
    """No DATABASE_URL: the live catalog is not available (the UI shows the snapshot)."""


class CatalogQueryRefused(ValueError):
    """A filter combination no MILO index serves (e.g. a degem_cd search without manufacturer + year)."""


Query = Callable[[str, dict], list[dict]]


def database_query(dsn: str) -> Query:
    """A read-only query function over DATABASE_URL (one short connection per call, read-only transaction)."""
    def run(sql: str, params: dict) -> list[dict]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
    return run


def vehicle_of(row: dict) -> dict:
    """A catalog row as a research target (the shape of data/benchmark_v1_ids.json vehicles)."""
    return {"upstream_record_id": str(row.get("upstream_record_id")), "manufacturer": row.get("tozar"),
            "model": row.get("kinuy_mishari"), "year": row.get("shnat_yitzur"), "trim": row.get("ramat_gimur"),
            "model_code": row.get("degem_nm"), "segment": row.get("vehicle_segment"),
            "country": row.get("tozeret_eretz_nm"), "propulsion": row.get("norm_propulsion_technology"),
            "drivetrain": row.get("hanaa_nm"), "source": "catalog"}


def item_of(row: dict) -> dict:
    """A catalog row as the browser lists it."""
    rid = str(row.get("upstream_record_id"))
    label = " · ".join(str(x) for x in (f"{row.get('tozar') or ''} {row.get('kinuy_mishari') or ''}".strip(),
                                       row.get("shnat_yitzur"), row.get("ramat_gimur")) if x)
    return {"record_id": rid, "manufacturer": row.get("tozar"), "model": row.get("kinuy_mishari"),
            "year": row.get("shnat_yitzur"), "trim": row.get("ramat_gimur"), "model_code": row.get("degem_nm"),
            "degem_cd": row.get("degem_cd"), "body": row.get("norm_body_style"),
            "propulsion": row.get("norm_propulsion_technology"), "drivetrain": row.get("norm_drivetrain"),
            "power_hp": row.get("koah_sus"), "engine_cc": row.get("nefah_manoa"), "label": f"{label} ({rid})"}


class CatalogBrowser:
    """Read-only catalog queries with a CACHE_TTL_S in-memory cache of the manufacturer / model / year / trim lists."""

    def __init__(self, query: Query | None, *, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._query, self.ttl_s, self.clock = query, float(ttl_s), clock
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._query is not None

    def _run(self, sql: str, params: dict) -> list[dict]:
        if self._query is None:
            raise CatalogUnavailable("DATABASE_URL is not set: the catalog browser shows the snapshot only")
        return self._query(sql, params)

    def _cached(self, key: tuple, compute: Callable[[], Any]) -> Any:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl_s:
                return hit[1]
        value = compute()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    # -- cascading lists (cached) -------------------------------------------------------------------------------------
    def manufacturers(self) -> list[dict]:
        return self._cached(("manufacturers",), lambda: [
            {"manufacturer": r["tozar"], "variants": int(r["n"])} for r in self._run(
                f"SELECT tozar, count(*) AS n FROM {VIEW} WHERE tozar IS NOT NULL GROUP BY tozar ORDER BY tozar", {})])

    def models(self, manufacturer: str) -> list[dict]:
        _need(manufacturer=manufacturer)
        return self._cached(("models", manufacturer), lambda: [
            {"model": r["kinuy_mishari"], "variants": int(r["n"])} for r in self._run(
                f"SELECT kinuy_mishari, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                "AND kinuy_mishari IS NOT NULL GROUP BY kinuy_mishari ORDER BY kinuy_mishari",
                {"manufacturer": manufacturer})])

    def years(self, manufacturer: str, model: str) -> list[dict]:
        _need(manufacturer=manufacturer, model=model)
        return self._cached(("years", manufacturer, model), lambda: [
            {"year": r["shnat_yitzur"], "variants": int(r["n"])} for r in self._run(
                f"SELECT shnat_yitzur, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                "AND kinuy_mishari = %(model)s GROUP BY shnat_yitzur ORDER BY shnat_yitzur DESC",
                {"manufacturer": manufacturer, "model": model})])

    def trims(self, manufacturer: str, model: str, year: int) -> list[dict]:
        _need(manufacturer=manufacturer, model=model, year=year)
        return self._cached(("trims", manufacturer, model, int(year)), lambda: [
            {"trim": r["ramat_gimur"], "variants": int(r["n"])} for r in self._run(
                f"SELECT ramat_gimur, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                "AND kinuy_mishari = %(model)s AND shnat_yitzur = %(year)s GROUP BY ramat_gimur ORDER BY ramat_gimur",
                {"manufacturer": manufacturer, "model": model, "year": int(year)})])

    # -- the filtered list -------------------------------------------------------------------------------------------
    def search(self, *, manufacturer: str | None = None, model: str | None = None, year: int | None = None,
               trim: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """{items, total, limit, offset, filters}: server-side filtering and paging (limit <= MAX_LIMIT). Accepted
        filter sets (indexed): q = an upstream record id (alone or with others); manufacturer + model [+ year
        [+ trim]]; manufacturer + year [+ trim / a degem_cd q]. Anything else is refused (CatalogQueryRefused)."""
        limit = max(1, min(int(limit or 50), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        where, params = catalog_filters(manufacturer=manufacturer, model=model, year=year, trim=trim, q=q)
        clause = " AND ".join(where)
        rows = self._run(f"SELECT {', '.join(COLUMNS)} FROM {VIEW} WHERE {clause} "
                         "ORDER BY tozar, kinuy_mishari, shnat_yitzur DESC, ramat_gimur, upstream_record_id "
                         "LIMIT %(limit)s OFFSET %(offset)s", {**params, "limit": limit, "offset": offset})
        total = self._run(f"SELECT count(*) AS n FROM {VIEW} WHERE {clause}", params)
        return {"items": [item_of(r) for r in rows], "total": int((total or [{"n": 0}])[0]["n"]), "limit": limit,
                "offset": offset, "filters": {k: v for k, v in (("manufacturer", manufacturer), ("model", model),
                                                                ("year", year), ("trim", trim), ("q", q)) if v}}

    def vehicles(self, record_ids: list[str]) -> list[dict]:
        """Research targets of record ids (the upstream record id index), in the given order; unknown ids dropped."""
        ids = [str(i) for i in record_ids][:MAX_SET]
        if not ids:
            return []
        rows = self._run(f"SELECT {', '.join(COLUMNS)} FROM {VIEW} WHERE upstream_record_id = ANY(%(ids)s::text[])",
                         {"ids": ids})
        by_id = {str(r["upstream_record_id"]): r for r in rows}
        return [{**vehicle_of(by_id[i]), "ordinal": n} for n, i in enumerate(ids, start=1) if i in by_id]


def _need(**values: Any) -> None:
    missing = [k for k, v in values.items() if v in (None, "")]
    if missing:
        raise CatalogQueryRefused(f"{', '.join(missing)} required")


def catalog_filters(*, manufacturer: str | None, model: str | None, year: int | None, trim: str | None,
                    q: str | None) -> tuple[list[str], dict]:
    """(WHERE clauses, params) of a catalog search; CatalogQueryRefused for a combination no index serves."""
    where, params = [], {}
    q = (q or "").strip()
    record_id = q if q.isdigit() else None
    if manufacturer:
        where.append("tozar = %(manufacturer)s")
        params["manufacturer"] = manufacturer
    if model:
        where.append("kinuy_mishari = %(model)s")
        params["model"] = model
    if year not in (None, ""):
        where.append("shnat_yitzur = %(year)s")
        params["year"] = int(year)
    indexed_year = bool(manufacturer) and year not in (None, "")
    if trim:
        if not (indexed_year or (manufacturer and model)):
            raise CatalogQueryRefused("a trim filter needs manufacturer + year (or manufacturer + model)")
        where.append("ramat_gimur = %(trim)s")
        params["trim"] = trim
    if q and record_id is None:
        if not indexed_year:
            raise CatalogQueryRefused("a text search needs manufacturer + year")
        where.append("ramat_gimur ILIKE %(trim_text)s")
        params["trim_text"] = f"%{q}%"
    elif record_id is not None:
        if indexed_year:
            where.append("(upstream_record_id = %(record_id)s OR degem_cd = %(degem_cd)s)")
            params.update(record_id=record_id, degem_cd=int(record_id))
        else:
            where.append("upstream_record_id = %(record_id)s")
            params["record_id"] = record_id
    if not record_id and not ((manufacturer and model) or indexed_year):
        raise CatalogQueryRefused("choose a manufacturer and a model (or a model year), or search a record id")
    return where, params

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

import atexit
import os
import re
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
PICKER_TTL_S = 6 * 3600.0                  # the facts API picker's lists (a `segment` is given): warmed at boot
PICKER_WARMUP_MODELS = 15                  # the model lists of the largest manufacturers (by private variants)
SNAPSHOT_LABEL = "snapshot (50 benchmark records)"


class CatalogUnavailable(RuntimeError):
    """No DATABASE_URL: the live catalog is not available (the UI shows the snapshot)."""


class CatalogQueryRefused(ValueError):
    """A filter combination no MILO index serves (e.g. a degem_cd search without manufacturer + year)."""


Query = Callable[[str, dict], list[dict]]


# The process-wide connection pool (one per DSN): MIN_POOL..MAX_POOL connections reused across requests, each checked on
# checkout (a dead connection is replaced, never handed out). Measured 2026-10-09: the government read of a memory-cache
# hit took 1.3-1.4 s, almost all of it the new TLS connection per call (the query itself: ~2 ms planning + ~3 ms
# execution on the MILO side, EXPLAIN ANALYZE).
MIN_POOL, MAX_POOL = 1, 5
POOL_TIMEOUT_S = 15.0
_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()
_TIMINGS = threading.local()


def _configure(conn) -> None:
    conn.read_only = True


def _new_pool(dsn: str):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # prepare_threshold=None: no server-side prepared statements, so the Supabase transaction pooler (port 6543) is safe
    # behind a long-lived connection. open=True does not wait: the first connection is made in the background.
    return ConnectionPool(dsn, min_size=MIN_POOL, max_size=MAX_POOL, timeout=POOL_TIMEOUT_S,
                          kwargs={"row_factory": dict_row, "connect_timeout": 15, "prepare_threshold": None},
                          configure=_configure, check=ConnectionPool.check_connection, open=True, name="milo")


def connection_pool(dsn: str):
    """The process-wide pool of this DSN (created on first use; the API, the MCP and the run fingerprint share it)."""
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            if not _POOLS:
                atexit.register(close_pools)
            pool = _POOLS[dsn] = _new_pool(dsn)
        return pool


def close_pools() -> None:
    """Close every pool (at exit: without it the pool's worker threads hold the interpreter's exit for seconds)."""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close(timeout=1.0)
        except Exception:  # noqa: BLE001 - closing never fails the exit
            pass


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def take_timings() -> dict:
    """The phases of this thread's last database_query call (ms), then forgotten: acquire (pool checkout, health check
    included), execute (BEGIN READ ONLY + the statement on the MILO side, round trips included), fetch (reading the
    rows), release (COMMIT + the return to the pool). {} when no pooled query ran since the last take."""
    value = getattr(_TIMINGS, "value", None) or {}
    _TIMINGS.value = None
    return dict(value)


def placement(dsn: str, env: Callable[[str], str | None] = os.environ.get) -> dict:
    """Where the service and the database run, without a secret: the Railway replica region and the database host's
    cloud region when the host names one (the Supabase pooler host aws-<n>-<region>.pooler.supabase.com; a direct
    db.<ref>.supabase.co host names none), the port (6543: the transaction pooler, 5432: session / direct)."""
    from urllib.parse import urlsplit

    host, port = "", None
    try:
        if "://" in dsn:
            parts = urlsplit(dsn)
            host, port = parts.hostname or "", parts.port
        else:                                                      # a libpq key=value string
            fields = dict(f.split("=", 1) for f in dsn.split() if "=" in f)
            host, port = fields.get("host", ""), int(fields["port"]) if fields.get("port") else None
    except ValueError:
        pass
    region = re.search(r"\b(?:aws|gcp|azure)-\d+-([a-z]{2}-[a-z]+-\d)\b", host or "")
    return {"service_region": env("RAILWAY_REPLICA_REGION") or None,
            "db_region": region.group(1) if region else None,
            "db_host_kind": "pooler" if "pooler." in host else ("direct" if host else None), "db_port": port}


def database_query(dsn: str) -> Query:
    """A read-only query function over DATABASE_URL: a pooled connection (connection_pool) per call, a read-only
    transaction, committed when the connection goes back to the pool. The pool (and psycopg) is set up here, in the
    caller's thread: at boot that is the main thread, before the background threads (the derived index, the picker
    warm-up) start; concurrent first imports of psycopg from two threads can deadlock on the import lock."""
    pool = connection_pool(dsn)

    def run(sql: str, params: dict) -> list[dict]:
        t0 = time.perf_counter()
        with pool.connection() as conn:
            t1 = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                t2 = time.perf_counter()
                rows = [dict(r) for r in cur.fetchall()]
                t3 = time.perf_counter()
        t4 = time.perf_counter()
        _TIMINGS.value = {"acquire_ms": _ms(t1 - t0), "execute_ms": _ms(t2 - t1), "fetch_ms": _ms(t3 - t2),
                          "release_ms": _ms(t4 - t3)}
        return rows
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
    """Read-only catalog queries with a CACHE_TTL_S in-memory cache of the manufacturer / model / year / trim lists;
    the facts API picker's lists (asked with a `segment`) are kept for picker_ttl_s (PICKER_TTL_S) and warmed at boot
    (start_picker_warmup)."""

    def __init__(self, query: Query | None, *, ttl_s: float = CACHE_TTL_S, picker_ttl_s: float = PICKER_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._query, self.ttl_s, self.picker_ttl_s, self.clock = query, float(ttl_s), float(picker_ttl_s), clock
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._warmup: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._query is not None

    def _run(self, sql: str, params: dict) -> list[dict]:
        if self._query is None:
            raise CatalogUnavailable("DATABASE_URL is not set: the catalog browser shows the snapshot only")
        return self._query(sql, params)

    def _cached(self, key: tuple, compute: Callable[[], Any], segment: str | None = None) -> Any:
        now = self.clock()
        ttl = self.picker_ttl_s if segment else self.ttl_s
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = compute()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    # -- cascading lists (cached) -------------------------------------------------------------------------------------
    # `segment` (the facts API: "private") narrows every list to one vehicle_segment; the cache keys carry it.
    def manufacturers(self, *, segment: str | None = None) -> list[dict]:
        seg, params = _segment(segment)
        return self._cached(("manufacturers", segment), lambda: [
            {"manufacturer": r["tozar"], "variants": int(r["n"])} for r in self._run(
                f"SELECT tozar, count(*) AS n FROM {VIEW} WHERE tozar IS NOT NULL{seg} GROUP BY tozar ORDER BY tozar",
                params)], segment)

    def models(self, manufacturer: str, *, segment: str | None = None) -> list[dict]:
        _need(manufacturer=manufacturer)
        seg, params = _segment(segment)
        return self._cached(("models", manufacturer, segment), lambda: [
            {"model": r["kinuy_mishari"], "variants": int(r["n"])} for r in self._run(
                f"SELECT kinuy_mishari, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                f"AND kinuy_mishari IS NOT NULL{seg} GROUP BY kinuy_mishari ORDER BY kinuy_mishari",
                {"manufacturer": manufacturer, **params})], segment)

    def years(self, manufacturer: str, model: str, *, segment: str | None = None) -> list[dict]:
        _need(manufacturer=manufacturer, model=model)
        seg, params = _segment(segment)
        return self._cached(("years", manufacturer, model, segment), lambda: [
            {"year": r["shnat_yitzur"], "variants": int(r["n"])} for r in self._run(
                f"SELECT shnat_yitzur, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                f"AND kinuy_mishari = %(model)s{seg} GROUP BY shnat_yitzur ORDER BY shnat_yitzur DESC",
                {"manufacturer": manufacturer, "model": model, **params})], segment)

    def trims(self, manufacturer: str, model: str, year: int) -> list[dict]:
        _need(manufacturer=manufacturer, model=model, year=year)
        return self._cached(("trims", manufacturer, model, int(year)), lambda: [
            {"trim": r["ramat_gimur"], "variants": int(r["n"])} for r in self._run(
                f"SELECT ramat_gimur, count(*) AS n FROM {VIEW} WHERE tozar = %(manufacturer)s "
                "AND kinuy_mishari = %(model)s AND shnat_yitzur = %(year)s GROUP BY ramat_gimur ORDER BY ramat_gimur",
                {"manufacturer": manufacturer, "model": model, "year": int(year)})])

    def variants(self, manufacturer: str, model: str, year: int, *, segment: str | None = None) -> list[dict]:
        """One item per variant of a manufacturer / model / year (the facts API's trim picker): its
        variant_identity_key and a display label. Variants without a key are left out (nothing to ask for)."""
        _need(manufacturer=manufacturer, model=model, year=year)
        seg, params = _segment(segment)
        return self._cached(("variants", manufacturer, model, int(year), segment), lambda: [
            variant_item(r) for r in self._run(
                f"SELECT variant_identity_key, {', '.join(COLUMNS)}, automatic_ind FROM {VIEW} "
                f"WHERE tozar = %(manufacturer)s AND kinuy_mishari = %(model)s AND shnat_yitzur = %(year)s{seg} "
                "AND variant_identity_key IS NOT NULL ORDER BY ramat_gimur, koah_sus, nefah_manoa, degem_nm, "
                "upstream_record_id", {"manufacturer": manufacturer, "model": model, "year": int(year), **params})],
            segment)

    # -- the facts API picker warm-up ---------------------------------------------------------------------------------
    def warm_picker(self, segment: str, models_of: int = PICKER_WARMUP_MODELS) -> dict:
        """Load the picker's manufacturer list and the model lists of the `models_of` largest manufacturers (by
        variants) into the cache. Returns what it did (counts and latency, never a value)."""
        started = time.perf_counter()
        makers = self.manufacturers(segment=segment)
        largest = sorted(makers, key=lambda m: (-int(m["variants"]), str(m["manufacturer"])))[:max(0, int(models_of))]
        for maker in largest:
            self.models(maker["manufacturer"], segment=segment)
        return {"segment": segment, "manufacturers": len(makers), "models_of": len(largest),
                "latency_ms": _ms(time.perf_counter() - started)}

    def start_picker_warmup(self, segment: str, log: Any = None) -> threading.Thread | None:
        """warm_picker in a background thread (once per browser; requests never wait for it). A failure only logs.
        No database: nothing to warm (None)."""
        if self._query is None:
            return None
        with self._lock:
            if self._warmup is not None:
                return self._warmup
            self._warmup = threading.Thread(target=self._warm, args=(segment, log), name="picker-warmup", daemon=True)
        self._warmup.start()
        return self._warmup

    def _warm(self, segment: str, log: Any) -> None:
        import json
        import logging

        logger = log or logging.getLogger("tripy.facts")
        try:
            done = self.warm_picker(segment)
            logger.info("facts picker warm-up %s", json.dumps({**done, "status": "ok"}, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - the warm-up never costs the process: the first request pays instead
            logger.warning("facts picker warm-up failed: %s", type(exc).__name__)

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


def _segment(segment: str | None) -> tuple[str, dict]:
    return (" AND vehicle_segment = %(segment)s", {"segment": segment}) if segment else ("", {})


def variant_item(row: dict) -> dict:
    """A catalog row as the facts API's trim picker lists it: the key, a display label and what tells variants of
    one trim apart."""
    parts = [row.get("ramat_gimur") or "", f"{row['koah_sus']} hp" if row.get("koah_sus") else "",
             f"{row['nefah_manoa']} cc" if row.get("nefah_manoa") else "", row.get("norm_propulsion_technology") or "",
             row.get("degem_nm") or ""]
    item = {"variant_identity_key": row.get("variant_identity_key"), "label": " · ".join(p for p in parts if p),
            "trim": row.get("ramat_gimur"), "degem_nm": row.get("degem_nm"), "horsepower": row.get("koah_sus"),
            "engine_cc": row.get("nefah_manoa"), "propulsion": row.get("norm_propulsion_technology"),
            "drivetrain": row.get("norm_drivetrain"), "body_style": row.get("norm_body_style"),
            "upstream_record_id": str(row.get("upstream_record_id"))}
    return {k: v for k, v in item.items() if v not in (None, "")}


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

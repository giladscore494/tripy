"""The vehicle facts API for yeda-rechev (src/facts), `Authorization: Bearer <TRIPY_FACTS_TOKEN | TRIPY_ACCESS_TOKEN>`:

    GET  /api/facts/v1/catalog/manufacturers                       the picker: the same CatalogBrowser and caching as
    GET  /api/facts/v1/catalog/models?manufacturer=                /api/catalog, the private segment only; a trim item
    GET  /api/facts/v1/catalog/years?manufacturer=&model=          is one variant with its variant_identity_key and a
    GET  /api/facts/v1/catalog/trims?manufacturer=&model=&year=    display label
    POST /api/facts/v1/vehicles  {"variant_identity_keys": [1-3]}  one `vehicle-facts/1` record per key, in order (an
                                                                   unknown key: status not_found, the others still
                                                                   return); ?debug=1 (operator token) adds `withheld`
    GET  /api/facts/v1/contract                                    the JSON schema, the admission / zero-semantics
                                                                   versions, the snapshot manifest sha and built_at,
                                                                   the gov manifest / recall model map shas

The MILO DB unreachable (or no DATABASE_URL): 503 catalog_unavailable, never the 50-record benchmark snapshot. A shard
the free-space guard refuses to decompress: 503 snapshots_unavailable. Responses are canonical JSON (sorted keys):
the same data and versions give byte-identical bodies. Deterministic: no model, no web, no run, no vPIC, no write.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...catalog import CatalogQueryRefused, CatalogUnavailable
from ...facts import CONTRACT
from ...facts import versions as V
from ...facts.service import MAX_KEYS, SnapshotsUnavailable, dumps, log
from ..auth import require_facts_access
from ..deps import ApiContext, get_context
from ..errors import ApiError

router = APIRouter(prefix="/api/facts/v1", tags=["facts"])
SEGMENT = "private"


class CanonicalJSON(Response):
    media_type = "application/json"

    def render(self, content) -> bytes:
        return dumps(content).encode("utf-8")


class VehiclesRequest(BaseModel):
    variant_identity_keys: list[str] = Field(min_length=1, max_length=MAX_KEYS)


def _picker(name: str, fn, *args, **kwargs):
    started = time.perf_counter()
    status = "ok"
    try:
        return fn(*args, **kwargs)
    except CatalogUnavailable as exc:
        status = "catalog_unavailable"
        raise ApiError(503, "catalog_unavailable", str(exc)) from None
    except CatalogQueryRefused as exc:
        status = "refused"
        raise ApiError(422, "catalog_filter_refused", str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - an unreachable MILO is a 503, never a crash or a fallback
        status = "catalog_unavailable"
        raise ApiError(503, "catalog_unavailable", f"The MILO catalogue query failed: {type(exc).__name__}") from None
    finally:
        log.info("facts picker %s", dumps({"endpoint": name, "status": status,
                                           "latency_ms": round((time.perf_counter() - started) * 1000, 1)}))


@router.get("/catalog/manufacturers", response_class=CanonicalJSON)
def facts_manufacturers(ctx: ApiContext = Depends(get_context)) -> CanonicalJSON:
    return CanonicalJSON({"manufacturers": _picker("manufacturers", ctx.catalog_browser.manufacturers,
                                                   segment=SEGMENT)})


@router.get("/catalog/models", response_class=CanonicalJSON)
def facts_models(manufacturer: str, ctx: ApiContext = Depends(get_context)) -> CanonicalJSON:
    return CanonicalJSON({"models": _picker("models", ctx.catalog_browser.models, manufacturer, segment=SEGMENT)})


@router.get("/catalog/years", response_class=CanonicalJSON)
def facts_years(manufacturer: str, model: str, ctx: ApiContext = Depends(get_context)) -> CanonicalJSON:
    return CanonicalJSON({"years": _picker("years", ctx.catalog_browser.years, manufacturer, model, segment=SEGMENT)})


@router.get("/catalog/trims", response_class=CanonicalJSON)
def facts_trims(manufacturer: str, model: str, year: int, ctx: ApiContext = Depends(get_context)) -> CanonicalJSON:
    return CanonicalJSON({"trims": _picker("trims", ctx.catalog_browser.variants, manufacturer, model, year,
                                           segment=SEGMENT)})


@router.post("/vehicles", response_class=CanonicalJSON)
def facts_vehicles(body: VehiclesRequest, debug: int = Query(0, ge=0, le=1),
                   role: str = Depends(require_facts_access), ctx: ApiContext = Depends(get_context)) -> CanonicalJSON:
    if debug and role != "operator":
        raise ApiError(403, "debug_requires_operator", "debug=1 needs the operator token.")
    try:
        records, _ = ctx.facts.records(body.variant_identity_keys, debug=bool(debug))
    except CatalogUnavailable as exc:
        raise ApiError(503, "catalog_unavailable", str(exc)) from None
    except SnapshotsUnavailable as exc:
        raise ApiError(503, "snapshots_unavailable", str(exc)) from None
    return CanonicalJSON({"contract": CONTRACT, "vehicles": records})


@router.get("/contract", response_class=CanonicalJSON)
def facts_contract() -> CanonicalJSON:
    adm, zero, snap = V.admission(), V.zero_semantics(), V.snapshots()
    return CanonicalJSON({"contract": CONTRACT, "schema": V.contract_schema(),
                          "admission": {"version": adm.get("version"), "consumer": adm.get("consumer")},
                          "zero_semantics": {"version": zero.get("version")},
                          "snapshots": {"manifest_sha256": snap["sha256"], "built_at": snap.get("built_at")},
                          "matcher": V.matcher_version(), "gov_datasets": V.gov()})

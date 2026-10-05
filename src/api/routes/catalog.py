"""The live MILO catalog (PR #47, B1 / B2), read-only (src/catalog.py):

    GET  /api/catalog/status                       live or snapshot; the derived trim index's last build
    GET  /api/catalog?manufacturer=&model=&year=&trim=&q=&limit=&offset=
                                                    server-side filtering and paging (limit <= 200)
    GET  /api/catalog/manufacturers                 cascading pickers (cached 10 minutes)
    GET  /api/catalog/models?manufacturer=
    GET  /api/catalog/years?manufacturer=&model=
    GET  /api/catalog/trims?manufacturer=&model=&year=
    POST /api/catalog/index/rebuild                 "Rebuild now" of the derived catalog trim index (202)

No write path to MILO anywhere. Without DATABASE_URL the browser answers 409 `catalog_unavailable` and the UI shows the
snapshot (the 50 benchmark records).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...catalog import MAX_LIMIT, SNAPSHOT_LABEL, CatalogQueryRefused, CatalogUnavailable
from ..deps import ApiContext, get_context
from ..errors import ApiError

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CatalogUnavailable as exc:
        raise ApiError(409, "catalog_unavailable", str(exc)) from None
    except CatalogQueryRefused as exc:
        raise ApiError(422, "catalog_filter_refused", str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - a database problem is reported, never a crash
        raise ApiError(503, "catalog_query_failed", f"The catalog query failed: {type(exc).__name__}") from None


@router.get("/status")
def catalog_status(ctx: ApiContext = Depends(get_context)) -> dict:
    live = ctx.catalog_browser.available
    return {"mode": "live" if live else "snapshot", "browser_enabled": live,
            "label": "live MILO catalog (public.catalog_variants_current)" if live else SNAPSHOT_LABEL,
            "max_set": 50, "max_limit": MAX_LIMIT, "derived_index": ctx.manager.derived_index.status()}


@router.get("")
def catalog_search(manufacturer: str | None = None, model: str | None = None, year: int | None = None,
                   trim: str | None = None, q: str | None = Query(None, max_length=60),
                   limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                   ctx: ApiContext = Depends(get_context)) -> dict:
    return _call(ctx.catalog_browser.search, manufacturer=manufacturer, model=model, year=year, trim=trim, q=q,
                 limit=limit, offset=offset)


@router.get("/manufacturers")
def catalog_manufacturers(ctx: ApiContext = Depends(get_context)) -> dict:
    return {"manufacturers": _call(ctx.catalog_browser.manufacturers)}


@router.get("/models")
def catalog_models(manufacturer: str, ctx: ApiContext = Depends(get_context)) -> dict:
    return {"models": _call(ctx.catalog_browser.models, manufacturer)}


@router.get("/years")
def catalog_years(manufacturer: str, model: str, ctx: ApiContext = Depends(get_context)) -> dict:
    return {"years": _call(ctx.catalog_browser.years, manufacturer, model)}


@router.get("/trims")
def catalog_trims(manufacturer: str, model: str, year: int, ctx: ApiContext = Depends(get_context)) -> dict:
    return {"trims": _call(ctx.catalog_browser.trims, manufacturer, model, year)}


@router.post("/index/rebuild", status_code=202)
def catalog_index_rebuild(ctx: ApiContext = Depends(get_context)) -> dict:
    job = ctx.manager.derived_index
    if not job.available:
        raise ApiError(409, "catalog_unavailable", "DATABASE_URL is not set: the repository's index stays in use.")
    started = job.rebuild_async()
    return {"started": started, "status": job.status(),
            "message": "Rebuild started." if started else "A rebuild is already running."}

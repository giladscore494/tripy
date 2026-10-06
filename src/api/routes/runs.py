"""Runs: history, details, progress, events, results, candidates, evidence, and the actions the dashboard offers.

Every action goes through the process-wide RunManager:

    POST /api/runs                                         RunManager.start(ResearchRequest)          ("Start research")
    POST /api/runs/{id}/cancel                             RunManager.cancel                          ("Stop run")
    POST /api/runs/{id}/vehicles/{rid}/finalize            RunManager.retry_finalization   ("Finalize from preserved
                                                                                             research" / "Retry ...")
    POST /api/runs/{id}/vehicles/{rid}/restart             RunManager.start, a new One-vehicle run ("Restart research")

The finalize and restart actions are offered only where the dashboard's failure card offers them
(runstate.failures.explain). A/B series: routes/series.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response

from ...db import Level15Error
from ...glm_client import GLMError
from ...jobs.manager import RunRejected
from ...app_config import blocking_errors
from ...catalog import MAX_SET
from ...research_targets import ALL, MANUFACTURER, ONE, SET, catalog_target, research_target
from ...run_profiles import PRODUCTION
from ...run_settings import build_research_request, settings_checks, settings_for_run, settings_from_env
from ...storage.disk import InsufficientDisk
from ..deps import ApiContext, get_context
from ..errors import ApiError, not_found
from ..schemas import (ActionAccepted, EventsPage, RestartVehicle, RunCandidates, RunDetail, RunEvidence, RunList,
                       RunProgress, RunResults, Started, StartRun)
from .. import service

router = APIRouter(prefix="/api/runs", tags=["runs"])

SCOPES = {"one": ONE, "manufacturer": MANUFACTURER, "all": ALL, "set": SET}


def reconciled(ctx: ApiContext = Depends(get_context)) -> ApiContext:
    """Mark orphaned runs INTERRUPTED (RunManager.maybe_reconcile: at most every 30 s, however often clients poll)."""
    ctx.manager.maybe_reconcile()
    return ctx


# --- reads -----------------------------------------------------------------------------------------------------------

@router.get("", response_model=RunList)
def list_runs(limit: int | None = Query(None, ge=1, le=1000), ctx: ApiContext = Depends(reconciled)) -> dict:
    """Run history, newest first, from the durable run repository (the dashboard's sidebar)."""
    records = ctx.manager.list_runs()
    executing = service.executing_ids(ctx)
    return service.redacted({"total": len(records),
                             "runs": [service.summary(r, executing) for r in records[:limit]]})


@router.get("/{run_id}", response_model=RunDetail)
def run_detail(run_id: str, ctx: ApiContext = Depends(reconciled)) -> dict:
    return service.redacted(service.run_detail(ctx, service.get_record(ctx, run_id)))


@router.get("/{run_id}/progress", response_model=RunProgress)
def run_progress(run_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    return service.redacted(service.run_progress(ctx, service.get_record(ctx, run_id)))


@router.get("/{run_id}/events", response_model=EventsPage)
def run_events(run_id: str, after: int = Query(0, ge=0, description="the next_cursor of the previous page"),
               limit: int = Query(200, ge=1, le=1000), record_id: str | None = None,
               kind: list[str] | None = Query(None, description="only these event kinds (repeatable)"),
               ctx: ApiContext = Depends(get_context)) -> dict:
    record = service.get_record(ctx, run_id)
    rid = service.record_id_of(record, record_id)
    return service.redacted(service.run_events(ctx, record, rid, after, limit, kind))


@router.get("/{run_id}/results", response_model=RunResults)
def run_results(run_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    return service.redacted(service.run_results(ctx, service.get_record(ctx, run_id)))


@router.get("/{run_id}/candidates", response_model=RunCandidates)
def run_candidates(run_id: str, record_id: str | None = None, field: str | None = None,
                   ctx: ApiContext = Depends(get_context)) -> dict:
    record = service.get_record(ctx, run_id)
    return service.redacted(service.run_candidates(ctx, record, service.record_id_of(record, record_id), field))


@router.get("/{run_id}/evidence", response_model=RunEvidence)
def run_evidence(run_id: str, record_id: str | None = None, field: str | None = None,
                 ctx: ApiContext = Depends(get_context)) -> dict:
    record = service.get_record(ctx, run_id)
    return service.redacted(service.run_evidence(ctx, record, service.record_id_of(record, record_id), field))


# --- start -----------------------------------------------------------------------------------------------------------

def _start(ctx: ApiContext, vehicles: list[dict], label: str, scope: str, key: str | None, profile: str,
           response: Response, overrides: dict | None = None) -> dict:
    """Start a run: the server's settings with the request's per-run overrides (run_settings.settings_for_run,
    the sidebar's assemble_settings), the same blocking checks, the same request, RunManager.start."""
    try:
        settings = settings_for_run(ctx.secret, ctx.manager.controller, overrides, profile)
    except ValueError as exc:
        raise ApiError(422, "invalid_settings", str(exc)) from None
    blocking = blocking_errors(settings_checks(settings, ctx.secret, ctx.paths))
    if blocking:
        raise ApiError(503, "configuration_incomplete", "The server is not configured to start research.",
                       checks=[{"name": c.name, "status": c.status, "detail": c.detail} for c in blocking])
    request = build_research_request(settings, ctx.secret, vehicles, label, scope, key, profile)
    try:
        started = ctx.manager.start(request)
    except RunRejected as exc:
        raise ApiError(409, "run_rejected", str(exc), existing_run_id=exc.existing_run_id) from None
    except InsufficientDisk as exc:
        raise ApiError(507, "insufficient_disk", str(exc)) from None
    except GLMError as exc:
        raise ApiError(503, "provider_configuration", f"Could not start the run: {exc}") from None
    except Level15Error as exc:
        raise ApiError(503, "level15_unavailable", f"Could not start the run: {exc}") from None
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"Could not start the run: {exc}") from None
    response.status_code = 201 if started.created else 200
    record = ctx.manager.get(started.run_id)
    return service.redacted({"run_id": started.run_id, "created": started.created, "message": started.message,
                             "warnings": list(started.warnings), "settings_overridden": sorted(overrides or {}),
                             "run": service.summary(record, service.executing_ids(ctx)) if record else None})


@router.post("", response_model=Started, status_code=201,
             responses={200: {"model": Started, "description": "the idempotency key already started this run"}})
def start_run(body: StartRun, response: Response, ctx: ApiContext = Depends(get_context)) -> dict:
    """Start research for one vehicle, one manufacturer or the whole benchmark (201; 200 for a repeated
    idempotency key; 409 when a run for the target is active or the active-run limit is reached)."""
    catalog = ctx.catalog
    if body.scope == "set" or (body.scope == "one" and body.record_id and body.record_id not in catalog.by_id):
        # PR #47 (B1): live-catalog vehicles (one, or a filtered set of at most 50), read-only from MILO
        ids = list(dict.fromkeys(body.record_ids or ([body.record_id] if body.record_id else [])))
        if not ids:
            raise ApiError(422, "empty_target", "scope=set needs record_ids.")
        if len(ids) > MAX_SET:
            raise ApiError(422, "too_many_vehicles", f"A catalog set is capped at {MAX_SET} vehicles per run.")
        if not ctx.catalog_browser.available:
            raise ApiError(422, "unknown_vehicle", "Without DATABASE_URL only the benchmark vehicles (snapshot) can run.")
        try:
            vehicles = ctx.catalog_browser.vehicles(ids)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(503, "catalog_query_failed", f"The catalog query failed: {type(exc).__name__}") from None
        missing = [i for i in ids if i not in {v["upstream_record_id"] for v in vehicles}]
        if missing:
            raise ApiError(422, "unknown_vehicle", f"Not in the live catalog: {', '.join(missing[:10])}.")
        vehicles, label = catalog_target(vehicles)
        overrides = body.settings.model_dump(exclude_none=True) if body.settings is not None else None
        return _start(ctx, vehicles, label, ONE if body.scope == "one" else SET, body.idempotency_key, body.profile,
                      response, overrides)
    if body.scope == "one":
        if not body.record_id or body.record_id not in catalog.by_id:
            raise ApiError(422, "unknown_vehicle", "scope=one needs the record_id of a benchmark vehicle.")
        value = body.record_id
    elif body.scope == "manufacturer":
        if not body.manufacturer or body.manufacturer not in catalog.manufacturers():
            raise ApiError(422, "unknown_manufacturer", "scope=manufacturer needs a benchmark manufacturer.")
        value = body.manufacturer
    else:
        value = None
    vehicles, label = research_target(catalog, SCOPES[body.scope], value)
    if not vehicles:
        raise ApiError(422, "empty_target", "The target has no vehicles.")
    overrides = body.settings.model_dump(exclude_none=True) if body.settings is not None else None
    return _start(ctx, vehicles, label, SCOPES[body.scope], body.idempotency_key, body.profile, response, overrides)


# --- cancel / finalize / restart -------------------------------------------------------------------------------------

@router.post("/{run_id}/cancel", response_model=ActionAccepted, status_code=202)
def cancel_run(run_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    """RunManager.cancel: the run stops at its next safe point and keeps what it completed (202). 409 when no worker
    of this server process executes it."""
    record = service.get_record(ctx, run_id)
    if not ctx.manager.cancel(record.run_id):
        raise ApiError(409, "not_executing", "This run is not executing in this server process.",
                       status=record.status)
    return {"run_id": record.run_id, "status": (ctx.manager.get(record.run_id) or record).status,
            "message": "Cancellation requested; the run stops at its next safe point."}


def _failure_card(ctx: ApiContext, run_id: str, record_id: str):
    record = service.get_record(ctx, run_id)
    if record_id not in record.record_ids:
        raise not_found("record_id", record_id)
    if record.active:
        raise ApiError(409, "run_active", "This run is still active.", status=record.status)
    titles = dict(service.vehicles_of(record))
    state = service.vehicle_state(ctx, record, record_id, titles.get(record_id, record_id))
    return record, state["failure"]


@router.post("/{run_id}/vehicles/{record_id}/finalize", response_model=ActionAccepted, status_code=202)
def finalize_vehicle(run_id: str, record_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    """RunManager.retry_finalization: build the final result from the saved research only (no search, fetch,
    harvest, sweep or recovery), with the run's own models. Allowed only where the failure card offers it."""
    record, failure = _failure_card(ctx, run_id, record_id)
    if not failure or "finalize" not in failure["actions"]:
        raise ApiError(409, "finalize_not_available", "Finalization from preserved research is not available for "
                                                      "this vehicle.")
    settings = settings_from_env(ctx.secret, ctx.manager.controller)
    try:
        ctx.manager.retry_finalization(record.run_id, record_id, settings.glm_settings())
    except RunRejected as exc:
        raise ApiError(409, "run_rejected", str(exc), existing_run_id=exc.existing_run_id) from None
    except InsufficientDisk as exc:
        raise ApiError(507, "insufficient_disk", str(exc)) from None
    except GLMError as exc:
        raise ApiError(503, "provider_configuration", f"Could not start the finalization: {exc}") from None
    return {"run_id": record.run_id, "record_id": record_id,
            "status": (ctx.manager.get(record.run_id) or record).status,
            "message": "Finalization from the preserved research started."}


@router.post("/{run_id}/vehicles/{record_id}/restart", response_model=Started, status_code=201,
             responses={200: {"model": Started, "description": "the idempotency key already started this run"}})
def restart_vehicle(run_id: str, record_id: str, response: Response, body: RestartVehicle | None = None,
                    ctx: ApiContext = Depends(get_context)) -> dict:
    """A new One-vehicle run of the same vehicle with the run's profile (the old run stays untouched)."""
    record, failure = _failure_card(ctx, run_id, record_id)
    if not failure or "restart" not in failure["actions"]:
        raise ApiError(409, "restart_not_available", "Restart is offered only for a vehicle that did not complete.")
    vehicle = ctx.catalog.by_id.get(record_id)
    if vehicle is None:
        raise ApiError(409, "vehicle_not_in_benchmark", "This vehicle is not in the benchmark sample any more.")
    key = (body.idempotency_key if body else None) or f"restart:{uuid.uuid4().hex}"
    profile = (record.request or {}).get("run_profile") or PRODUCTION
    return _start(ctx, [vehicle], ctx.catalog.title(record_id), ONE, key, profile, response)

"""A/B benchmark series, through the process-wide RunManager (the former dashboard's "Benchmark A/B" semantics):

    POST /api/series                     RunManager.start_series(SeriesRequest)     ("Start A/B series")
    POST /api/series/{id}/cancel         RunManager.cancel_series                    ("Cancel series")
    GET  /api/series, /api/series/{id}   RunManager.list_series / get_series + series_progress (runs/_series/<id>)
    GET  /api/series/{id}/export/{file}  the benchmark files the finished series wrote ("Benchmark diagnostics")

A request carries what the A/B form offers: benchmark vehicles, runs per arm (1-5), arms from run_profiles.ARMS (run in
ARMS order, interleaved per repeat by jobs.manager.plan_series) and optional per-run settings, the SAME typed
RunSettingsOverrides as POST /api/runs, through the same run_settings.settings_for_run. Those settings become the
SeriesRequest template; each arm's AgentConfig is settings.agent_config(secret, arm), so the arm's named profile pins
its experiment settings as before. Endpoints, credentials and the process-wide limits stay the server's; one series
executes at a time per process (RunManager.start_series).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from ...app_config import blocking_errors
from ...db import Level15Error
from ...storage.disk import InsufficientDisk
from ...diagnostics import PARSER_GAPS_FILE
from ...glm_client import GLMError
from ...jobs.manager import RunManager, RunRejected, SeriesRequest, series_progress
from ...run_profiles import ARMS, BASELINE, PRODUCTION, PROFILE_LABELS, TREATMENT
from ...run_settings import build_research_request, settings_checks, settings_for_run
from ..deps import ApiContext, get_context
from ..errors import ApiError, not_found
from ..schemas import SeriesList, SeriesStarted, SeriesState, StartSeries
from .. import service

router = APIRouter(prefix="/api/series", tags=["series"])

SERIES_SCOPE = "Benchmark A/B"
# the files a finished series writes into runs/_series/<id>/ that the dashboard offers for download
EXPORTS = {"benchmark.json": "application/json", "per_vehicle.csv": "text/csv; charset=utf-8",
           PARSER_GAPS_FILE: "application/x-ndjson", "binding_replay_items.jsonl": "application/x-ndjson"}
ITEM_KEYS = ("index", "repeat", "arm", "run_id", "status", "started_at", "finished_at")


def _executing(manager: RunManager, series: list[dict]) -> str | None:
    return next((s["series_id"] for s in series if manager.series_executing(s["series_id"])), None)


def series_state(manager: RunManager, series: dict) -> dict:
    """The durable series.json as a client sees it: no owner, no server paths (the benchmark's `dir`)."""
    bench = series.get("benchmark")
    folder = manager.series_dir(series["series_id"])
    benchmark = None if not isinstance(bench, dict) else {
        "files": [name for name in EXPORTS if (folder / name).is_file()], "run_ids": list(bench.get("run_ids") or []),
        "complete": bool(bench.get("complete")), "written_at": bench.get("written_at")}
    return {"series_id": series["series_id"], "status": series.get("status"), "label": series.get("label") or "",
            "created_at": series.get("created_at"), "updated_at": series.get("updated_at"),
            "finished_at": series.get("finished_at"), "arms": list(series.get("arms") or []),
            "arm_labels": {arm: PROFILE_LABELS.get(arm, arm) for arm in series.get("arms") or []},
            "repeats": int(series.get("repeats") or 0), "vehicles": list(series.get("vehicles") or []),
            "planned": [{**{k: item.get(k) for k in ITEM_KEYS}, "arm_label": PROFILE_LABELS.get(item.get("arm"),
                                                                                                 item.get("arm"))}
                        for item in series.get("planned") or []],
            "current_index": series.get("current_index"), "run_ids": list(series.get("run_ids") or []),
            "error": series.get("error"), "benchmark": benchmark, "arm_configs": series.get("arm_configs"),
            "executing": manager.series_executing(series["series_id"]), "series_progress": series_progress(series)}


def _get(ctx: ApiContext, series_id: str) -> dict:
    try:
        series = ctx.manager.get_series(series_id)
    except RunRejected:            # an invalid id (separators, leading dot)
        series = None
    if series is None or series.get("series_id") != series_id:
        raise not_found("series", series_id)
    return series


@router.get("", response_model=SeriesList)
def list_series(limit: int | None = Query(None, ge=1, le=200), ctx: ApiContext = Depends(get_context)) -> dict:
    """Series history, newest first (runs/_series), and the one this process is driving now."""
    series = ctx.manager.list_series()
    return service.redacted({"total": len(series), "executing": _executing(ctx.manager, series),
                             "arms": [{"id": arm, "label": PROFILE_LABELS[arm]} for arm in ARMS],
                             "default_arms": [BASELINE, TREATMENT], "max_repeats": 5,
                             "series": [series_state(ctx.manager, s) for s in series[:limit]]})


@router.get("/{series_id}", response_model=SeriesState)
def get_series(series_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    return service.redacted(series_state(ctx.manager, _get(ctx, series_id)))


@router.post("", response_model=SeriesStarted, status_code=201,
             responses={200: {"model": SeriesStarted, "description": "the idempotency key already started it"}})
def start_series(body: StartSeries, response: Response, ctx: ApiContext = Depends(get_context)) -> dict:
    """Start an A/B series: the server's settings with the request's per-run overrides (run_settings.settings_for_run,
    exactly as POST /api/runs), the same blocking checks, the request template built from those settings, one
    AgentConfig per arm (UISettings.agent_config(secret, arm)), RunManager.start_series. 201; 200 for a repeated
    idempotency key; 409 while another series executes; 422 for invalid settings."""
    catalog = ctx.catalog
    record_ids = list(dict.fromkeys(body.record_ids))
    unknown = [rid for rid in record_ids if rid not in catalog.by_id]
    if unknown:
        raise ApiError(422, "unknown_vehicle", "Every record_id must be a benchmark vehicle.",
                       record_ids=unknown[:20])
    arms = [arm for arm in ARMS if arm in body.arms]              # the dashboard's checkbox order
    overrides = body.settings.model_dump(exclude_none=True) if body.settings is not None else None
    try:
        settings = settings_for_run(ctx.secret, ctx.manager.controller, overrides, arms[0] if arms else None)
    except ValueError as exc:
        raise ApiError(422, "invalid_settings", str(exc)) from None
    blocking = blocking_errors(settings_checks(settings, ctx.secret, ctx.paths))
    if blocking:
        raise ApiError(503, "configuration_incomplete", "The server is not configured to start research.",
                       checks=[{"name": c.name, "status": c.status, "detail": c.detail} for c in blocking])
    selection = [catalog.by_id[rid] for rid in record_ids]
    label = f"A/B · {len(selection)} vehicle(s)"
    key = f"series:{body.idempotency_key}" if body.idempotency_key else None
    request = SeriesRequest(
        template=build_research_request(settings, ctx.secret, selection, label, SERIES_SCOPE, None, PRODUCTION),
        arm_configs={arm: settings.agent_config(ctx.secret, arm) for arm in arms}, repeats=body.repeats,
        label=label, idempotency_key=key)
    try:
        started = ctx.manager.start_series(request)
    except RunRejected as exc:
        raise ApiError(409, "series_rejected", str(exc),
                       existing_series_id=_executing(ctx.manager, ctx.manager.list_series(limit=20))) from None
    except InsufficientDisk as exc:
        from .runs import disk_refusal

        raise disk_refusal(ctx, exc) from None
    except GLMError as exc:
        raise ApiError(503, "provider_configuration", f"Could not start the series: {exc}") from None
    except Level15Error as exc:
        raise ApiError(503, "level15_unavailable", f"Could not start the series: {exc}") from None
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"Could not start the series: {exc}") from None
    response.status_code = 201 if started.created else 200
    series = ctx.manager.get_series(started.run_id)
    return service.redacted({"series_id": started.run_id, "created": started.created, "message": started.message,
                             "settings_overridden": sorted(overrides or {}),
                             "series": series_state(ctx.manager, series) if series else None})


@router.post("/{series_id}/cancel", status_code=202, response_model=SeriesState)
def cancel_series(series_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    """RunManager.cancel_series: the running run stops at its next safe point, planned runs are not started (202).
    409 when this process is not driving the series."""
    series = _get(ctx, series_id)
    if not ctx.manager.cancel_series(series_id):
        raise ApiError(409, "not_executing", "This series is not executing in this server process.",
                       status=series.get("status"))
    return service.redacted(series_state(ctx.manager, ctx.manager.get_series(series_id) or series))


@router.get("/{series_id}/export/{name}", response_class=Response)
def series_export(series_id: str, name: str, ctx: ApiContext = Depends(get_context)) -> Response:
    """One benchmark file the finished series wrote over exactly its own runs (the dashboard's download)."""
    from .exports import _download

    _get(ctx, series_id)
    if name not in EXPORTS:
        raise not_found("export", name)
    path = ctx.manager.series_dir(series_id) / name
    if not path.is_file() or not service.safety.inside(path, ctx.runs_dir):
        raise ApiError(404, "nothing_to_export", "This series has not written that file.")
    return _download(path.read_text("utf-8", errors="replace"), f"{series_id}_{name}", EXPORTS[name])

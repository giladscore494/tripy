"""Search bake-offs (PR #46, S4), through the process-wide RunManager's bake-off jobs (src/jobs/bakeoffs.py):

    GET  /api/bakeoffs/options             the backends (available or not, and why), the benchmark records, defaults
    POST /api/bakeoffs                     start one (backends, record_ids, fetch_top); 201, 409 while one runs, 422 for
                                           an unknown / unavailable backend or an unknown record
    GET  /api/bakeoffs, /api/bakeoffs/{id} the jobs; one job's status, metrics and per-record rows
    POST /api/bakeoffs/{id}/cancel         stop it at the next record (it keeps what it measured)
    GET  /api/bakeoffs/{id}/export/{file}  metrics.csv, records.csv, summary.json

Keys are the server's; a backend without its key is listed as unavailable and refused, never swapped.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from ...jobs.bakeoffs import BakeoffRejected
from ...search_bakeoff import DEFAULT_BACKENDS, BakeoffConfig, metrics_csv, read_rows, records_csv
from ...tools.search_backends import backend_status
from ..deps import ApiContext, get_context
from ..errors import ApiError, not_found
from .. import service

router = APIRouter(prefix="/api/bakeoffs", tags=["bakeoffs"])

BAKEOFF_BACKENDS = DEFAULT_BACKENDS          # the page's checkboxes (glm, serper, gemini)
EXPORTS = {"metrics.csv": "text/csv; charset=utf-8", "records.csv": "text/csv; charset=utf-8",
           "summary.json": "application/json"}


class StartBakeoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backends: list[str] = Field(min_length=1, max_length=len(BAKEOFF_BACKENDS))
    record_ids: list[str] | None = Field(None, description="benchmark record ids; absent or empty = all of them")
    fetch_top: bool = True
    label: str | None = Field(None, max_length=120)


def _jobs(ctx: ApiContext):
    return ctx.manager.bakeoffs


def _options(ctx: ApiContext) -> dict:
    status = backend_status(lambda n: ctx.secret(n))
    backends = [{"name": name, **status[name]} for name in BAKEOFF_BACKENDS]
    for b in backends:
        if b["name"] == "glm" and not (ctx.secret("GLM_API_KEY") or "").strip():
            b.update(available=False, reason="GLM_API_KEY is not set")
    records = [{"record_id": str(v["upstream_record_id"]), "label": ctx.catalog.title(str(v["upstream_record_id"]))}
               for v in ctx.catalog.vehicles]
    return {"backends": backends, "records": records,
            "defaults": {"backends": [b["name"] for b in backends if b["available"]], "record_ids": [],
                         "fetch_top": True},
            "note": "A backend without its key is unavailable. Results never enter a run's evidence."}


@router.get("/options")
def bakeoff_options(ctx: ApiContext = Depends(get_context)) -> dict:
    return _options(ctx)


@router.get("")
def list_bakeoffs(ctx: ApiContext = Depends(get_context)) -> dict:
    return service.redacted({"bakeoffs": _jobs(ctx).list(100)})


@router.post("", status_code=201)
def start_bakeoff(body: StartBakeoff, ctx: ApiContext = Depends(get_context)) -> dict:
    options = _options(ctx)
    known = {b["name"]: b for b in options["backends"]}
    backends = list(dict.fromkeys(body.backends))
    unknown = [b for b in backends if b not in known]
    if unknown:
        raise ApiError(422, "unknown_backend", "Choose backends from: " + ", ".join(known) + ".", backends=unknown)
    unavailable = {b: known[b]["reason"] for b in backends if not known[b]["available"]}
    if unavailable:
        raise ApiError(422, "backend_unavailable", "Selected backends are unavailable on this server: "
                       + "; ".join(f"{b} ({why})" for b, why in unavailable.items()) + ".")
    record_ids = list(dict.fromkeys(body.record_ids or []))
    missing = [rid for rid in record_ids if rid not in ctx.catalog.by_id]
    if missing:
        raise ApiError(422, "unknown_vehicle", "Every record_id must be a benchmark vehicle.", record_ids=missing[:20])
    config = BakeoffConfig(backends=backends, records=record_ids, fetch_top=1 if body.fetch_top else 0,
                           label=(body.label or "").strip())
    try:
        bakeoff_id = _jobs(ctx).start(config)
    except BakeoffRejected as exc:
        raise ApiError(409, "bakeoff_rejected", str(exc)) from None
    return {"bakeoff_id": bakeoff_id, "bakeoff": _jobs(ctx).get(bakeoff_id)}


@router.get("/{bakeoff_id}")
def get_bakeoff(bakeoff_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    jobs = _jobs(ctx)
    state = jobs.get(bakeoff_id)
    if state is None:
        raise not_found("bakeoff", bakeoff_id)
    return service.redacted({"bakeoff": state, "summary": jobs.summary(bakeoff_id),
                             "records": read_rows(jobs.dir(bakeoff_id) / "records.jsonl")})


@router.post("/{bakeoff_id}/cancel")
def cancel_bakeoff(bakeoff_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    jobs = _jobs(ctx)
    if jobs.get(bakeoff_id) is None:
        raise not_found("bakeoff", bakeoff_id)
    return {"cancelled": jobs.cancel(bakeoff_id), "bakeoff": jobs.get(bakeoff_id)}


@router.get("/{bakeoff_id}/export/{name}")
def export_bakeoff(bakeoff_id: str, name: str, ctx: ApiContext = Depends(get_context)) -> Response:
    jobs = _jobs(ctx)
    if name not in EXPORTS:
        raise not_found("export", name)
    if jobs.get(bakeoff_id) is None:
        raise not_found("bakeoff", bakeoff_id)
    summary = jobs.summary(bakeoff_id)
    if name == "metrics.csv":
        if summary is None:
            raise ApiError(409, "not_finished", "The bake-off has no metrics yet.")
        body = metrics_csv(summary)
    elif name == "records.csv":
        body = records_csv(read_rows(jobs.dir(bakeoff_id) / "records.jsonl"))
    else:
        if summary is None:
            raise ApiError(409, "not_finished", "The bake-off has no summary yet.")
        import json

        body = json.dumps(summary, ensure_ascii=False, indent=1)
    return Response(body.encode("utf-8"), media_type=EXPORTS[name],
                    headers={"Content-Disposition": f'attachment; filename="{bakeoff_id}-{name}"'})

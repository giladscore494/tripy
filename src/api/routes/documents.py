"""Documents, Binding Replay and run diagnostics: read-only views the dashboard's run tabs offer, served by the SAME
read-only implementations the MCP uses (mcp_server.tools.Observer) and the same diagnostics pipeline as the
benchmark export (diagnostics.diagnostics_from_dir / aggregate):

    GET /api/runs/{id}/documents?record_id=          Observer.documents          (the Documents tab's table)
    GET /api/documents/{doc_id}/text                 Observer.document_text      ("Preview document")
    GET /api/documents/{doc_id}/structure            Observer.document_structure (tables, DOM pairs, variant map)
    GET /api/runs/{id}/binding-replay?record_id=     Observer.binding_replay     (Technical details · Binding)
    GET /api/runs/{id}/diagnostics                   diagnostics over the run's vehicles (benchmark.json content)

The Observer's guards apply unchanged: ids validated against what exists on disk, every path contained in the runs /
cache folders, responses paged and capped (safety.page), redaction. Nothing is written: documents are read through a
ReadOnlyCache, Binding Replay and diagnostics run with persist=False exactly as the MCP does.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ... import diagnostics as diag_mod
from ...mcp_server.safety import ToolInputError
from ...mcp_server.tools import Observer
from ...server_logging import get_logger
from ..deps import ApiContext, get_context
from ..errors import ApiError
from ..schemas import BindingReplay, DocumentStructure, DocumentText, RunDiagnostics, RunDocuments
from .. import service

router = APIRouter(prefix="/api", tags=["documents"])
log = get_logger("api")
AGGREGATE_CLIP = 2_000


def _input_error(exc: ToolInputError) -> ApiError:
    """The Observer's rejection (unknown / invalid id, traversal): 404 for an unknown id, 422 otherwise."""
    message = service.safety.Redactor().text(str(exc))
    if message.startswith("unknown"):
        return ApiError(404, "not_found", message[:200])
    return ApiError(422, "invalid_request", message[:200])


def _vehicle(ctx: ApiContext, run_id: str, record_id: str | None) -> tuple:
    record = service.get_record(ctx, run_id)
    return record, service.record_id_of(record, record_id)


@router.get("/runs/{run_id}/documents", response_model=RunDocuments)
def run_documents(run_id: str, record_id: str | None = None, offset: int = Query(0, ge=0),
                  limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_context)) -> dict:
    """The documents one vehicle run fetched: title, URL, source authority, market, type, HTTP status, size, fetch
    time and the fields its admitted evidence supplied."""
    record, rid = _vehicle(ctx, run_id, record_id)
    try:
        page = Observer(ctx.paths).documents(record.run_id, rid, offset, limit)
    except ToolInputError:            # the vehicle has not written anything yet (queued)
        page = {"run_id": record.run_id, "record_id": rid, "documents": [],
                "paging": {"offset": 0, "limit": limit, "returned": 0, "total": 0, "remaining": 0,
                           "next_offset": None}}
    return service.redacted(page)


@router.get("/documents/{doc_id}/text", response_model=DocumentText)
def document_text(doc_id: str, offset: int = Query(0, ge=0), limit: int = Query(20_000, ge=1, le=50_000),
                  ctx: ApiContext = Depends(get_context)) -> dict:
    """A cached document's extracted text, in character pages (next_offset)."""
    try:
        return service.redacted(Observer(ctx.paths).document_text(doc_id, offset, limit))
    except ToolInputError as exc:
        raise _input_error(exc) from None


@router.get("/documents/{doc_id}/structure", response_model=DocumentStructure)
def document_structure(doc_id: str, run_id: str | None = None, record_id: str | None = None,
                       offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                       ctx: ApiContext = Depends(get_context)) -> dict:
    """Tables (with column identity), DOM pair groups and the Document Variant Map; with run_id (+ record_id for a
    multi-vehicle run) the map is computed in memory for that run's target."""
    if run_id is not None:
        _record, record_id = _vehicle(ctx, run_id, record_id)
    elif record_id is not None:
        raise ApiError(422, "run_id_required", "record_id needs run_id.")
    try:
        return service.redacted(Observer(ctx.paths).document_structure(doc_id, run_id, record_id, offset, limit))
    except ToolInputError as exc:
        raise _input_error(exc) from None


@router.get("/runs/{run_id}/binding-replay", response_model=BindingReplay)
def binding_replay(run_id: str, record_id: str | None = None, field: str | None = None,
                   offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                   ctx: ApiContext = Depends(get_context)) -> dict:
    """Today's binding over the vehicle's admitted evidence and open-field candidates (no model, no network), with
    `would be ok` from the field evaluator on an in-memory copy. Never persisted (persist=False, as the MCP)."""
    record, rid = _vehicle(ctx, run_id, record_id)
    try:
        page = Observer(ctx.paths).binding_replay(record.run_id, rid, field, offset, limit)
    except ToolInputError as exc:
        if "events.jsonl" in str(exc):
            raise ApiError(409, "nothing_to_replay", "This vehicle run has no events to replay yet.") from None
        raise _input_error(exc) from None
    except TimeoutError:
        raise ApiError(504, "replay_timeout", "Binding replay took too long for this vehicle run.") from None
    except Exception as exc:  # noqa: BLE001 - logged server-side; never a traceback to the client
        log.warning("binding replay %s/%s failed: %s", record.run_id, rid, type(exc).__name__, exc_info=exc)
        raise ApiError(500, "replay_failed", "Binding replay failed for this vehicle run.") from None
    return service.redacted(page)


@router.get("/runs/{run_id}/diagnostics", response_model=RunDiagnostics)
def run_diagnostics(run_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    """The run's vehicle diagnostics through the benchmark export's own pipeline (diagnostics.aggregate over
    diagnostics_from_dir), read-only (persist_replay=False). The metrics are the server's; a client computes none."""
    record = service.get_record(ctx, run_id)
    diags = [d for d in (diag_mod.diagnostics_from_dir(p, persist_replay=False)
                         for p in diag_mod.vehicle_dirs(ctx.runs_dir, [record.run_id])) if d]
    aggregate = diag_mod.aggregate(diags) if diags else {}
    per_vehicle = aggregate.pop("per_vehicle", [])
    return service.redacted({"run_id": record.run_id, "vehicles": len(diags),
                             "aggregate": service.safety.clip(aggregate, AGGREGATE_CLIP),
                             "per_vehicle": service.safety.clip(per_vehicle, AGGREGATE_CLIP)})

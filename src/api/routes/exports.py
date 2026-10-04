"""Downloads, with the dashboard's own serializers (src/exports.py) and file names:

    GET /api/runs/{id}/export/candidates.csv?record_id=   the Candidates tab's "Download candidates.csv"
    GET /api/runs/{id}/export/per_vehicle.csv             "Benchmark diagnostics" -> per_vehicle.csv for this run
    GET /api/runs/{id}/export/benchmark.json              "Benchmark diagnostics" -> benchmark.json for this run

404 `nothing_to_export` where the dashboard offers no download (no candidates / no vehicle diagnostics yet).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Response

from ... import exports
from ..deps import ApiContext, get_context
from ..errors import ApiError
from .. import service

router = APIRouter(prefix="/api/runs", tags=["exports"])


def _download(text: str, filename: str, media_type: str) -> Response:
    """The dashboard's bytes; only a literal configured secret value (never expected in an export) is replaced, so
    the file stays byte-identical to the dashboard's download otherwise."""
    for value in service.safety.secret_values():
        text = text.replace(value, "[redacted]")
    return Response(content=text.encode("utf-8"), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _nothing(what: str) -> ApiError:
    return ApiError(404, "nothing_to_export", f"This run has no {what} to export yet.")


@router.get("/{run_id}/export/candidates.csv", response_class=Response)
def candidates_csv(run_id: str, record_id: str | None = None, ctx: ApiContext = Depends(get_context)) -> Response:
    record = service.get_record(ctx, run_id)
    rid = service.record_id_of(record, record_id)
    rows = exports.candidate_rows(exports.vehicle_events(ctx.runs_dir, record.run_id, rid))
    if not rows:
        raise _nothing("candidates")
    return _download(exports.candidates_csv(rows), "tripy_candidates.csv", "text/csv; charset=utf-8")


def _benchmark(ctx: ApiContext, run_id: str) -> dict:
    record = service.get_record(ctx, run_id)
    diags = exports.run_diagnostics(Path(ctx.runs_dir), [record.run_id])
    if not diags:
        raise _nothing("vehicle diagnostics")
    return exports.benchmark_export(diags)


@router.get("/{run_id}/export/per_vehicle.csv", response_class=Response)
def per_vehicle_csv(run_id: str, ctx: ApiContext = Depends(get_context)) -> Response:
    result = _benchmark(ctx, run_id)
    if not result["per_vehicle"]:
        raise _nothing("vehicle diagnostics")
    return _download(exports.per_vehicle_csv(result), "tripy_per_vehicle.csv", "text/csv; charset=utf-8")


@router.get("/{run_id}/export/benchmark.json", response_class=Response)
def benchmark_json(run_id: str, ctx: ApiContext = Depends(get_context)) -> Response:
    return _download(exports.benchmark_json(_benchmark(ctx, run_id)), "tripy_benchmark.json", "application/json")

"""Technical views of runs, the benchmark observation metrics and multi-run benchmark exports: what the former
dashboard showed under Technical details, its Benchmark tab, its "Benchmark diagnostics" sidebar and its
System panel. Data only, through the framework-neutral view models (presentation.run_views) over the same loaders:

    GET /api/runs/{id}/vehicles/{rid}/technical   one finished vehicle: notices, human view extras, raw output, partial
                                                  research, evidence QA, tool calls, model responses, Level 1.5 input,
                                                  config & cost, API attempts, field recovery, layered candidates,
                                                  brief + detailed acquisition / sweep diagnostics
    GET /api/runs/{id}/live?record_id=            an active vehicle: the live field table, the activity log, the brief
    GET /api/runs/{id}/benchmark                  observation metrics per vehicle (benchmark.compute_metrics), aggregate,
                                                  tool usage by name
    GET /api/benchmark/batches                    "Compare batches": one aggregate row per recorded batch
    GET /api/benchmark/summary?run_id=...         the aggregate diagnostics over several runs (and which files exist)
    GET /api/benchmark/export/{name}?run_id=...   benchmark.json, per_vehicle.csv, parser_gaps.jsonl,
                                                  binding_replay_items.jsonl over several runs (the dashboard's bytes)
    GET /api/cache/documents                      the shared document cache ("Entire cache") and cache counters
    GET /api/config/reachability                  network reachability of the GLM base URL (no credentials sent)

Nothing here starts, changes or deletes research state. Everything returned is redacted; long strings are clipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Response

from ... import diagnostics as diag_mod
from ... import exports
from ...app_config import endpoint_reachable
from ...presentation import labels_he as he
from ...presentation import run_views as rv
from ...run_settings import settings_from_env
from ...storage.run_loader import load_run, load_runs, run_document_metas
from ...storage.run_log import load_input
from ..deps import ApiContext, get_context
from ..errors import ApiError, not_found
from .. import service

router = APIRouter(prefix="/api", tags=["technical"])

CLIP = 4_000
LONG_CLIP = 50_000
LIVE_LINES = 60
MAX_RUNS = 200
BENCHMARK_EXPORTS = {"benchmark.json": ("tripy_benchmark.json", "application/json"),
                     "per_vehicle.csv": ("tripy_per_vehicle.csv", "text/csv; charset=utf-8"),
                     "parser_gaps.jsonl": ("tripy_parser_gaps.jsonl", "application/x-ndjson"),
                     "binding_replay_items.jsonl": ("binding_replay_items.jsonl", "application/x-ndjson")}


def _vehicle(ctx: ApiContext, run_id: str, record_id: str):
    record = service.get_record(ctx, run_id)
    if record_id not in record.record_ids:
        raise not_found("record_id", record_id)
    return record, dict(service.vehicles_of(record)).get(record_id, record_id)


def _diagnostics(run_dir: Path) -> dict | None:
    """The vehicle's diagnostics without writing into the run folder (as GET /api/runs/{id}/diagnostics)."""
    if not (run_dir / "events.jsonl").is_file():
        return None
    return diag_mod.diagnostics_from_dir(run_dir, persist_replay=False)


@router.get("/runs/{run_id}/vehicles/{record_id}/technical")
def vehicle_technical(run_id: str, record_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    record, title = _vehicle(ctx, run_id, record_id)
    run_dir = Path(ctx.runs_dir) / record.run_id / record_id
    view = service._pipeline(ctx, record, record_id, title).view()
    diag = _diagnostics(run_dir)
    brief = rv.brief_pairs(view, diag)
    if record.active:
        return service.redacted({"run_id": record.run_id, "record_id": record_id, "title": title, "available": False,
                                 "reason": "The run is still active; the live view shows its progress.",
                                 "brief": brief})
    result = load_run(ctx.runs_dir, record.run_id, record_id, cache=ctx.manager.cache)
    if not result:
        return service.redacted({"run_id": record.run_id, "record_id": record_id, "title": title, "available": False,
                                 "reason": "This vehicle has no input, events or result yet.", "brief": brief,
                                 "diagnostics": rv.detailed_diagnostics(diag)})
    events = rv.vehicle_events(ctx.runs_dir, result)
    metas = run_document_metas(result, ctx.runs_dir, ctx.manager.cache)
    output = result.get("output")
    payload = {
        "run_id": record.run_id, "record_id": record_id, "title": title, "available": True, "reason": None,
        "status": result.get("status"), "duration_s": result.get("duration_s"),
        "synthesized": bool(result.get("synthesized")), "recovered": bool(result.get("recovered")),
        "has_output": output is not None, "has_research": rv.has_research(result),
        "no_output_message": None if output is not None else rv.no_output_message(result),
        "error": result.get("error"), "notices": rv.status_notices(result),
        "human": rv.human_view(output) if output is not None else None,
        "raw": {"parse_note": result.get("parse_note"), "output": output,
                "raw_final_text": result.get("raw_final_text") or ""},
        "partial_research": rv.partial_research(result, events, metas),
        "evidence_admission": result.get("evidence_admission") or {},
        "consistency_checks": (result.get("consistency_checks") or {}).get("checks") or [],
        "tool_calls": rv.tool_call_rows(result),
        "model_responses": rv.model_responses(result, events),
        "level15_input": load_input(ctx.runs_dir, record.run_id, record_id) or {},
        "config": rv.config_and_cost(result),
        "api_attempts": rv.api_attempts(result),
        "field_recovery": rv.field_recovery_view(result, events),
        "candidates": rv.candidates_layered(result),
        "brief": brief,
        "diagnostics": rv.detailed_diagnostics(diag),
    }
    payload = {**service.safety.clip({k: v for k, v in payload.items() if k not in ("raw", "model_responses")}, CLIP),
               "raw": service.safety.clip(payload["raw"], LONG_CLIP),
               "model_responses": service.safety.clip(payload["model_responses"], LONG_CLIP)}
    return service.redacted(payload)


@router.get("/runs/{run_id}/live")
def vehicle_live(run_id: str, record_id: str | None = None, ctx: ApiContext = Depends(get_context)) -> dict:
    """The live field-progress table (Hebrew, as the dashboard showed it while a run is active), the last lines of
    the activity log and the brief acquisition / sweep blocks, from the same incremental pipeline state."""
    record = service.get_record(ctx, run_id)
    rid = service.record_id_of(record, record_id)
    title = dict(service.vehicles_of(record)).get(rid, rid)
    pipeline = service._pipeline(ctx, record, rid, title)
    view = pipeline.view()
    live = pipeline.live
    columns = he.FIELD_TABLE_COLUMNS_HE
    return service.redacted(service.safety.clip({
        "run_id": record.run_id, "record_id": rid, "active": record.active, "columns": columns,
        "fields": [[row.get(c) for c in columns] for row in live.field_rows()],
        "lines": list(live.lines)[-LIVE_LINES:], "brief": rv.brief_pairs(view, None)}, CLIP))


@router.get("/runs/{run_id}/benchmark")
def run_benchmark(run_id: str, ctx: ApiContext = Depends(get_context)) -> dict:
    """The Benchmark tab: observation metrics only (what the model and tools did), not a correctness score. Runs
    without a final JSON are counted too; each run's cost uses the pricing recorded with it."""
    record = service.get_record(ctx, run_id)
    if record.active:
        return {"run_id": record.run_id, "available": False, "reason": "The run is still active.",
                "vehicles": 0, "aggregate": None, "columns": [], "rows": [], "tool_usage": [], "cost_note": None}
    catalog = ctx.catalog
    results = load_runs(ctx.runs_dir, record.run_id, cache=ctx.manager.cache)
    view = rv.benchmark_view(results, catalog.by_id, catalog.labels, ctx.manager.cache)
    return service.redacted({"run_id": record.run_id, "available": True, "reason": None, **view})


@router.get("/benchmark/batches")
def benchmark_batches(ctx: ApiContext = Depends(get_context)) -> dict:
    """"Compare batches": aggregate observation metrics of every recorded batch, newest first."""
    rows = rv.batch_rows(Path(ctx.runs_dir), ctx.catalog.by_id, ctx.manager.cache)
    return service.redacted({"batches": rows, "columns": list(rows[0]) if rows else []})


def _run_ids(ctx: ApiContext, run_ids: list[str] | None) -> list[str]:
    chosen = list(dict.fromkeys(run_ids or []))
    if not chosen:
        raise ApiError(422, "run_id_required", "Pass at least one run_id.")
    if len(chosen) > MAX_RUNS:
        raise ApiError(422, "too_many_runs", f"At most {MAX_RUNS} runs at a time.")
    return [service.get_record(ctx, rid).run_id for rid in chosen]


def _benchmark_diags(ctx: ApiContext, run_ids: list[str]) -> list[dict]:
    """The vehicle diagnostics the dashboard's Benchmark diagnostics export reads (src/exports.run_diagnostics)."""
    return exports.run_diagnostics(Path(ctx.runs_dir), run_ids)


@router.get("/benchmark/summary")
def benchmark_summary(run_id: list[str] | None = Query(None, description="repeatable"),
                      ctx: ApiContext = Depends(get_context)) -> dict:
    ids = _run_ids(ctx, run_id)
    diags = _benchmark_diags(ctx, ids)
    result = diag_mod.aggregate(diags) if diags else None
    files = []
    if result:
        files = ["benchmark.json"] + (["per_vehicle.csv"] if result["per_vehicle"] else []) \
            + (["parser_gaps.jsonl"] if diag_mod.parser_gap_rows(diags) else []) + ["binding_replay_items.jsonl"]
    a, s = ((result or {}).get("acquisition") or {}), ((result or {}).get("document_sweep") or {})
    return service.redacted({"run_ids": ids, "vehicles": (result or {}).get("vehicles", 0),
                             "mean_turns": (a.get("turns") or {}).get("mean"),
                             "mean_searches": (a.get("searches_per_vehicle") or {}).get("mean"),
                             "sweep_resolution_rate": s.get("resolution_rate"), "files": files})


@router.get("/benchmark/export/{name}", response_class=Response)
def benchmark_export(name: str, run_id: list[str] | None = Query(None, description="repeatable"),
                     ctx: ApiContext = Depends(get_context)) -> Response:
    """The dashboard's Benchmark diagnostics downloads over the chosen runs, byte-identical to what it offered."""
    from .exports import _download

    if name not in BENCHMARK_EXPORTS:
        raise not_found("export", name)
    ids = _run_ids(ctx, run_id)
    diags = _benchmark_diags(ctx, ids)
    if not diags:
        raise ApiError(404, "nothing_to_export", "The chosen runs have no vehicle diagnostics yet.")
    filename, media = BENCHMARK_EXPORTS[name]
    if name == "benchmark.json":
        text = exports.benchmark_json(diag_mod.aggregate(diags))
    elif name == "per_vehicle.csv":
        result = diag_mod.aggregate(diags)
        if not result["per_vehicle"]:
            raise ApiError(404, "nothing_to_export", "The chosen runs have no per-vehicle rows.")
        text = exports.per_vehicle_csv(result)
    else:
        rows = diag_mod.parser_gap_rows(diags) if name == "parser_gaps.jsonl" else diag_mod.binding_replay_rows(diags)
        if name == "parser_gaps.jsonl" and not rows:
            raise ApiError(404, "nothing_to_export", "The chosen runs recorded no parser gaps.")
        text = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
    return _download(text, filename, media)


@router.get("/cache/documents")
def cache_documents(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                    ctx: ApiContext = Depends(get_context)) -> dict:
    """Every document in the shared cache (metadata only) and this process's cache counters."""
    cache = ctx.manager.cache
    metas = cache.list_documents()
    page = metas[offset:offset + limit]
    rows = [{**{c: m.get(c) for c in rv.DOCUMENT_COLUMNS}, "title": m.get("title")} for m in page]
    remaining = max(0, len(metas) - offset - len(page))
    return service.redacted(service.safety.clip({
        "total": len(metas), "offset": offset, "returned": len(rows), "remaining": remaining,
        "next_offset": offset + len(rows) if remaining else None, "stats": cache.stats_snapshot(),
        "documents": rows}, CLIP))


@router.get("/config/reachability")
def reachability(ctx: ApiContext = Depends(get_context)) -> dict:
    """Whether the configured GLM base URL answers at all (any HTTP status counts; no credentials are sent; cached
    for five minutes). Not an authentication check, and never part of /health."""
    settings = settings_from_env(ctx.secret, ctx.manager.controller)
    if not settings.base_url or not settings.api_key:
        return {"checked": False, "reachable": None, "detail": "GLM is not configured."}
    ok, note = endpoint_reachable(settings.base_url)
    return {"checked": True, "reachable": ok, "detail": note}

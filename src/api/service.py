"""The API's read model of runs, built ONLY from functions the dashboard and the read-only MCP already use:

    run records         RunManager.list_runs / get / active_runs / is_executing      (run_state.json, legacy folders)
    pipeline + counters runstate.pipeline.PipelineCache over each vehicle's events.jsonl (the live dashboard's state)
    failures            runstate.failures.explain                                     (the dashboard's failure card)
    results             storage.run_loader.load_run / load_runs                      (result.json or the trace)
    report              run_state.json report, else runstate.report.vehicle_report   (the dashboard's Run report)
    events              mcp_server.tools.Observer.run_events                          (line cursor, torn-line safe)
    candidates          candidate_harvest.candidate_matrix + runstate.live_state.rejected_candidates_by_field
    evidence            storage.trace.evidence_items + evidence_rejected events + bundle.admission_summary

Everything returned is redacted with the MCP's Redactor (configured secrets, secret-looking env values, credentialed
URLs, Authorization / Bearer values, secret-named keys). Nothing here writes research state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import bundle as bundle_mod
from ..candidate_harvest import candidate_matrix
from ..field_recovery import current_evaluation
from ..fields import normalize_field_name
from ..mcp_server import safety
from ..mcp_server.safety import ToolInputError
from ..mcp_server.tools import Observer
from ..presentation import run_views
from ..runstate.failures import explain
from ..runstate.model import COMPLETED, STATUS_LABELS, TERMINAL_STATUSES, RunRecord
from ..runstate.report import elapsed_s, vehicle_report
from ..runstate.repository import RunStateError
from ..schemas import iter_fields
from ..storage import trace
from ..storage.run_loader import load_run, load_runs
from ..storage.run_log import read_events
from .deps import ApiContext
from .errors import ApiError, not_found

PIPELINE_KEYS = ("current_stage", "activity", "engine_status", "stop_reason", "started_at", "last_event_at",
                 "finished_at", "events_seen", "research_model", "finalizer_model", "counters", "errors")
STRING_CLIP = 4_000


def redacted(value: Any) -> Any:
    return safety.Redactor().obj(value)


# --- records ---------------------------------------------------------------------------------------------------------

def get_record(ctx: ApiContext, run_id: str) -> RunRecord:
    if not run_id or run_id.startswith(("_", ".")):
        raise not_found("run", run_id)
    try:
        record = ctx.manager.get(run_id)
    except RunStateError:
        record = None
    if record is None:
        raise not_found("run", run_id)
    return record


def executing_ids(ctx: ApiContext) -> set[str]:
    """Runs executing now (RunManager.active_runs: a live worker, not just a durable active status)."""
    return {r.run_id for r in ctx.manager.active_runs()}


def summary(record: RunRecord, executing: set[str]) -> dict:
    duration = elapsed_s(record)
    return {"run_id": record.run_id, "label": record.label, "scope": record.target.get("scope"),
            "status": record.status, "status_label": STATUS_LABELS.get(record.status, record.status),
            "stage": record.stage, "profile": (record.request or {}).get("run_profile"),
            "record_ids": record.record_ids, "created_at": record.created_at, "updated_at": record.updated_at,
            "started_at": record.started_at, "finished_at": record.finished_at, "active": record.active,
            "executing": record.run_id in executing, "cancel_requested": record.cancel_requested,
            "legacy": record.legacy, "elapsed_s": round(duration, 1) if duration is not None else None,
            "resolved_fields_text": run_views.resolved_text(record)}


def vehicles_of(record: RunRecord) -> list[tuple[str, str]]:
    """(record id, title) of every vehicle of the run, in run order (as the dashboard lists them)."""
    items = record.target.get("vehicles") or [{"record_id": r, "label": r} for r in record.record_ids]
    return [(str(item["record_id"]), str(item.get("label") or item["record_id"])) for item in items]


def record_id_of(record: RunRecord, record_id: str | None) -> str:
    """The vehicle a per-vehicle endpoint addresses: required for a multi-vehicle run."""
    ids = [rid for rid, _ in vehicles_of(record)]
    if record_id is None:
        if len(ids) == 1:
            return ids[0]
        raise ApiError(422, "record_id_required", "This run has several vehicles; pass record_id.",
                       record_ids=ids)
    if record_id not in ids:
        raise not_found("record_id", record_id)
    return record_id


def _pipeline(ctx: ApiContext, record: RunRecord, record_id: str, title: str):
    return ctx.pipelines.get(Path(ctx.runs_dir) / record.run_id / record_id / "events.jsonl", record_id, title)


def pipeline_view(view: dict) -> dict:
    return {**{k: view.get(k) for k in PIPELINE_KEYS},
            "stages": [{k: s.get(k) for k in ("key", "label", "state", "state_label", "duration_s", "note")}
                       for s in view.get("stages") or []]}


def _result(ctx: ApiContext, record: RunRecord, record_id: str) -> dict | None:
    """The vehicle's result as the dashboard loads it (only once the run is no longer active)."""
    if record.active:
        return None
    return load_run(ctx.runs_dir, record.run_id, record_id, cache=ctx.manager.cache)


def _failure(record: RunRecord, record_id: str, view: dict, result: dict | None) -> dict | None:
    """The failure card of one vehicle (None while active or when it succeeded)."""
    if record.active:
        return None
    vstate = (record.vehicles.get(record_id) or {}).get("status")
    return explain(result, view, run_status=vstate, run_error=record.error if len(record.record_ids) == 1 else None,
                   can_finalize=not record.legacy)


def vehicle_state(ctx: ApiContext, record: RunRecord, record_id: str, title: str) -> dict:
    """Everything known about one vehicle of a run: durable status, live pipeline, failure, result availability."""
    pipeline = _pipeline(ctx, record, record_id, title)
    view = pipeline.view()
    result = _result(ctx, record, record_id)
    failure = _failure(record, record_id, view, result)
    durable = record.vehicles.get(record_id) or {}
    report = None
    if not record.active:
        report = ((record.report or {}).get("vehicles") or {}).get(record_id) or vehicle_report(result, pipeline)
    notices = run_views.result_notices(result, view.get("counters") or {}, failure is not None)
    return {"record_id": record_id, "title": title, "status": durable.get("status") or record.status,
            "engine_status": durable.get("engine_status") or view.get("engine_status"),
            "stage": durable.get("stage"), "error": durable.get("error"), "pipeline": pipeline_view(view),
            "failure": None if failure is None else {k: failure.get(k) for k in (
                "code", "title", "stage", "stage_label", "reason", "preserved", "actions", "technical")},
            "result_available": bool(result and result.get("output") is not None),
            "report": report, "notices": notices, "_view": view}


def progress_counts(vehicles: list[dict]) -> dict:
    return {"vehicles_total": len(vehicles),
            "vehicles_finished": sum(1 for v in vehicles if v["status"] in TERMINAL_STATUSES),
            "vehicles_completed": sum(1 for v in vehicles if v["status"] == COMPLETED)}


def run_detail(ctx: ApiContext, record: RunRecord) -> dict:
    vehicles = [vehicle_state(ctx, record, rid, title) for rid, title in vehicles_of(record)]
    views = [v.pop("_view") for v in vehicles]
    return {**summary(record, executing_ids(ctx)), "request": dict(record.request or {}),
            "status_panel": [[label, value] for label, value in run_views.status_panel_rows(record, views)],
            "heartbeat_at": record.heartbeat_at, "error": record.error, "notes": list(record.notes),
            "jobs": list(record.jobs), "terminal": record.terminal, "completed": record.status == COMPLETED,
            "progress": progress_counts(vehicles), "vehicles": vehicles}


def run_progress(ctx: ApiContext, record: RunRecord) -> dict:
    """Live progress from the same pipeline state the dashboard polls. No percentages: stage states and counts
    with a real denominator only (vehicles of the run; applicable fields once the run declared them)."""
    vehicles = []
    for rid, title in vehicles_of(record):
        view = _pipeline(ctx, record, rid, title).view()
        counters = view.get("counters") or {}
        durable = record.vehicles.get(rid) or {}
        vehicles.append({"record_id": rid, "title": title, "status": durable.get("status") or record.status,
                         "current_stage": view.get("current_stage"), "activity": view.get("activity"),
                         "last_event_at": view.get("last_event_at"),
                         "resolved_fields": counters.get("resolved_fields"),
                         "applicable_fields": counters.get("applicable_fields"),
                         "stages": pipeline_view(view)["stages"], "counters": counters})
    counts = progress_counts(vehicles)
    return {"run_id": record.run_id, "status": record.status,
            "status_label": STATUS_LABELS.get(record.status, record.status), "stage": record.stage,
            "active": record.active, "executing": record.run_id in executing_ids(ctx),
            "completed": counts["vehicles_finished"], "total": counts["vehicles_total"], **counts,
            "updated_at": record.updated_at, "heartbeat_at": record.heartbeat_at, "vehicles": vehicles}


# --- events ----------------------------------------------------------------------------------------------------------

def run_events(ctx: ApiContext, record: RunRecord, record_id: str, after: int, limit: int,
               kinds: list[str] | None) -> dict:
    """One vehicle's events.jsonl after line `after` (1-based line numbers of complete lines). `next_cursor` is the
    line to pass as `after` next; a line still being written is returned by a later call."""
    try:
        page = Observer(ctx.paths).run_events(record.run_id, record_id, after, limit, kinds)
    except ToolInputError:            # the vehicle has not written anything yet (queued): nothing to return
        page = {"events": [], "next": max(0, after), "more": False}
    return {"run_id": record.run_id, "record_id": record_id, "after": max(0, after), "next_cursor": page["next"],
            "more": bool(page["more"]), "returned": len(page["events"]), "events": page["events"]}


# --- results ---------------------------------------------------------------------------------------------------------

def _result_fields(result: dict) -> list[dict]:
    states = ((result.get("research_bundle") or {}).get("field_states") or {})
    return [{"field": name, "value": entry.get("value", entry.get("values")), "unit": entry.get("unit"),
             "market": entry.get("market"), "provenance": entry.get("provenance"), "notes": entry.get("notes"),
             "evidence_ids": list(entry.get("evidence_ids") or []), "state": (states.get(name) or {}).get("state")}
            for name, entry in iter_fields(result.get("output"))]


def vehicle_result(result: dict, title: str) -> dict:
    output = result.get("output") if isinstance(result.get("output"), dict) else None
    finalization = result.get("finalization")
    return {"record_id": str(result.get("record_id")), "title": title, "engine_status": result.get("status"),
            "result_source": result.get("result_source"), "synthesized": bool(result.get("synthesized")),
            "has_output": output is not None, "stop_reason": result.get("stop_reason"), "error": result.get("error"),
            "recovered": bool(result.get("recovered")), "duration_s": result.get("duration_s"),
            "cost": result.get("cost"), "research_model": result.get("research_model") or result.get("model"),
            "finalizer_model": result.get("finalizer_model"), "prompt_version": result.get("prompt_version"),
            "target_market": result.get("target_market"),
            "finalization": {k: v for k, v in finalization.items() if k not in ("raw_text", "repair_text")}
            if isinstance(finalization, dict) else None,
            "summary": (output or {}).get("summary"), "fields": _result_fields(result),
            "conflicts": list((output or {}).get("conflicts") or []),
            "additional_findings": list((output or {}).get("additional_findings") or []),
            "evidence_admission": result.get("evidence_admission"),
            "output_source": result.get("output_source") or ("model" if output is not None else None),
            "no_output_message": None if output is not None else run_views.no_output_message(result)}


def run_results(ctx: ApiContext, record: RunRecord) -> dict:
    if record.active:
        return {"run_id": record.run_id, "available": False, "reason": "The run is still active.", "vehicles": []}
    titles = dict(vehicles_of(record))
    results = load_runs(ctx.runs_dir, record.run_id, cache=ctx.manager.cache)
    return {"run_id": record.run_id, "available": True, "reason": None,
            "output_source_caption": run_views.output_source_caption(results)
            if any(r.get("output") is not None for r in results) else None,
            "vehicles": [safety.clip(vehicle_result(r, titles.get(str(r.get("record_id")), str(r.get("record_id")))),
                                     STRING_CLIP) for r in results]}


# --- candidates and evidence -----------------------------------------------------------------------------------------

def _events(ctx: ApiContext, record: RunRecord, record_id: str) -> list[dict]:
    try:
        vehicle = safety.vehicle_dir(Path(ctx.runs_dir), record.run_id, record_id)
    except ToolInputError:
        return []
    return read_events(vehicle / "events.jsonl")


def run_candidates(ctx: ApiContext, record: RunRecord, record_id: str, field: str | None) -> dict:
    """The candidate matrix of one vehicle (the data behind candidates.csv), structured: per applicable field the
    harvested candidates (value, unit, origin / extraction method, document, quote, hints), the rejected ones with
    their reasons, and the field's current state as the candidate table computes it."""
    from ..runstate.live_state import rejected_candidates_by_field

    events = _events(ctx, record, record_id)
    started = trace.first_event(events, "run_started") or {}
    applicable = [s for s in started.get("requested_field_specs") or [] if s.get("applicable", True)]
    matrix = candidate_matrix(events, applicable, started.get("vehicle_label")) if applicable else {"fields": {}}
    rejected = rejected_candidates_by_field(events)
    evaluation = {e["field"]: e for e in current_evaluation(events, applicable)} if applicable else {}
    wanted = normalize_field_name(field) if field else None
    fields = []
    for spec in applicable:
        name = spec["name"]
        if wanted and name != wanted:
            continue
        entry = evaluation.get(name) or {}
        fields.append({"field": name, "group": spec.get("group"), "state": entry.get("state"),
                       "evidence_ids": list(entry.get("evidence_ids") or []),
                       "candidates": safety.clip(matrix["fields"].get(name) or [], STRING_CLIP),
                       "rejected": safety.clip(rejected.get(name) or [], STRING_CLIP)})
    keys = ("fields_with_candidates", "fields_without_candidates", "candidate_count", "grounded_candidate_count",
            "fields_with_grounded_candidates", "documents", "applicable_fields", "candidate_field_coverage_pct")
    return {"run_id": record.run_id, "record_id": record_id, "field": wanted,
            "summary": {k: matrix.get(k) for k in keys}, "fields": fields}


def run_evidence(ctx: ApiContext, record: RunRecord, record_id: str, field: str | None) -> dict:
    """Admitted evidence (with its binding / authority / variant metadata) and the requests the admission gate
    rejected (with reasons), as recorded in the vehicle's events."""
    events = _events(ctx, record, record_id)
    wanted = normalize_field_name(field) if field else None
    admitted = [i for i in trace.evidence_items(events) if not wanted or normalize_field_name(i.get("field")) == wanted]
    rejected = [{k: v for k, v in e.items() if k != "kind"} for e in events if e.get("kind") == "evidence_rejected"
                and (not wanted or normalize_field_name(e.get("field")) == wanted)]
    return {"run_id": record.run_id, "record_id": record_id, "field": wanted,
            "summary": bundle_mod.admission_summary(events), "admitted": safety.clip(admitted, STRING_CLIP),
            "rejected": safety.clip(rejected, STRING_CLIP)}

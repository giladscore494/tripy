"""End-of-run observability report, built from existing telemetry only.

Sources: the run's result.json `metrics` (src/benchmark.py compute_metrics, already written by the engine), its
`primary_research` summary, and stage timings derived from event timestamps (src/runstate/pipeline.py). Nothing
is re-measured or duplicated; a value the run did not record stays None.
"""

from __future__ import annotations

from .pipeline import VehiclePipeline


def vehicle_report(result: dict | None, pipeline: VehiclePipeline | None) -> dict:
    result = result or {}
    metrics = result.get("metrics") or {}
    primary = result.get("primary_research") or {}
    counters = pipeline.counters() if pipeline is not None else {}
    durations = {stage: pipeline.stage_duration_s(stage) for stage in
                 ("acquisition", "harvest", "sweep", "recovery", "finalization")} if pipeline is not None else {}
    by_stage = dict(counters.get("model_calls_by_stage") or {})
    if metrics:          # usage per phase as recorded by the engine wins over the event count
        by_stage = {"acquisition": metrics.get("primary_model_calls", by_stage.get("acquisition", 0)),
                    "sweep": metrics.get("document_sweep_model_calls", by_stage.get("sweep", 0)),
                    "recovery": metrics.get("field_recovery_model_calls", by_stage.get("recovery", 0)),
                    "finalization": metrics.get("finalizer_model_calls", by_stage.get("finalization", 0))}

    def pick(metric_key: str, counter_key: str | None = None):
        if metric_key in metrics and metrics[metric_key] is not None:
            return metrics[metric_key]
        return counters.get(counter_key) if counter_key else None

    return {
        "record_id": result.get("record_id") or (pipeline.record_id if pipeline else None),
        "engine_status": result.get("status") or (pipeline.engine_status if pipeline else None),
        "stop_reason": result.get("stop_reason") or (pipeline.stop_reason if pipeline else None),
        "primary_research_stop_reason": primary.get("stop_reason"),
        "total_duration_s": result.get("duration_s"),
        "durations_s": durations,
        "search_calls": pick("search_api_calls", "searches"),
        "model_calls": pick("model_calls", "model_calls"),
        "model_calls_by_stage": by_stage,
        "research_turns": pick("primary_research_turns", "research_turns"),
        "fetched_documents": pick("documents_opened", "sources"),
        "useful_documents": primary.get("useful_documents", counters.get("useful_sources")),
        "official_documents": primary.get("official_sources", counters.get("official_sources")),
        # newer runs only (then official_documents = fetched official documents); None for older runs
        "official_urls_discovered": primary.get("official_urls_discovered", counters.get("official_urls_discovered")),
        "target_market_documents": primary.get("target_market_documents", counters.get("target_market_sources")),
        "candidate_count": pick("candidate_count_total", "candidates"),
        "candidate_fields": pick("candidate_fields_total", "candidate_fields"),
        "evidence_admitted": pick("evidence_items", "evidence_admitted"),
        "evidence_rejected": pick("evidence_rejected", "evidence_rejected"),
        "applicable_fields": counters.get("applicable_fields") or metrics.get("requested_fields"),
        "resolved_fields": counters.get("resolved_fields"),
        "fields_with_value": metrics.get("fields_with_value"),
        "cost_usd": metrics.get("cost_usd"),
        "cost_complete": not (result.get("api_stats") or {}).get("unknown_usage_attempts"),
        "timeouts": pick("timeout_count", "timeouts"),
        "note": "observational telemetry; resolved = evidence-backed field state, not a correctness score",
    }

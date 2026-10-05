"""Benchmark v1: fixed sample, selection, batch execution and observational metrics.

Metrics count what happened (fields filled, sources, tool calls, cache reuse,
time, tokens, cost). None of them is a truth score.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .concurrency import BatchCancelled
from .agent import AgentConfig, effective_glm_config, finalizer_model_of, research_model_of, run_vehicle
from .db import build_level15_payload
from .pricing import default_pricing, phase_models_of, phase_run_cost, run_cost, search_cost_inputs
from .schemas import LEVEL3_TOPICS, has_value, iter_fields, target_field_names
from .source_authority import OFFICIAL_CLASSES
from .storage.run_log import RunLog, update_batch, utc_now, write_batch
from .storage.trace import sum_usage
from .tools import ToolConfig

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IDS_PATH = DATA_DIR / "benchmark_v1_ids.json"
# Vehicle #44 (XPeng G6 2026 MAX, NSGHA): the first real-API handshake vehicle.
HANDSHAKE_RECORD_ID = "101122"


def load_benchmark(path: Path = IDS_PATH) -> dict:
    return json.loads(Path(path).read_text("utf-8"))


def benchmark_vehicles(path: Path = IDS_PATH) -> list[dict]:
    return load_benchmark(path)["vehicles"]


def manufacturers(vehicles: list[dict]) -> list[str]:
    seen: list[str] = []
    for v in vehicles:
        if v["manufacturer"] not in seen:
            seen.append(v["manufacturer"])
    return seen


def select_vehicles(vehicles: list[dict], mode: str, value: str | None = None) -> list[dict]:
    """mode: 'one' (value=upstream_record_id), 'manufacturer' (value=name) or 'all'. Order is preserved."""
    if mode == "all":
        return list(vehicles)
    if mode == "manufacturer":
        return [v for v in vehicles if v["manufacturer"] == value]
    if mode == "one":
        return [v for v in vehicles if v["upstream_record_id"] == str(value)]
    raise ValueError(f"Unknown selection mode {mode!r}")


def vehicle_label(v: dict) -> str:
    return f"#{v['ordinal']} {v['manufacturer']} {v['model']} {v['year']} {v['trim']} ({v['upstream_record_id']})"


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def compute_metrics(result: dict, vehicle: dict | None = None, cache=None, pricing: dict | None = None) -> dict:
    """Observational metrics. Cost uses the pricing stored with the run unless `pricing` is given.

    Works for completed runs and for failed / interrupted / reconstructed ones: research
    work is always counted; coverage is simply zero when no structured output exists.
    """
    output = result.get("output")
    # Coverage is measured against the fields this run actually requested (schema-driven).
    requested = result.get("requested_fields")
    targets = list(requested) if requested else target_field_names(propulsion=(vehicle or {}).get("propulsion"))
    fields = iter_fields(output)
    filled_names = {name for name, entry in fields if has_value(entry)}
    stored_ids = {str(item.get("evidence_id")) for item in result.get("evidence", [])}
    # coverage counts a value only when the output entry cites evidence ids that all exist in the run's evidence;
    # `filled_by_output` keeps the old count (any value in the output, whoever wrote it)
    backed_names = {name for name, entry in fields if has_value(entry) and entry.get("evidence_ids")
                    and all(str(i) in stored_ids for i in entry["evidence_ids"])}
    filled_by_output = [name for name in targets if name in filled_names]
    target_filled = [name for name in targets if name in backed_names]
    extra = sorted(filled_names - set(targets) - set(target_field_names(include_electric=True)))
    recovery = result.get("field_recovery") or {}

    urls = set()
    for item in result.get("evidence", []):
        if item.get("source_url"):
            urls.add(item["source_url"])
    for doc_id in result.get("documents", []):
        meta = cache.get(doc_id) if cache is not None else None
        if meta:
            urls.add(meta.get("final_url") or meta.get("url"))
    executed = [call for call in result.get("tool_calls", []) if not call.get("reused") and not call.get("blocked")]
    by_tool = Counter(call["name"] for call in executed)  # real executions; replayed repeats are separate
    counters = result.get("counters", {})
    usage = result.get("usage", {})
    conflicts = output.get("conflicts") if isinstance(output, dict) else None
    findings = output.get("additional_findings") if isinstance(output, dict) else None
    level3 = output.get("level3") if isinstance(output, dict) else None
    level3_topics = [k for k in (level3 or {}) if k in LEVEL3_TOPICS] if isinstance(level3, dict) else []
    search_api_calls = counters.get("search_api_calls", 0)
    stats = result.get("api_stats") or {}
    usage_research = result.get("usage_research")
    usage_finalizer = result.get("usage_finalizer") or {}
    if usage_research is None:  # results written before the research/finalization split
        usage_research, usage_finalizer = usage, {}
    usage_recovery = result.get("usage_field_recovery") or {}
    usage_sweep = result.get("usage_document_sweep") or {}
    layered = result.get("candidate_summary") or {}
    primary = result.get("primary_research") or {}
    sweep = result.get("document_sweep") or {}
    # a sweep / recovery phase that ran on its own model (src/phase_settings.py) keeps that model's prices
    phase_models = phase_models_of(result.get("cost_details"))
    priced_searches, extra_search_usd = search_cost_inputs(counters, search_api_calls)
    cost, _ = phase_run_cost(usage_research=usage_research, usage_sweep=usage_sweep, usage_recovery=usage_recovery,
                             usage_finalizer=usage_finalizer, search_api_calls=priced_searches,
                             extra_search_usd=extra_search_usd,
                             pricing=pricing if pricing is not None else result.get("pricing"),
                             pricing_finalizer=pricing if pricing is not None else (result.get("pricing_finalizer")
                                                                                   or result.get("pricing")),
                             unknown_usage_attempts=stats.get("unknown_usage_attempts", 0), phase_models=phase_models)
    finalization = result.get("finalization") or {}
    tracking = result.get("research_tracking") or {}
    provenance = Counter(str(entry.get("provenance") or "not_stated") for _, entry in fields)
    cited = [str(i) for _, entry in fields for i in (entry.get("evidence_ids") or []) if i is not None]
    evidence = result.get("evidence") or []
    admission = result.get("evidence_admission") or {}
    variant_matches = Counter(str(item.get("variant_match") or "not_recorded") for item in evidence)
    sanity = (result.get("consistency_checks") or {}).get("summary") or {}
    # tail cost: the recovery stage's tokens + its billable searches (no finalizer), when pricing is known
    tail_pricing = pricing if pricing is not None else result.get("pricing")
    if phase_models.get("field_recovery"):
        tail_pricing = {**(tail_pricing or {}), **{k: v for k, v in default_pricing(phase_models["field_recovery"]).items()
                                                   if k in ("input_per_mtok", "output_per_mtok")}}
    tail_cost, _ = run_cost(usage_recovery, {}, int(recovery.get("tail_billable_search_calls",
                                                                recovery.get("tail_search_calls")) or 0),
                            tail_pricing, None, 0)
    tail_resolved = int(recovery.get("tail_fields_resolved") or 0)
    tail_cost_usd = tail_cost["total_usd"] if usage_recovery.get("model_calls") or recovery.get("tail_search_calls") \
        else (0.0 if recovery else None)
    return {
        "record_id": result.get("record_id"),
        "status": result.get("status"),
        "result_source": result.get("result_source", "result.json"),
        "final_output": output is not None,
        "stop_reason": result.get("stop_reason"),
        "finalization_status": finalization.get("status") or ("not_needed" if output is not None else "not_run"),
        "research_model": result.get("research_model") or result.get("model"),
        "finalizer_model": finalization.get("model") or result.get("finalizer_model"),
        "research_steps": result.get("research_steps") or 0,
        "target_fields": len(targets),
        "target_filled": len(target_filled),
        "coverage_pct": round(100 * len(target_filled) / len(targets), 1) if targets else 0.0,
        "filled_by_output": len(filled_by_output),
        "coverage_pct_by_output": round(100 * len(filled_by_output) / len(targets), 1) if targets else 0.0,
        "output_source": result.get("output_source"),
        "final_assembly": result.get("final_assembly"),
        "fields_returned": len(fields),
        "fields_with_value": len(filled_names),
        "extra_fields": len(extra),
        "extra_field_names": extra,
        "evidence_items": len(result.get("evidence", [])),
        "evidence_with_market": sum(1 for item in result.get("evidence", []) if item.get("market")),
        # evidence admission / binding / authority (observational; never accuracy)
        "evidence_rejected": admission.get("rejected", counters.get("evidence_rejected", 0)) or 0,
        "evidence_rejected_by_reason": admission.get("rejected_by_reason") or {},
        "evidence_variant_exact": variant_matches.get("exact", 0),
        "evidence_variant_unclear": variant_matches.get("unclear", 0),
        "evidence_variant_different": variant_matches.get("different", 0),
        "evidence_unbound": variant_matches.get("unbound", 0),
        "evidence_official_source": sum(1 for item in evidence if item.get("source_authority") in OFFICIAL_CLASSES),
        "evidence_aggregator_source": sum(1 for item in evidence if item.get("source_authority") == "aggregator"),
        "consistency_checks_suspicious": sanity.get("suspicious", 0),
        "consistency_checks_consistent": sanity.get("consistent", 0),
        "fields_israel_direct": provenance.get("israel_direct", 0),
        "fields_foreign_direct": provenance.get("foreign_direct", 0),
        "fields_inferred": provenance.get("inferred", 0),
        "fields_unresolved": provenance.get("unresolved", 0),
        "fields_provenance_not_stated": provenance.get("not_stated", 0),
        "cited_evidence_ids": len(cited),
        "cited_ids_not_in_evidence": sum(1 for i in cited if i not in stored_ids),
        "unique_sources": len(urls),
        "unique_domains": len({_domain(u) for u in urls if u}),
        "documents_opened": len(result.get("documents", [])),
        "tool_calls": sum(by_tool.values()),
        "tool_calls_by_name": dict(by_tool),
        "tool_errors": sum(1 for call in executed if call.get("error")),
        "duplicate_evidence_suppressed": counters.get("duplicate_evidence_suppressed", 0),
        "field_recovery_early_resolutions": recovery.get("early_resolution_count",
                                                         recovery.get("turns_saved_by_early_resolution")) or 0,
        "field_recovery_turn_budget_skipped_by_early_resolution":
            recovery.get("turn_budget_skipped_by_early_resolution") or 0,
        # deprecated alias (it always counted early-resolution events, not turns)
        "field_recovery_turns_saved_by_early_resolution": recovery.get("turns_saved_by_early_resolution") or 0,
        "recovery_prior_excerpt_items": recovery.get("prior_excerpt_items") or 0,
        "recovery_prior_excerpt_chars": recovery.get("prior_excerpt_chars") or 0,
        "recovery_attempts_with_prior_excerpts": recovery.get("attempts_with_prior_excerpts") or 0,
        "recovery_document_rereads_after_prior_excerpt": recovery.get("document_rereads_after_prior_excerpt") or 0,
        "duplicate_calls_suppressed": tracking.get("duplicate_calls_suppressed",
                                                   counters.get("duplicate_calls_suppressed", 0)),
        "duplicate_searches_suppressed": tracking.get("duplicate_searches_suppressed", 0),
        "duplicate_fetches_suppressed": tracking.get("duplicate_fetches_suppressed", 0),
        "duplicate_inspections_suppressed": tracking.get("duplicate_inspections_suppressed", 0),
        "recovery_operations_with_new_material":
            (tracking.get("operations_with_new_material") or {}).get("field_recovery", 0),
        "recovery_operations_without_new_material":
            (tracking.get("operations_without_new_material") or {}).get("field_recovery", 0),
        "document_cache_hits": counters.get("cache_hits", 0),
        "document_cache_misses": counters.get("cache_misses", 0),
        "search_cache_hits": counters.get("search_cache_hits", 0),
        "search_api_calls": search_api_calls,
        "api_errors": counters.get("api_errors", 0),
        "conflicts_reported": len(conflicts) if isinstance(conflicts, list) else 0,
        "additional_findings": len(findings) if isinstance(findings, list) else 0,
        "level3_topics": len(level3_topics),
        "duration_s": result.get("duration_s") or 0,
        "model_calls": usage.get("model_calls", 0),
        "research_model_calls": usage_research.get("model_calls", 0),
        "field_recovery_model_calls": usage_recovery.get("model_calls", 0),
        "requested_fields": len(targets),
        "fields_failed_primary": len(recovery.get("queue") or []),
        "fields_retried": len(recovery.get("fields_retried") or []),
        "fields_recovered": len(recovery.get("fields_recovered") or []),
        "fields_still_failed": len(recovery.get("fields_still_failed") or []),
        "field_retry_attempts": recovery.get("attempt_count") or 0,
        "field_recovery_turns_used": recovery.get("field_recovery_turns_used", recovery.get("turns")) or 0,
        "fields_not_attempted_due_to_budget": len(recovery.get("fields_not_attempted_due_to_budget") or []),
        "fields_conflicting_final": (recovery.get("final_states") or {}).get("conflicting", 0),
        "fields_resolved_directly_by_recovery": len(recovery.get("fields_resolved_directly") or []),
        "fields_resolved_indirectly_by_other_recovery": len(recovery.get("fields_resolved_indirectly") or {}),
        "finalizer_model_calls": usage_finalizer.get("model_calls", 0),
        "primary_model_calls": usage_research.get("model_calls", 0),
        "document_sweep_model_calls": usage_sweep.get("model_calls", 0),
        # primary research = source acquisition (src/acquisition.py; scheduling telemetry, never accuracy)
        "primary_research_turns": primary.get("turns", result.get("research_steps") or 0),
        "primary_research_documents_added": primary.get("documents_added", 0),
        "primary_research_candidate_fields": primary.get("candidate_fields", 0),
        "primary_research_stop_reason": primary.get("stop_reason"),
        "primary_research_no_artifact_turns": primary.get("no_artifact_turns", 0),
        # the fail-safe minimum acquisition base (scheduling only)
        "primary_research_minimum_acquisition_met": primary.get("minimum_acquisition_met"),
        "primary_research_stop_deferred_count": primary.get("stop_deferred_count", 0),
        "primary_research_under_acquired_turns": primary.get("under_acquired_turns", 0),
        "primary_research_extended_turns": primary.get("extended_turns", 0),
        "primary_research_scoped_coverage_pct": primary.get("scoped_coverage_pct"),
        # adaptive document sweep (src/document_sweep.py)
        "document_sweep_calls": sweep.get("document_sweep_calls", usage_sweep.get("model_calls", 0)),
        "document_sweep_chunks": sweep.get("document_sweep_chunks", 0),
        "document_sweep_packet_chars": sweep.get("document_sweep_packet_chars", 0),
        "document_sweep_estimated_input_tokens": sweep.get("document_sweep_estimated_input_tokens", 0),
        "document_sweep_fields": sweep.get("document_sweep_fields", 0),
        "document_sweep_candidates": sweep.get("document_sweep_candidates", sweep.get("candidates_presented", 0)),
        "document_sweep_documents": sweep.get("document_sweep_documents", 0),
        "document_sweep_latency_ms": sweep.get("document_sweep_latency_ms", usage_sweep.get("model_latency_ms", 0)),
        "document_sweep_timeouts": sweep.get("document_sweep_timeouts", 0),
        "document_sweep_input_tokens": sweep.get("document_sweep_input_tokens", usage_sweep.get("prompt_tokens", 0)),
        "document_sweep_output_tokens": sweep.get("document_sweep_output_tokens",
                                                  usage_sweep.get("completion_tokens", 0)),
        "documents_harvested": layered.get("documents_harvested", 0),
        "candidate_count_total": layered.get("candidate_count_total", 0),
        "candidate_fields_total": layered.get("candidate_fields_total", 0),
        "candidate_field_coverage_pct": layered.get("candidate_field_coverage_pct", 0.0),
        "candidate_cache_hits": layered.get("candidate_cache_hits", 0),
        "candidate_cache_misses": layered.get("candidate_cache_misses", 0),
        "document_sweep_fields_promoted_to_evidence": layered.get("document_sweep_fields_promoted_to_evidence", 0),
        "document_sweep_evidence_promoted": layered.get("document_sweep_evidence_promoted", 0),
        "document_sweep_fields_resolved": layered.get("document_sweep_fields_resolved", 0),
        "document_sweep_deterministic_misses_found": layered.get("document_sweep_deterministic_misses_found", 0),
        "fields_unresolved_before_harvest": layered.get("fields_unresolved_before_harvest") or 0,
        "fields_unresolved_after_harvest_review": layered.get("fields_unresolved_after_harvest_review") or 0,
        "fields_entering_web_recovery": layered.get("fields_entering_web_recovery", len(recovery.get("queue") or [])),
        "recovery_fields_given_first_attempt": recovery.get("recovery_fields_given_first_attempt") or 0,
        "recovery_fields_never_attempted": recovery.get("recovery_fields_never_attempted") or 0,
        "recovery_second_attempts_started": recovery.get("recovery_second_attempts_started") or 0,
        "recovery_unique_fields_touched": recovery.get("recovery_unique_fields_touched") or 0,
        "recovery_unique_fields_resolved": recovery.get("recovery_unique_fields_resolved") or 0,
        "recovery_resolution_per_turn": recovery.get("recovery_resolution_per_turn"),
        "fields_never_attempted_due_to_budget": len(recovery.get("fields_never_attempted_due_to_budget") or []),
        "tool_calls_blocked": sum(1 for call in result.get("tool_calls", []) if call.get("blocked")),
        # tail recovery efficiency (legacy and cluster modes; observational, never accuracy)
        "recovery_mode": recovery.get("mode"),
        "tail_fields_at_start": recovery.get("tail_fields_at_start") or 0,
        "tail_fields_resolved": tail_resolved,
        "tail_fields_remaining": recovery.get("tail_fields_remaining") or 0,
        "tail_model_calls": recovery.get("tail_model_calls") or 0,
        "tail_search_calls": recovery.get("tail_search_calls") or 0,
        "tail_billable_search_calls": recovery.get("tail_billable_search_calls") or 0,
        "cluster_attempts": recovery.get("cluster_attempts") or 0,
        "fields_resolved_by_cluster": sum(len(v) for v in (recovery.get("fields_resolved_by_cluster") or {}).values()),
        "no_novelty_stops": recovery.get("no_novelty_stops") or 0,
        "budget_extensions": recovery.get("budget_extensions") or 0,
        "search_budget_refusals": recovery.get("search_budget_refusals") or 0,
        "conflicts_normalized_without_search": recovery.get("conflicts_normalized_without_search") or 0,
        "portable_facts_accepted": recovery.get("portable_facts_accepted") or 0,
        "portable_facts_rejected": recovery.get("portable_facts_rejected") or 0,
        "fields_resolved_per_tail_turn": recovery.get("fields_resolved_per_tail_turn"),
        "fields_resolved_per_tail_search": recovery.get("fields_resolved_per_tail_search"),
        # scale economics: what related variants reuse (observational; savings are claimed only from benchmarks)
        "verified_fact_cache_hits": counters.get("verified_fact_cache_hits", 0),
        "verified_facts_not_readmitted": sum(((result.get("fact_reuse") or {}).get("not_readmitted") or {}).values()),
        "verified_facts_recorded": result.get("verified_facts_recorded") or 0,
        "negative_route_cache_hits": recovery.get("negative_route_cache_hits") or 0,      # equivalent routes refused
        "negative_route_fields_shown": recovery.get("negative_route_fields_shown") or 0,
        "training_feedback_examples": (result.get("training_feedback") or {}).get("examples", 0),
        "tail_cost_usd": tail_cost_usd,
        "cost_per_tail_field_resolved": round(tail_cost_usd / tail_resolved, 6)
        if tail_cost_usd is not None and tail_resolved else None,
        "finalizer_input_chars": finalization.get("finalizer_input_chars") or 0,
        "finalizer_prompt_tokens": usage_finalizer.get("prompt_tokens", 0),
        "finalizer_completion_tokens": usage_finalizer.get("completion_tokens", 0),
        "finalizer_latency_s": round((finalization.get("latency_ms") or 0) / 1000, 2),
        "api_attempts": stats.get("api_attempts", 0),
        "chat_attempts": stats.get("chat_attempts", 0),
        "search_attempts": stats.get("search_attempts", 0),
        "timeout_count": stats.get("timeout_count", 0),
        "unknown_usage_attempts": stats.get("unknown_usage_attempts", 0),
        "duplicate_searches": tracking.get("duplicate_searches", 0),
        "duplicate_fetches": tracking.get("duplicate_fetches", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "model_latency_s": round(usage.get("model_latency_ms", 0) / 1000, 2),
        "cost_tokens_usd": cost["tokens_usd"],
        "cost_search_usd": cost["web_search_usd"],
        "cost_usd": cost["total_usd"],
    }


SUM_KEYS = ("target_filled", "filled_by_output", "fields_with_value", "extra_fields", "evidence_items",
            "documents_opened", "tool_calls", "tool_errors", "document_cache_hits", "document_cache_misses",
            "search_cache_hits", "search_api_calls", "api_errors", "conflicts_reported", "additional_findings",
            "duration_s",
            "model_latency_s", "model_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
            "research_steps", "research_model_calls", "field_recovery_model_calls", "fields_failed_primary",
            "fields_retried", "fields_recovered", "fields_still_failed", "field_retry_attempts",
            "fields_resolved_directly_by_recovery", "fields_resolved_indirectly_by_other_recovery",
            "field_recovery_turns_used", "fields_not_attempted_due_to_budget", "fields_conflicting_final",
            "duplicate_evidence_suppressed", "field_recovery_turns_saved_by_early_resolution",
            "field_recovery_early_resolutions", "field_recovery_turn_budget_skipped_by_early_resolution",
            "recovery_prior_excerpt_items", "recovery_prior_excerpt_chars", "recovery_attempts_with_prior_excerpts",
            "recovery_document_rereads_after_prior_excerpt",
            "tail_fields_at_start", "tail_fields_resolved", "tail_fields_remaining", "tail_model_calls",
            "tail_search_calls", "cluster_attempts", "fields_resolved_by_cluster", "no_novelty_stops",
            "budget_extensions", "search_budget_refusals", "conflicts_normalized_without_search",
            "portable_facts_accepted", "portable_facts_rejected", "verified_fact_cache_hits",
            "verified_facts_not_readmitted", "verified_facts_recorded", "negative_route_cache_hits",
            "negative_route_fields_shown",
            "training_feedback_examples",
            "duplicate_calls_suppressed", "duplicate_searches_suppressed", "duplicate_fetches_suppressed",
            "duplicate_inspections_suppressed", "recovery_operations_with_new_material",
            "recovery_operations_without_new_material", "evidence_with_market", "fields_israel_direct",
            "fields_foreign_direct", "fields_inferred", "fields_unresolved", "cited_ids_not_in_evidence", "finalizer_model_calls", "finalizer_input_chars",
            "finalizer_prompt_tokens", "finalizer_completion_tokens", "api_attempts", "chat_attempts",
            "search_attempts", "timeout_count", "unknown_usage_attempts", "duplicate_searches", "duplicate_fetches",
            "primary_model_calls", "document_sweep_model_calls", "primary_research_turns",
            "primary_research_documents_added", "primary_research_no_artifact_turns", "document_sweep_calls",
            "primary_research_stop_deferred_count", "primary_research_under_acquired_turns",
            "primary_research_extended_turns",
            "document_sweep_chunks", "document_sweep_packet_chars", "document_sweep_estimated_input_tokens",
            "document_sweep_fields", "document_sweep_candidates", "document_sweep_latency_ms",
            "document_sweep_timeouts", "document_sweep_input_tokens", "document_sweep_output_tokens", "documents_harvested", "candidate_count_total",
            "candidate_fields_total", "candidate_cache_hits", "candidate_cache_misses",
            "document_sweep_fields_promoted_to_evidence", "document_sweep_evidence_promoted",
            "document_sweep_fields_resolved", "document_sweep_deterministic_misses_found",
            "fields_unresolved_before_harvest", "fields_unresolved_after_harvest_review", "fields_entering_web_recovery",
            "recovery_fields_given_first_attempt", "recovery_fields_never_attempted",
            "recovery_second_attempts_started", "recovery_unique_fields_touched", "recovery_unique_fields_resolved",
            "fields_never_attempted_due_to_budget", "tool_calls_blocked", "evidence_rejected",
            "evidence_variant_exact", "evidence_variant_unclear", "evidence_variant_different", "evidence_unbound",
            "evidence_official_source", "evidence_aggregator_source", "consistency_checks_suspicious",
            "consistency_checks_consistent")


def aggregate(metrics: list[dict]) -> dict:
    if not metrics:
        return {"vehicles": 0}
    n = len(metrics)
    out: dict = {"vehicles": n, "statuses": dict(Counter(m["status"] for m in metrics)),
                 "primary_research_stop_reasons": dict(Counter(str(m.get("primary_research_stop_reason"))
                                                               for m in metrics)),
                 "primary_research_minimum_acquisition_not_met": sum(
                     1 for m in metrics if m.get("primary_research_minimum_acquisition_met") is False)}
    for key in SUM_KEYS:
        total = sum(m.get(key) or 0 for m in metrics)
        out[f"{key}_total"] = round(total, 2)
        out[f"{key}_mean"] = round(total / n, 2)
    out["coverage_pct_mean"] = round(sum(m["coverage_pct"] for m in metrics) / n, 1)
    out["runs_with_final_output"] = sum(1 for m in metrics if m.get("final_output", True))
    out["runs_without_final_output"] = n - out["runs_with_final_output"]
    hits, misses = out["document_cache_hits_total"], out["document_cache_misses_total"]
    out["document_cache_hit_rate_pct"] = round(100 * hits / (hits + misses), 1) if hits + misses else 0.0
    costs = [m["cost_usd"] for m in metrics if m.get("cost_usd") is not None]
    out["cost_usd_total"] = round(sum(costs), 4) if costs else None
    out["cost_complete"] = out.get("unknown_usage_attempts_total", 0) == 0
    return out


class VehicleRunError(RuntimeError):
    """Placeholder for a vehicle whose worker raised an ordinary exception (the batch goes on)."""


def error_result(vehicle: dict, exc: BaseException) -> dict:
    """Result-shaped record for a vehicle whose worker raised instead of returning a result."""
    return {"record_id": vehicle["upstream_record_id"], "ordinal": vehicle.get("ordinal"), "status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}", "output": None, "partial": True,
            "worker_exception": True}


def run_batch(vehicles: list[dict], rows_by_id: dict[str, dict], run_one: Callable[[dict, dict], dict],
              on_start: Callable[[dict], None] | None = None,
              on_done: Callable[[dict, dict], None] | None = None, *, max_workers: int = 1,
              cancel_event: threading.Event | None = None, on_poll: Callable[[], None] | None = None,
              poll_interval_s: float = 0.2, stats: dict | None = None,
              cancel_grace_s: float = 3.0) -> list[dict]:
    """Run the selected vehicles; return their results in benchmark (input) order.

    max_workers=1 and no `on_poll`: the historical sequential loop, unchanged (one shared cache, so
    reuse is measured honestly; a worker exception propagates).

    Otherwise vehicles run concurrently on a thread pool of min(max_workers, vehicles) workers. The
    real request guards are the shared ConcurrencyController pools (per HTTP attempt), not the
    worker count. All callbacks (`on_start`, `on_done`, `on_poll`) run on the CALLING thread, never
    on a worker, so a UI caller can render from them. An ordinary exception in one worker
    becomes an `error` result for that vehicle and never cancels the others. A control-flow
    BaseException raised by a callback (Ctrl+C, a UI stop) cancels the batch: nothing new is
    scheduled, queued vehicles are cancelled, running workers are asked to stop at their next safe
    point (they persist a partial, interrupted result.json in their own folders), and it is re-raised.
    """
    jobs = [(i, v, rows_by_id.get(v["upstream_record_id"])) for i, v in enumerate(vehicles)]
    jobs = [(i, v, row) for i, v, row in jobs if row is not None]
    stats = stats if stats is not None else {}
    stats.update({"vehicles": len(jobs), "peak_vehicle_workers": 0, "worker_errors": 0})
    if max_workers <= 1 and on_poll is None:
        results = []
        for _, vehicle, row in jobs:
            if on_start:
                on_start(vehicle)
            stats["peak_vehicle_workers"] = 1
            result = run_one(vehicle, row)
            results.append(result)
            if on_done:
                on_done(vehicle, result)
        return results

    workers = max(1, min(int(max_workers), len(jobs) or 1))
    stats["max_workers"] = workers
    cancel_event = cancel_event or threading.Event()
    lock = threading.Lock()
    running = {"now": 0}

    def work(vehicle: dict, row: dict) -> dict:
        if cancel_event.is_set():
            raise BatchCancelled("batch cancelled before this vehicle started")
        with lock:
            running["now"] += 1
            stats["peak_vehicle_workers"] = max(stats["peak_vehicle_workers"], running["now"])
        try:
            return run_one(vehicle, row)
        finally:
            with lock:
                running["now"] -= 1

    results: dict[int, dict] = {}
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vehicle")
    futures = {executor.submit(work, vehicle, row): (index, vehicle) for index, vehicle, row in jobs}
    started: set = set()
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=poll_interval_s, return_when=FIRST_COMPLETED)
            for future, (index, vehicle) in futures.items():
                if future not in started and (future.running() or future.done()):
                    started.add(future)
                    if on_start:
                        on_start(vehicle)
            for future in sorted(done, key=lambda f: futures[f][0]):
                index, vehicle = futures[future]
                exc = future.exception()
                if exc is None:
                    result = future.result()
                elif isinstance(exc, Exception):
                    stats["worker_errors"] += 1
                    result = error_result(vehicle, exc)
                else:   # the worker was interrupted (it already persisted its partial result)
                    raise exc
                results[index] = result
                if on_done:
                    on_done(vehicle, result)
            if on_poll:
                on_poll()
    except BaseException:
        cancel_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        still = [f for f in futures if f.running()]
        if still and cancel_grace_s > 0:
            wait(still, timeout=cancel_grace_s)
        stats["cancelled"] = True
        raise
    executor.shutdown(wait=True)
    return [results[i] for i in sorted(results)]


def vehicle_client_factory(settings, controller=None, cancel_event: threading.Event | None = None,
                           session_factory: Callable[[], Any] | None = None) -> Callable[[], Any]:
    """One GLMClient per vehicle worker: shared immutable settings + shared ConcurrencyController,
    private hooks and HTTP session (so one vehicle's API events can never reach another's trace)."""
    from .glm_client import GLMClient

    def make():
        return GLMClient(settings, session=session_factory() if session_factory else None,
                         concurrency=controller, cancel_event=cancel_event)

    return make


def batch_observability(stats: dict, observation, cache_before: dict | None, cache_after: dict | None) -> dict:
    """End-of-batch concurrency/cache metrics for batch.json. Observational only (no pricing)."""
    obs = observation.as_dict() if observation is not None else {}
    before, after = cache_before or {}, cache_after or {}
    delta = {k: (after.get(k) or 0) - (before.get(k) or 0) for k in after}
    return {
        "peak_vehicle_workers": stats.get("peak_vehicle_workers", 0),
        "max_workers": stats.get("max_workers", 1),
        "worker_errors": stats.get("worker_errors", 0),
        "cancelled": bool(stats.get("cancelled")),
        "peak_chat_inflight_by_model": obs.get("peak_chat_inflight_by_model", {}),
        "peak_search_inflight": obs.get("peak_search_inflight", 0),
        "chat_queue_wait_count": obs.get("chat_queue_wait_count", 0),
        "chat_queue_wait_ms": obs.get("chat_queue_wait_ms", 0),
        "search_queue_wait_count": obs.get("search_queue_wait_count", 0),
        "search_queue_wait_ms": obs.get("search_queue_wait_ms", 0),
        "chat_requests": obs.get("chat_requests", 0),
        "search_requests": obs.get("search_requests", 0),
        "cross_vehicle_cache_waits": delta.get("cross_vehicle_cache_waits", 0),
        "cross_vehicle_search_singleflight_reuses": delta.get("cross_vehicle_search_singleflight_reuses", 0),
        "cross_vehicle_document_singleflight_reuses": delta.get("cross_vehicle_document_singleflight_reuses", 0),
        "cross_vehicle_singleflight_reuses": delta.get("cross_vehicle_search_singleflight_reuses", 0)
        + delta.get("cross_vehicle_document_singleflight_reuses", 0),
        "recorded_at": utc_now(),
    }


def start_batch(runs_dir: Path | str, batch_id: str, *, client, agent_cfg: AgentConfig, tool_cfg: ToolConfig,
                pricing: dict, vehicles: list[dict], level15_source: str, level15_note: str, selection: str,
                prompt_version: str, concurrency: dict | None = None) -> dict:
    """Write batch.json with the complete effective configuration (no secrets)."""
    info = {
        "batch_id": batch_id,
        "created_at": utc_now(),
        "model": client.model,
        "research_model": research_model_of(client),
        "finalizer_model": finalizer_model_of(client, agent_cfg),
        "glm_config": effective_glm_config(client, agent_cfg, tool_cfg),
        "prompt_version": prompt_version,
        "search_backend": tool_cfg.search_backend,
        "pricing": pricing,
        "level15_source": level15_source,
        "level15_note": level15_note,
        "selection": selection,
        "record_ids": [v["upstream_record_id"] for v in vehicles],
        "agent_config": {k: v for k, v in agent_cfg.__dict__.items()},
        "tool_config": dict(tool_cfg.__dict__),
    }
    if concurrency is not None:
        info["concurrency"] = concurrency
    write_batch(runs_dir, batch_id, info)
    return info


def research_one(vehicle: dict, row: dict, *, client, cache, runs_dir: Path | str, batch_id: str,
                 agent_cfg: AgentConfig, tool_cfg: ToolConfig, pricing: dict, level15_source: str,
                 listener: Callable[[str, dict], None] | None = None, session=None,
                 cancel_event: threading.Event | None = None) -> dict:
    """Research exactly one vehicle, write result.json, and return the result. Never moves on by itself.

    result.json is written on every exit path, including research failures,
    finalization failures and KeyboardInterrupt (which is re-raised afterwards).
    """
    log = RunLog(runs_dir, batch_id, vehicle["upstream_record_id"], listener=listener)

    def persist(result: dict) -> None:
        result["level15_source"] = level15_source
        result["metrics"] = compute_metrics(result, vehicle, cache)
        log.write_result(result)

    try:
        return run_vehicle(row, build_level15_payload(row), client=client, cache=cache, run_log=log,
                           config=agent_cfg, tool_config=tool_cfg, vehicle_meta=vehicle, batch_id=batch_id,
                           ordinal=vehicle.get("ordinal"), session=session, pricing=pricing, persist=persist,
                           cancel_event=cancel_event)
    finally:
        # Observational diagnostics (src/diagnostics.py), rebuilt from events.jsonl on every exit path; never raises.
        from .diagnostics import write_vehicle_diagnostics

        diag = write_vehicle_diagnostics(log.dir, run_id=batch_id)
        if diag:
            from .server_logging import get_logger

            get_logger("diagnostics").info("run %s vehicle %s diagnostics:\n%s", batch_id,
                                           vehicle["upstream_record_id"], diag["summary_text"])

"""Benchmark v1: fixed sample, selection, batch execution and observational metrics.

Metrics count what happened (fields filled, sources, tool calls, cache reuse,
time, tokens, cost). None of them is a truth score.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .agent import AgentConfig, effective_glm_config, finalizer_model_of, research_model_of, run_vehicle
from .db import build_level15_payload
from .pricing import run_cost
from .schemas import LEVEL3_TOPICS, has_value, iter_fields, target_field_names
from .storage.run_log import RunLog, utc_now, write_batch
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
    target_filled = [name for name in targets if name in filled_names]
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
    executed = [call for call in result.get("tool_calls", []) if not call.get("reused")]
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
    cost, _ = run_cost(sum_usage(usage_research, usage_recovery), usage_finalizer, search_api_calls,
                       pricing if pricing is not None else result.get("pricing"),
                       pricing if pricing is not None else (result.get("pricing_finalizer") or result.get("pricing")),
                       stats.get("unknown_usage_attempts", 0))
    finalization = result.get("finalization") or {}
    tracking = result.get("research_tracking") or {}
    provenance = Counter(str(entry.get("provenance") or "not_stated") for _, entry in fields)
    stored_ids = {str(item.get("evidence_id")) for item in result.get("evidence", [])}
    cited = [str(i) for _, entry in fields for i in (entry.get("evidence_ids") or []) if i is not None]
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
        "fields_returned": len(fields),
        "fields_with_value": len(filled_names),
        "extra_fields": len(extra),
        "extra_field_names": extra,
        "evidence_items": len(result.get("evidence", [])),
        "evidence_with_market": sum(1 for item in result.get("evidence", []) if item.get("market")),
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


SUM_KEYS = ("target_filled", "fields_with_value", "extra_fields", "evidence_items", "documents_opened", "tool_calls",
            "tool_errors", "document_cache_hits", "document_cache_misses", "search_cache_hits",
            "search_api_calls", "api_errors", "conflicts_reported", "additional_findings", "duration_s",
            "model_latency_s", "model_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
            "research_steps", "research_model_calls", "field_recovery_model_calls", "fields_failed_primary",
            "fields_retried", "fields_recovered", "fields_still_failed", "field_retry_attempts",
            "fields_resolved_directly_by_recovery", "fields_resolved_indirectly_by_other_recovery",
            "field_recovery_turns_used", "fields_not_attempted_due_to_budget", "fields_conflicting_final",
            "duplicate_evidence_suppressed", "field_recovery_turns_saved_by_early_resolution",
            "recovery_prior_excerpt_items", "recovery_prior_excerpt_chars", "recovery_attempts_with_prior_excerpts",
            "recovery_document_rereads_after_prior_excerpt",
            "duplicate_calls_suppressed", "duplicate_searches_suppressed", "duplicate_fetches_suppressed",
            "duplicate_inspections_suppressed", "recovery_operations_with_new_material",
            "recovery_operations_without_new_material", "evidence_with_market", "fields_israel_direct",
            "fields_foreign_direct", "fields_inferred", "fields_unresolved", "cited_ids_not_in_evidence", "finalizer_model_calls", "finalizer_input_chars",
            "finalizer_prompt_tokens", "finalizer_completion_tokens", "api_attempts", "chat_attempts",
            "search_attempts", "timeout_count", "unknown_usage_attempts", "duplicate_searches", "duplicate_fetches")


def aggregate(metrics: list[dict]) -> dict:
    if not metrics:
        return {"vehicles": 0}
    n = len(metrics)
    out: dict = {"vehicles": n, "statuses": dict(Counter(m["status"] for m in metrics))}
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


def run_batch(vehicles: list[dict], rows_by_id: dict[str, dict], run_one: Callable[[dict, dict], dict],
              on_start: Callable[[dict], None] | None = None,
              on_done: Callable[[dict, dict], None] | None = None) -> list[dict]:
    """Run vehicles sequentially (one shared cache, so reuse is measured honestly)."""
    results = []
    for vehicle in vehicles:
        row = rows_by_id.get(vehicle["upstream_record_id"])
        if row is None:
            continue
        if on_start:
            on_start(vehicle)
        result = run_one(vehicle, row)
        results.append(result)
        if on_done:
            on_done(vehicle, result)
    return results


def start_batch(runs_dir: Path | str, batch_id: str, *, client, agent_cfg: AgentConfig, tool_cfg: ToolConfig,
                pricing: dict, vehicles: list[dict], level15_source: str, level15_note: str, selection: str,
                prompt_version: str) -> dict:
    """Write batch.json with the complete effective configuration (no secrets)."""
    info = {
        "batch_id": batch_id,
        "created_at": utc_now(),
        "model": client.model,
        "research_model": research_model_of(client),
        "finalizer_model": finalizer_model_of(client),
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
    write_batch(runs_dir, batch_id, info)
    return info


def research_one(vehicle: dict, row: dict, *, client, cache, runs_dir: Path | str, batch_id: str,
                 agent_cfg: AgentConfig, tool_cfg: ToolConfig, pricing: dict, level15_source: str,
                 listener: Callable[[str, dict], None] | None = None, session=None) -> dict:
    """Research exactly one vehicle, write result.json, and return the result. Never moves on by itself.

    result.json is written on every exit path, including research failures,
    finalization failures and KeyboardInterrupt (which is re-raised afterwards).
    """
    log = RunLog(runs_dir, batch_id, vehicle["upstream_record_id"], listener=listener)

    def persist(result: dict) -> None:
        result["level15_source"] = level15_source
        result["metrics"] = compute_metrics(result, vehicle, cache)
        log.write_result(result)

    return run_vehicle(row, build_level15_payload(row), client=client, cache=cache, run_log=log,
                       config=agent_cfg, tool_config=tool_cfg, vehicle_meta=vehicle, batch_id=batch_id,
                       ordinal=vehicle.get("ordinal"), session=session, pricing=pricing, persist=persist)

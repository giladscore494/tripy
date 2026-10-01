"""Benchmark tab: observational metrics per vehicle, per batch and across batches.

Failed, interrupted and incomplete runs are included: their research work
(steps, tool calls, searches, documents, evidence, tokens, timeouts) is counted;
coverage is simply zero when no final structured output exists.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..benchmark import aggregate, compute_metrics
from ..pricing import UNKNOWN_USAGE_NOTE
from ..storage.cache import DocumentCache
from ..storage.run_loader import load_runs
from ..storage.run_log import list_batches

PER_VEHICLE_COLS = ["vehicle", "status", "result_source", "final_output", "finalization_status", "stop_reason",
                    "coverage_pct", "target_filled", "target_fields", "fields_with_value", "extra_fields",
                    "evidence_items", "evidence_with_market", "fields_israel_direct", "fields_foreign_direct",
                    "fields_inferred", "fields_unresolved", "cited_ids_not_in_evidence", "unique_sources", "unique_domains", "documents_opened", "research_steps",
                    "tool_calls", "tool_errors", "document_cache_hits", "search_cache_hits", "search_api_calls",
                    "duplicate_searches", "duplicate_fetches", "api_errors", "api_attempts", "chat_attempts",
                    "search_attempts", "timeout_count", "unknown_usage_attempts", "conflicts_reported",
                    "additional_findings", "level3_topics", "duration_s", "model_latency_s", "model_calls",
                    "research_model_calls", "finalizer_model_calls", "prompt_tokens", "completion_tokens",
                    "cached_tokens", "finalizer_input_chars", "finalizer_prompt_tokens",
                    "finalizer_completion_tokens", "finalizer_latency_s", "research_model", "finalizer_model",
                    "cost_tokens_usd", "cost_search_usd", "cost_usd"]


def metrics_for(results: list[dict], vehicles_by_id: dict[str, dict], cache: DocumentCache) -> list[dict]:
    """Each run's cost uses the pricing recorded with that run."""
    return [compute_metrics(r, vehicles_by_id.get(r["record_id"]), cache) for r in results]


def render_benchmark(results: list[dict], vehicles_by_id: dict[str, dict], labels: dict[str, str],
                     cache: DocumentCache, runs_dir: Path) -> None:
    st.caption("Observation metrics only: what the model and tools did. Not a correctness score; human "
               "fact-checking happens outside this demo. Runs without a final JSON are counted too.")
    metrics = metrics_for(results, vehicles_by_id, cache)
    if metrics:
        agg = aggregate(metrics)
        cols = st.columns(8)
        cols[0].metric("Vehicles", agg["vehicles"])
        cols[1].metric("Without final JSON", agg["runs_without_final_output"])
        cols[2].metric("Mean coverage", f"{agg['coverage_pct_mean']}%")
        cols[3].metric("Tool calls", agg["tool_calls_total"])
        cols[4].metric("Doc cache hit rate", f"{agg['document_cache_hit_rate_pct']}%")
        cols[5].metric("Known tokens", f"{agg['total_tokens_total']:,}")
        cols[6].metric("Timeouts", agg["timeout_count_total"])
        cols[7].metric("Recorded cost (USD)",
                       "n/a" if agg["cost_usd_total"] is None else f"{agg['cost_usd_total']:.4f}")
        st.caption(UNKNOWN_USAGE_NOTE if not agg["cost_complete"] else "Recorded API cost from returned usage.")
        df = pd.DataFrame([{**m, "vehicle": labels.get(m["record_id"], m["record_id"])} for m in metrics])
        st.dataframe(df[[c for c in PER_VEHICLE_COLS if c in df.columns]], hide_index=True, width="stretch")
        with st.expander("Batch aggregate (raw)"):
            st.json(agg)
        with st.expander("Tool usage by name"):
            usage = pd.DataFrame([{"vehicle": labels.get(m["record_id"], m["record_id"]), **m["tool_calls_by_name"]}
                                  for m in metrics]).fillna(0)
            st.dataframe(usage, hide_index=True, width="stretch")
    else:
        st.caption("No runs in this batch yet.")

    st.subheader("Compare batches")
    rows = []
    for info in list_batches(runs_dir):
        batch_results = load_runs(runs_dir, info["batch_id"], cache=cache)
        if not batch_results:
            continue
        agg = aggregate(metrics_for(batch_results, vehicles_by_id, cache))
        rows.append({
            "batch": info["batch_id"], "model": info.get("research_model") or info.get("model"),
            "finalizer": info.get("finalizer_model") or info.get("model"), "prompt": info.get("prompt_version"),
            "search": info.get("search_backend"), "data": info.get("level15_source"), "vehicles": agg["vehicles"],
            "no_final_json": agg["runs_without_final_output"], "statuses": ", ".join(f"{k}:{v}" for k, v in agg["statuses"].items()),
            "coverage_mean_%": agg["coverage_pct_mean"], "fields_mean": agg["fields_with_value_mean"],
            "extra_fields_mean": agg["extra_fields_mean"], "evidence_mean": agg["evidence_items_mean"],
            "docs_mean": agg["documents_opened_mean"], "steps_mean": agg["research_steps_mean"],
            "tool_calls_mean": agg["tool_calls_mean"], "cache_hit_%": agg["document_cache_hit_rate_pct"],
            "conflicts_total": agg["conflicts_reported_total"], "findings_total": agg["additional_findings_total"],
            "time_mean_s": agg["duration_s_mean"], "searches_total": agg["search_api_calls_total"],
            "timeouts_total": agg["timeout_count_total"], "unknown_usage_total": agg["unknown_usage_attempts_total"],
            "tokens_total": agg["total_tokens_total"], "cost_usd_recorded": agg["cost_usd_total"],
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No batches recorded yet.")

"""Benchmark tab: observational metrics per vehicle, per batch and across batches."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..benchmark import aggregate, compute_metrics
from ..storage.cache import DocumentCache
from ..storage.run_log import list_batches, load_results

PER_VEHICLE_COLS = ["vehicle", "status", "coverage_pct", "target_filled", "target_fields", "fields_with_value",
                    "extra_fields", "evidence_items", "unique_sources", "unique_domains", "documents_opened",
                    "tool_calls", "tool_errors", "document_cache_hits", "search_cache_hits", "conflicts_reported",
                    "additional_findings", "level3_topics", "duration_s", "model_calls", "prompt_tokens",
                    "completion_tokens", "cost_usd"]


def metrics_for(results: list[dict], vehicles_by_id: dict[str, dict], cache: DocumentCache,
                price_in: float, price_out: float) -> list[dict]:
    return [compute_metrics(r, vehicles_by_id.get(r["record_id"]), cache, price_in, price_out) for r in results]


def render_benchmark(results: list[dict], vehicles_by_id: dict[str, dict], labels: dict[str, str],
                     cache: DocumentCache, runs_dir: Path, price_in: float, price_out: float) -> None:
    st.caption("Observation metrics only: what the model and tools did. Not a correctness score; human "
               "fact-checking happens outside this demo.")
    metrics = metrics_for(results, vehicles_by_id, cache, price_in, price_out)
    if metrics:
        agg = aggregate(metrics)
        cols = st.columns(6)
        cols[0].metric("Vehicles", agg["vehicles"])
        cols[1].metric("Mean coverage", f"{agg['coverage_pct_mean']}%")
        cols[2].metric("Tool calls", agg["tool_calls_total"])
        cols[3].metric("Doc cache hit rate", f"{agg['document_cache_hit_rate_pct']}%")
        cols[4].metric("Tokens", f"{agg['total_tokens_total']:,}")
        cols[5].metric("Cost (USD)", "n/a" if agg["cost_usd_total"] is None else f"{agg['cost_usd_total']:.4f}")
        df = pd.DataFrame([{**m, "vehicle": labels.get(m["record_id"], m["record_id"])} for m in metrics])
        st.dataframe(df[PER_VEHICLE_COLS], hide_index=True, width="stretch")
        with st.expander("Batch aggregate (raw)"):
            st.json(agg)
        with st.expander("Tool usage by name"):
            usage = pd.DataFrame([{"vehicle": labels.get(m["record_id"], m["record_id"]), **m["tool_calls_by_name"]}
                                  for m in metrics]).fillna(0)
            st.dataframe(usage, hide_index=True, width="stretch")
    else:
        st.caption("No results in this batch yet.")

    st.subheader("Compare batches")
    rows = []
    for info in list_batches(runs_dir):
        batch_results = load_results(runs_dir, info["batch_id"])
        if not batch_results:
            continue
        agg = aggregate(metrics_for(batch_results, vehicles_by_id, cache, price_in, price_out))
        rows.append({
            "batch": info["batch_id"], "model": info.get("model"), "prompt": info.get("prompt_version"),
            "search": info.get("search_backend"), "data": info.get("level15_source"), "vehicles": agg["vehicles"],
            "coverage_mean_%": agg["coverage_pct_mean"], "fields_mean": agg["fields_with_value_mean"],
            "extra_fields_mean": agg["extra_fields_mean"], "evidence_mean": agg["evidence_items_mean"],
            "docs_mean": agg["documents_opened_mean"], "tool_calls_mean": agg["tool_calls_mean"],
            "cache_hit_%": agg["document_cache_hit_rate_pct"], "conflicts_total": agg["conflicts_reported_total"],
            "findings_total": agg["additional_findings_total"], "time_mean_s": agg["duration_s_mean"],
            "tokens_total": agg["total_tokens_total"], "cost_usd": agg["cost_usd_total"],
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No batches recorded yet.")

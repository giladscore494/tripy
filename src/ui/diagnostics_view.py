"""Concise acquisition / document-sweep diagnostics for the Technical details area (Streamlit).

Live blocks come from the pipeline view (events replayed incrementally); the detailed per-turn / per-call tables come
from src/diagnostics.py (diagnostics.json when the run wrote it, else built in memory from events.jsonl; nothing is
written into a run folder from here).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pandas as pd
import streamlit as st

from .. import diagnostics as diag_mod
from ..storage.run_log import read_events
from . import dashboard as ui

_CACHE: dict[str, tuple[tuple, dict]] = {}
_LOCK = threading.Lock()


def diagnostics_for(run_dir: Path) -> dict | None:
    """diagnostics.json if present (a finished run); otherwise built in memory from events (cached by file size)."""
    events_path = run_dir / "events.jsonl"
    try:
        stamp = (events_path.stat().st_size, (run_dir / diag_mod.DIAGNOSTICS_FILE).stat().st_mtime_ns
                 if (run_dir / diag_mod.DIAGNOSTICS_FILE).exists() else 0)
    except OSError:
        return None
    key = str(run_dir)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    data = diag_mod.load_vehicle_diagnostics(run_dir, rebuild_if_missing=False)
    if data is None or not data.get("complete"):
        events = read_events(events_path)
        data = diag_mod.vehicle_diagnostics(events, run_id=run_dir.parent.name, record_id=run_dir.name) if events else None
    if data is not None:
        with _LOCK:
            _CACHE[key] = (stamp, data)
    return data


def _frac(a, b):
    return f"{a} / {b}" if a is not None and b else (a if a is not None else None)


def _search_text(acq: dict, live: dict):
    """Search tool calls, with billable provider requests when they differ (cache hits are free)."""
    calls, billable = acq.get("search_calls"), acq.get("billable_search_requests")
    if calls is None:
        return live.get("searches")
    return f"{calls} ({billable} billable)" if billable is not None and billable != calls else calls


def _official_pairs(acq_live: dict, acq: dict) -> list[tuple]:
    """Newer runs record fetched official documents and discovered official URLs separately; an older run's single
    number (fetched + search-seen) keeps its old label and is never shown as "fetched"."""
    official = acq_live.get("official_sources", acq.get("official_documents"))
    discovered = acq_live.get("official_urls_discovered", acq.get("official_urls_discovered"))
    if discovered is None:
        return [("Official", official)]
    return [("Official docs (fetched)", official), ("Official URLs (discovered)", discovered)]


def render_brief(view: dict, diag: dict | None) -> None:
    """The two concise blocks: Source acquisition and Document sweep."""
    acq_live, sweep_live = view.get("acquisition") or {}, view.get("sweep") or {}
    acq = (diag or {}).get("acquisition", {}).get("summary") or {}
    sweep = (diag or {}).get("document_sweep", {}).get("summary") or {}
    applicable = view["counters"].get("applicable_fields") or acq.get("applicable_fields")
    turns = acq_live.get("research_turns") or acq.get("turns")
    st.markdown('<p class="tripy-kicker">Source acquisition</p>', unsafe_allow_html=True)
    pairs = [("Research turn", _frac(turns, acq_live.get("max_turns") or acq.get("max_turns"))),
             ("Search calls", _search_text(acq, acq_live)),
             ("Useful docs", acq_live.get("useful_documents", acq.get("useful_documents"))),
             *_official_pairs(acq_live, acq),
             ("Target market", acq_live.get("target_market_documents", acq.get("target_market_documents"))),
             ("Candidate fields", _frac(acq_live.get("candidate_fields", acq_live.get("fields_with_candidates",
                                                                                   acq.get("candidate_fields"))),
                                        applicable)),
             ("Scoped coverage", f"{acq_live.get('scoped_coverage_pct', acq.get('final_scoped_coverage_pct'))}%"
              if acq_live.get("scoped_coverage_pct", acq.get("final_scoped_coverage_pct")) is not None else None)]
    st.markdown(ui.facts_html(pairs), unsafe_allow_html=True)
    if not sweep and not sweep_live:
        return
    st.markdown('<p class="tripy-kicker">Document sweep</p>', unsafe_allow_html=True)
    if sweep.get("skipped"):
        st.caption(f"Skipped: {sweep['skipped']}")
        return
    latency = sweep.get("latency_ms") or sweep_live.get("document_sweep_latency_ms")
    pairs = [("Fields entering", sweep.get("fields_entered", sweep_live.get("fields_entering"))),
             ("Fields resolved", sweep.get("fields_resolved", sweep_live.get("fields_resolved"))),
             ("Model calls", sweep.get("model_calls", sweep_live.get("document_sweep_calls"))),
             ("Latency", f"{round(latency / 1000, 1)} s" if latency else None),
             ("Deterministic misses recovered", sweep.get("deterministic_harvest_misses_recovered")),
             ("Timeouts", sweep.get("timeouts", sweep_live.get("document_sweep_timeouts")))]
    st.markdown(ui.facts_html(pairs), unsafe_allow_html=True)


def _turn_rows(diag: dict) -> list[dict]:
    rows = []
    for t in diag["acquisition"]["turns"]:
        before, after, delta = t.get("state_before") or {}, t.get("state_after") or {}, t.get("delta") or {}
        model = t.get("model") or {}
        rows.append({"turn": t["turn_number"], "model": model.get("configured_model"),
                     "resolved model": model.get("resolved_model"), "latency s":
                         round(model["latency_ms"] / 1000, 1) if model.get("latency_ms") else None,
                     "in tok": model.get("input_tokens"), "out tok": model.get("output_tokens"),
                     "searches": t["search"]["calls"], "queries": " | ".join(t["search"]["queries"]),
                     "results": t["search"]["results_returned"], "repeat q": t["search"]["repeated_queries"],
                     "fetch ok/try": f"{t['fetch']['succeeded']}/{t['fetch']['attempted']}",
                     "fetch failures": ", ".join(f"{k}:{v}" for k, v in t["fetch"]["failure_categories"].items()),
                     "useful": f"{before.get('useful_documents')}→{after.get('useful_documents')}" if after else None,
                     "official": f"{before.get('official_documents')}→{after.get('official_documents')}" if after else None,
                     "official discovered": f"{before.get('official_urls_discovered')}→"
                                            f"{after.get('official_urls_discovered')}"
                     if "official_urls_discovered" in after else None,
                     "target mkt": f"{before.get('target_market_documents')}→{after.get('target_market_documents')}"
                     if after else None,
                     "cand fields": f"{before.get('candidate_fields')}→{after.get('candidate_fields')}" if after else None,
                     "scoped %": f"{before.get('scoped_coverage_pct')}→{after.get('scoped_coverage_pct')}" if after else None,
                     "gain": delta.get("scoped_coverage_gain"), "artifacts": ", ".join(t.get("artifacts") or []),
                     "streak": t.get("no_artifact_streak"), "base met": t.get("base_gate_met"),
                     "stop wanted": t.get("stop_wanted"), "deferred": t.get("stop_deferred_by_base_gate"),
                     "extended": t.get("extended_beyond_normal_budget"), "stop": t.get("stop_reason")})
    return rows


def render_detailed(diag: dict | None) -> None:
    with st.expander("Detailed diagnostics"):
        if not diag:
            st.caption("No diagnostics yet (the run has not logged any events).")
            return
        st.caption("Observational telemetry of source acquisition and the document sweep. "
                   f"Schema {diag.get('schema')} · models configured: "
                   + ", ".join(f"{k} {v}" for k, v in (diag.get("configured_models") or {}).items() if v))
        tabs = st.tabs(["Summary", "Acquisition turns", "Sweep calls", "Sweep fields", "Raw"])
        with tabs[0]:
            st.code(diag.get("summary_text") or "—", language=None, wrap_lines=True)
        with tabs[1]:
            rows = _turn_rows(diag)
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            else:
                st.caption("No primary-research turns recorded.")
        calls = diag["document_sweep"]["calls"]
        with tabs[2]:
            if calls:
                st.dataframe(pd.DataFrame([{
                    "call": c["sweep_call_number"], "model": (c.get("model") or {}).get("configured_model"),
                    "resolved model": (c.get("model") or {}).get("resolved_model"),
                    "model calls": c["model_call_count"], "latency s": round(c["latency_ms"] / 1000, 1)
                    if c.get("latency_ms") else None, "timeout": c["timeout"], "retries": c["retries"],
                    "in tok": c["input_tokens"], "out tok": c["output_tokens"],
                    "fields in": c["fields_entering"], "resolved": c["fields_resolved"],
                    "packet chars": c["input"]["packet_chars"], "docs": c["input"]["cached_documents_available"],
                    "candidates": c["input"]["candidates_supplied"], "evidence +": c["evidence_admitted"],
                    "evidence −": c["evidence_rejected"], "malformed": c["malformed_field_outputs"],
                    "classification": ", ".join(f"{k}:{v}" for k, v in c["deterministic_classification"].items()),
                    "ok": c["call_success"]} for c in calls]), hide_index=True, width="stretch")
            else:
                st.caption("No document-sweep call was made.")
        with tabs[3]:
            fields = [{"call": c["sweep_call_number"], **{k: (", ".join(map(str, v)) if isinstance(v, list) and k ==
                                                              "classifications" else v)
                                                          for k, v in f.items() if k not in ("evidence_admitted",
                                                                                             "evidence_rejected")},
                       "evidence +": len(f["evidence_admitted"]), "evidence −": len(f["evidence_rejected"]),
                       "rejection reasons": "; ".join(",".join(r.get("reasons") or []) for r in f["evidence_rejected"])}
                      for c in calls for f in c["fields"]]
            if fields:
                st.dataframe(pd.DataFrame(fields), hide_index=True, width="stretch")
            else:
                st.caption("No field entered the document sweep.")
        with tabs[4]:
            st.json(diag, expanded=False)


def render_benchmark_export(runs_dir: Path, run_ids: list[str], labels: dict[str, str],
                            series: list[dict] | None = None) -> None:
    """Sidebar: aggregate acquisition / sweep diagnostics over chosen runs and download them (read-only); the
    benchmark files an A/B series wrote over exactly its own runs."""
    with st.expander("Benchmark diagnostics"):
        _render_series_benchmarks(series or [])
        if not run_ids:
            st.caption("No runs yet.")
            return
        chosen = st.multiselect("Runs", run_ids, default=run_ids[:15], format_func=lambda r: labels.get(r, r),
                                key="bench_runs")
        if not chosen:
            return
        diags = [d for d in (diagnostics_for(p) for p in diag_mod.vehicle_dirs(runs_dir, chosen)) if d]
        result = diag_mod.aggregate(diags)
        a, s = result["acquisition"], result["document_sweep"]
        st.caption(f"{result['vehicles']} vehicle run(s) · mean turns {a['turns']['mean']} · mean searches "
                   f"{a['searches_per_vehicle']['mean']} · sweep resolution rate {s['resolution_rate']}")
        import csv as _csv
        import io as _io
        import json as _json
        buf = _io.StringIO()
        if result["per_vehicle"]:
            writer = _csv.DictWriter(buf, fieldnames=list(result["per_vehicle"][0]))
            writer.writeheader()
            writer.writerows(result["per_vehicle"])
        st.download_button("Download benchmark.json", _json.dumps(result, ensure_ascii=False, indent=1, default=str),
                           file_name="tripy_benchmark.json", mime="application/json", width="stretch")
        st.download_button("Download per_vehicle.csv", buf.getvalue(), file_name="tripy_per_vehicle.csv",
                           mime="text/csv", width="stretch", disabled=not result["per_vehicle"])
        gaps = diag_mod.parser_gap_rows(diags)
        st.download_button("Download parser_gaps.jsonl", "".join(_json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                                                 for r in gaps),
                           file_name="tripy_parser_gaps.jsonl", mime="application/x-ndjson", width="stretch",
                           disabled=not gaps)


def _render_series_benchmarks(series: list[dict]) -> None:
    """The benchmark.json / parser_gaps.jsonl each finished A/B series wrote (runs/_series/<id>/)."""
    done = [s for s in series if (s.get("benchmark") or {}).get("dir")]
    if not done:
        return
    st.markdown("**A/B series**")
    for item in done:
        bench = item["benchmark"]
        st.caption(f"{item.get('label')} · {item.get('status')} · {len(bench.get('run_ids') or [])} run(s)"
                   + ("" if bench.get("complete") else " · partial"))
        folder = Path(bench["dir"])
        for name, mime in (("benchmark.json", "application/json"),
                           (diag_mod.PARSER_GAPS_FILE, "application/x-ndjson")):
            path = folder / name
            if path.is_file():
                st.download_button(f"Download {name}", path.read_bytes(), file_name=f"{item['series_id']}_{name}",
                                   mime=mime, width="stretch", key=f"dl_{item['series_id']}_{name}")
    st.divider()

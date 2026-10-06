"""Framework-neutral view models of a run's technical views: plain dicts / lists the HTTP API serves and the React
workspace renders. Every function here is a data-only port of what the former dashboard views computed (no rendering,
no I/O beyond the loaders they call, no research logic): the same fields, the same labels, the same rules.

    result views     status_notices, no_output_message, human_view, partial_research, target_status_lines,
                     field_recovery_view, config_and_cost, api_attempts, candidates_layered, output_source_caption
    run summaries    resolved_text, status_panel_rows
    diagnostics      brief_pairs (acquisition / sweep), turn_rows, sweep_call_rows, sweep_field_rows, binding_rows
    benchmark        PER_VEHICLE_COLS, metrics_for, batch_rows (the Benchmark tab and "Compare batches")
"""

from __future__ import annotations

import json
from pathlib import Path

from ..schemas import iter_fields
from ..storage import trace
from ..storage.run_log import load_events
from . import labels_he as he

FAILED_STATUSES = ("research_failed", "finalization_failed", "interrupted", "incomplete", "error")
NO_OUTPUT_REASON = {
    "finalization_failed": "finalization failed",
    "interrupted": "the run was interrupted",
    "incomplete": "the run ended without a result.json (finalization did not complete)",
    "research_failed": "research failed",
    "error": "the run failed",
    "completed_unparsed": "the final answer could not be parsed as JSON",
    "finalization_pending": "research finished and was checkpointed, but the finalizer did not complete",
}
EVIDENCE_COLUMNS = ("evidence_id", "field", "value", "unit", "variant_match", "binding_level", "binding_veto", "market",
                    "market_basis", "source_authority", "source_domain", "entailment", "condition", "valid_as_of",
                    "quote", "document_id", "model_variant_claim", "model_market_claim", "note")
DOCUMENT_COLUMNS = ("document_id", "kind", "url", "final_url", "status", "content_type", "extraction_path", "bytes",
                    "text_chars", "pages", "truncated", "fetched_at")
HUMAN_KNOWN_KEYS = {"vehicle_id", "summary", "fields", "conflicts", "additional_findings", "level3", "research_trace",
                    "variant_identity", "provenance_summary"}
PROVENANCE_LABELS = {"israeli_market_values": "Israeli-market values", "foreign_market_values": "Foreign-market values",
                     "inferred_variant_mappings": "Inferred variant mappings", "conflicts": "Conflicts",
                     "unresolved_fields": "Unresolved / null"}


def short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def has_research(result: dict) -> bool:
    return bool(result.get("tool_calls") or result.get("evidence") or result.get("documents")
                or (result.get("usage") or {}).get("model_calls"))


def no_output_message(result: dict) -> str:
    reason = NO_OUTPUT_REASON.get(result.get("status"), "no final JSON was produced")
    return f"Not produced — {reason}."


def vehicle_events(runs_dir: Path, result: dict) -> list[dict]:
    return load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", "")) if runs_dir else []


# --- result notices ----------------------------------------------------------------------------------------------------

def status_notices(result: dict) -> list[dict]:
    """The banner lines above a vehicle's technical views: {tone: info|warn, text, detail?}."""
    out = []
    if result.get("status") == "finalization_pending":
        out.append({"tone": "info", "text": he.FINALIZATION_PENDING_MESSAGE_HE,
                    "detail": f"python -m src.cli --finalize-existing --batch-id {result.get('batch_id')} "
                              f"--record-id {result.get('record_id')}"})
    if result.get("interrupted"):
        out.append({"tone": "info", "text": result.get("interruption_message") or (
            "Run interrupted. All completed research/evidence was preserved. The result below is partial.")})
    if result.get("synthesized"):
        facts = [f"status {result.get('status')}", f"started {result.get('started_at')}",
                 f"latest event {result.get('latest_event_at')}",
                 f"last successful step {result.get('last_successful_step')}",
                 f"{result.get('research_steps')} model turn(s)", f"{len(result.get('tool_calls') or [])} tool call(s)"]
        out.append({"tone": "warn", "text": result.get("banner") or "This run did not produce a final result.json.",
                    "detail": " · ".join(facts) + (f"\n{result['end_state']}" if result.get("end_state") else "")})
    if result.get("recovered"):
        rec = result.get("recovery") or {}
        out.append({"tone": "info", "text": f"Recovered from a prior research run: finalized {rec.get('recovered_at')} "
                                            f"with {rec.get('finalizer_model')} (prior status {rec.get('prior_status')}). "
                                            "The web research was not repeated."})
    if result.get("cost_note"):
        out.append({"tone": "info", "text": str(result["cost_note"])})
    return out


def result_notices(result: dict | None, counters: dict, failed: bool) -> list[dict]:
    """The notices above a finished vehicle's result (insufficient evidence, thin sources, finalized from preserved
    research)."""
    if not result or failed:
        return []
    out = []
    if counters.get("applicable_fields") and not counters.get("resolved_fields"):
        out.append({"tone": "info", "text": "Insufficient evidence: no requested field reached an evidence-backed "
                                            "state. The research trace is under Technical."})
    if (result.get("primary_research") or {}).get("stop_reason") == "hard_max_turns_under_acquired":
        out.append({"tone": "warn", "text": "Few usable sources were found for this vehicle; the result may be thin."})
    if result.get("recovered"):
        out.append({"tone": "info", "text": "Finalized from the preserved research (no web research was repeated)."})
    return out


# --- human view --------------------------------------------------------------------------------------------------------

def human_view(output) -> dict:
    """What the model's final JSON object holds beyond the field values: variant identity, per-field alternatives,
    provenance summary, Level 3, other keys and the model's own research trace."""
    if not isinstance(output, dict):
        return {"is_object": False}
    summary = output.get("provenance_summary")
    return {
        "is_object": True,
        "variant_identity": output.get("variant_identity") if isinstance(output.get("variant_identity"), dict) else None,
        "alternatives": [{"field": name, "alternatives": short(entry.get("alternatives"), 200)}
                         for name, entry in iter_fields(output) if entry.get("alternatives")],
        "provenance": [{"label": label, "text": short(summary[key], 600)} for key, label in PROVENANCE_LABELS.items()
                       if isinstance(summary, dict) and summary.get(key)],
        "level3": output.get("level3") or None,
        "other_keys": {k: v for k, v in output.items() if k not in HUMAN_KNOWN_KEYS} or None,
        "research_trace": [short(step, 400) for step in output.get("research_trace") or []],
    }


# --- partial research --------------------------------------------------------------------------------------------------

def target_status_lines(bundle: dict) -> dict[str, list[str]]:
    """Display lines for the three distinct pending-work lists of a research bundle.

    "without stored evidence" is strict (zero evidence records); "still unresolved" is the current
    evaluated state (it may well have evidence); Level 3 topics are listed apart from Level 2 fields.
    """
    states = bundle.get("field_states") or {}
    unresolved = bundle.get("unresolved_targets") or []
    legacy = [t for t in unresolved if str(t).startswith("level3:")]   # bundles written before the split
    explicit = {u.get("field"): u.get("state") for u in bundle.get("unresolved_target_states") or []}
    return {
        "no_evidence": list(bundle.get("targets_without_stored_evidence") or []),
        "unresolved": [f"{name} — {explicit.get(name) or (states.get(name) or {}).get('state', 'unresolved')}"
                       for name in unresolved if not str(name).startswith("level3:")],
        "level3": list(bundle.get("level3_topics_without_evidence") or [t.split(":", 1)[1] for t in legacy]),
    }


def source_urls(result: dict, metas: list[dict]) -> list[str]:
    urls: list[str] = []
    for item in result.get("evidence") or []:
        if item.get("source_url") and item["source_url"] not in urls:
            urls.append(item["source_url"])
    for meta in metas:
        url = meta.get("final_url") or meta.get("url")
        if url and url not in urls:
            urls.append(url)
    return urls


def api_error_rows(result: dict) -> list[dict]:
    return [{k: short(v, 200) for k, v in e.items() if k != "headers"} for e in result.get("api_errors") or []
            if isinstance(e, dict)]


def partial_research(result: dict, events: list[dict], metas: list[dict]) -> dict:
    """Everything the research produced, without inventing structured values."""
    bundle = result.get("research_bundle") or {}
    facts = bundle.get("candidate_facts") or {}
    responses = [r for r in (result.get("model_responses") or trace.model_responses(events))
                 if (r.get("content") or "").strip()]
    return {
        "evidence_count": len(result.get("evidence") or []),
        "candidate_facts": [{"field": f, "values": short([v.get("value") for v in values], 300),
                             "evidence": ", ".join(str(v.get("evidence_id")) for v in values)}
                            for f, values in facts.items()],
        "fields_with_multiple_stored_values": list(bundle.get("fields_with_multiple_stored_values") or []),
        "model_noted_conflicts": [{"source": n.get("source"), "text": short(n.get("text"), 400)}
                                  for n in bundle.get("model_noted_conflicts") or []],
        "response_excerpts": [{"seq": r.get("seq"), "phase": r.get("phase"), "text": short(r.get("content"), 500)}
                              for r in responses[-5:]],
        "last_model_content": result.get("last_model_content") or bundle.get("last_model_content"),
        "urls": source_urls(result, metas)[:100],
        "documents": [{c: m.get(c) for c in DOCUMENT_COLUMNS} for m in metas],
        "target_status": target_status_lines(bundle),
        "error": result.get("error"),
        "api_errors": api_error_rows(result),
        "bundle": bundle or None,
    }


# --- field recovery ----------------------------------------------------------------------------------------------------

def field_recovery_view(result: dict, events: list[dict]) -> dict:
    """Requested-field states after primary research and after targeted retries."""
    recovery = result.get("field_recovery")
    requested = result.get("requested_fields") or {}
    out: dict = {"requested_fields": len(requested), "available": bool(recovery)}
    if not recovery:
        return out
    primary = {f["field"]: f for f in recovery.get("evaluation_primary") or []}
    final = {f["field"]: f for f in recovery.get("evaluation_final") or []}
    attempts: dict[str, int] = {}
    for a in recovery.get("attempts") or []:
        for name in a.get("fields") or [a.get("field")]:     # a cluster attempt covers several fields
            attempts[name] = attempts.get(name, 0) + 1
    current = recovery.get("current_states") or {}
    budget = recovery.get("field_recovery_turn_budget")
    out.update({
        "error": recovery.get("error"),
        "metrics": {"failed_after_primary": len(recovery.get("queue") or []),
                    "retried": len(recovery.get("fields_retried") or []),
                    "recovered": len(recovery.get("fields_recovered") or []),
                    "retry_attempts": recovery.get("attempt_count") or 0},
        "turns": f"{recovery.get('field_recovery_turns_used', recovery.get('turns', 0))}"
                 + (f" of {budget} (remaining {recovery.get('field_recovery_turns_remaining')})" if budget
                    else " (no cap)"),
        "stopped": recovery.get("stopped"),
        "not_attempted_due_to_budget": list(recovery.get("fields_not_attempted_due_to_budget") or []),
        "cut_short_by_budget": recovery.get("field_cut_short_by_budget"),
        "mode": recovery.get("mode"),
        "cluster": {k: recovery.get(k, 0) for k in (
            "cluster_attempts", "tail_fields_at_start", "tail_fields_resolved", "tail_search_calls", "no_novelty_stops",
            "budget_extensions", "conflicts_normalized_without_search", "portable_facts_accepted",
            "portable_facts_rejected")} if recovery.get("mode") == "cluster" else None,
        "triage": [{"field": f, **v} for f, v in (recovery.get("triage") or {}).items()],
        "fields": [{"field": name, "primary_state": (primary.get(name) or {}).get("state"),
                    "retry_attempts": attempts.get(name, 0), "last_attempt_state": (final.get(name) or {}).get("state"),
                    "current_state": current.get(name, (final.get(name) or {}).get("state")),
                    "evidence": ", ".join(str(i) for i in (final.get(name) or primary.get(name) or {})
                                          .get("evidence_ids") or []),
                    "markets": ", ".join((final.get(name) or {}).get("markets") or []),
                    "info": ", ".join((final.get(name) or {}).get("info") or [])}
                   for name in (primary or final)],
        "attempts": [{"field": a.get("field"), "attempt": a.get("attempt"), "before": a.get("state_before"),
                      "after": a.get("state_after"), "turns": a.get("turns"), "early_resolved": a.get("early_resolved"),
                      "mode": a.get("mode"), "stop": a.get("stop"), "searches": a.get("search_provider_calls"),
                      "resolved": ", ".join(a.get("fields_resolved") or []),
                      "prior_excerpts": a.get("prior_excerpt_items"), "prior_excerpt_chars": a.get("prior_excerpt_chars"),
                      "packet_chars": a.get("packet_chars"),
                      "rereads_after_excerpt": a.get("document_rereads_after_prior_excerpt"),
                      "reply": short(a.get("reply") or a.get("reply_text"), 300), "error": a.get("error")}
                     for a in recovery.get("attempts") or []],
        "prior_excerpts_summary": {"items": recovery.get("prior_excerpt_items", 0),
                                   "chars": recovery.get("prior_excerpt_chars", 0),
                                   "attempts_with_prior_excerpts": recovery.get("attempts_with_prior_excerpts", 0)},
        "prior_excerpts": [{"field": e.get("field"), "attempt": e.get("attempt"),
                            "items": e.get("prior_excerpt_items"), "chars": e.get("prior_excerpt_chars"),
                            "excerpts": e["prior_excerpts"]}
                           for e in events if e.get("kind") == "field_recovery_started" and e.get("prior_excerpts")],
    })
    return out


# --- config, cost, API attempts, candidates ----------------------------------------------------------------------------

def config_and_cost(result: dict) -> dict:
    return {
        "api_error": result.get("api_error"),
        "research_model": result.get("research_model") or result.get("model"),
        "finalizer_model": result.get("finalizer_model"), "stop_reason": result.get("stop_reason"),
        "prompt_version": result.get("prompt_version"),
        "finalization": {k: v for k, v in result["finalization"].items() if k not in ("raw_text", "repair_text")}
        if isinstance(result.get("finalization"), dict) else None,
        "effective_config": result.get("effective_config") or result.get("glm_config") or {},
        "usage_known": not (result.get("api_stats") or {}).get("unknown_usage_attempts"),
        "usage": {"usage": result.get("usage"), "usage_research": result.get("usage_research"),
                  "usage_finalizer": result.get("usage_finalizer"),
                  "search_api_calls": result.get("search_api_calls"), "pricing": result.get("pricing"),
                  "pricing_finalizer": result.get("pricing_finalizer"), "cost": result.get("cost"),
                  "cost_details": result.get("cost_details"), "research_tracking": {
                      k: v for k, v in (result.get("research_tracking") or {}).items() if k != "turns"}},
    }


def api_attempts(result: dict) -> dict:
    return {"api_stats": result.get("api_stats") or {}, "api_errors": api_error_rows(result)}


def candidates_layered(result: dict) -> dict:
    """The layered-pipeline candidate metrics recorded in result.json (candidate_summary) and the raw primary research /
    document sweep blocks."""
    return {"candidate_summary": result.get("candidate_summary") or {},
            "primary_research": result.get("primary_research"), "document_sweep": result.get("document_sweep")}


def model_responses(result: dict, events: list[dict]) -> list[dict]:
    return [{"seq": r.get("seq"), "phase": r.get("phase"), "tool_calls": r.get("tool_calls"),
             "prompt_tokens": (r.get("usage") or {}).get("prompt_tokens"),
             "completion_tokens": (r.get("usage") or {}).get("completion_tokens"), "latency_ms": r.get("latency_ms"),
             "content": r.get("content"), "reasoning_content": r.get("reasoning_content")}
            for r in (result.get("model_responses") or trace.model_responses(events))]


def tool_call_rows(result: dict) -> list[dict]:
    return [{**call, "arguments": short(call.get("arguments"), 300)} if isinstance(call, dict) else {"call": call}
            for call in result.get("tool_calls") or []]


def output_source_caption(results: list[dict]) -> str:
    """Where the shown values came from (result.json `output_source`: code = Deterministic Final Assembly)."""
    sources = {r.get("output_source") or ("model" if r.get("output") is not None else None) for r in results
               if r.get("output") is not None}
    if sources == {"code"}:
        return ("Output source: code (deterministic final assembly). Values come only from admitted evidence and the "
                "engine's field states; a model wrote at most the summary text. No reliability pass/fail is applied.")
    if sources == {"model"}:
        return "Output source: model. Values exactly as the model returned them. No reliability pass/fail is applied."
    return ("Output source: mixed (code for deterministic final assembly runs, model for the others; see each run). "
            "No reliability pass/fail is applied.")


# --- run summaries -----------------------------------------------------------------------------------------------------

def resolved_text(record) -> str | None:
    """'26 / 37' from the final report (sum over vehicles), else None."""
    vehicles = ((record.report or {}).get("vehicles") or {}).values()
    pairs = [(v.get("resolved_fields"), v.get("applicable_fields")) for v in vehicles]
    pairs = [(r, a) for r, a in pairs if r is not None and a]
    if not pairs:
        return None
    return f"{sum(r for r, _ in pairs)} / {sum(a for _, a in pairs)}"


def _known(value):
    return "—" if value is None else value


def status_panel_rows(record, views: list[dict]) -> list[tuple]:
    """(label, value) rows of the run's status panel (totals over every vehicle)."""
    from ..runstate.model import STATUS_LABELS
    from ..runstate.pipeline import STAGE_LABELS
    from ..runstate.report import elapsed_s
    from .format import fmt_duration, fmt_time

    # a total is shown only when at least one vehicle has a known value; unknown is "—", never 0
    totals: dict[str, int | None] = {"sources": None, "candidates": None, "model_calls": None, "searches": None}
    resolved, applicable = 0, 0
    for view in views:
        c = view["counters"]
        for key in totals:
            if c.get(key) is not None:
                totals[key] = (totals[key] or 0) + c[key]
        if c.get("applicable_fields"):
            resolved += c.get("resolved_fields") or 0
            applicable += c["applicable_fields"]
    stage = None
    if record.active:
        current = [v["current_stage"] for v in views if v.get("current_stage")]
        stage = STAGE_LABELS.get(current[0]) if current else STATUS_LABELS.get(record.status)
    model = record.request.get("research_model") or "—"
    finalizer = record.request.get("finalizer_model")
    status = "Running" if record.active and record.status not in ("QUEUED", "STARTING") else \
        STATUS_LABELS.get(record.status, record.status)
    rows = [("Run", record.run_id), ("Status", status)]
    if record.active:
        rows.append(("Phase", stage or "—"))
    rows += [("Model", model + (f" · finalizer {finalizer}" if finalizer and finalizer != model
                                                         else "")),
             ("Started", fmt_time(record.started_at or record.created_at)),
             ("Elapsed" if record.active else "Duration", fmt_duration(elapsed_s(record))),
             ("Sources", _known(totals["sources"])), ("Candidates", _known(totals["candidates"])),
             ("Resolved fields", f"{resolved} / {applicable}" if applicable else "—"),
             ("Search calls", _known(totals["searches"])), ("Model calls", _known(totals["model_calls"]))]
    if len(record.record_ids) > 1:
        rows.insert(1, ("Vehicles", len(record.record_ids)))
    return rows


# --- diagnostics -------------------------------------------------------------------------------------------------------

def _frac(a, b):
    return f"{a} / {b}" if a is not None and b else (a if a is not None else None)


def _search_text(acq: dict, live: dict):
    """Search tool calls, with billable provider requests when they differ (cache hits are free)."""
    calls, billable = acq.get("search_calls"), acq.get("billable_search_requests")
    if calls is None:
        return live.get("searches")
    return f"{calls} ({billable} billable)" if billable is not None and billable != calls else calls


def official_pairs(acq_live: dict, acq: dict) -> list[tuple]:
    """Newer runs record fetched official documents and discovered official URLs separately; an older run's single
    number (fetched + search-seen) keeps its old label and is never shown as "fetched"."""
    official = acq_live.get("official_sources", acq.get("official_documents"))
    discovered = acq_live.get("official_urls_discovered", acq.get("official_urls_discovered"))
    if discovered is None:
        return [("Official", official)]
    return [("Official docs (fetched)", official), ("Official URLs (discovered)", discovered)]


def brief_pairs(view: dict, diag: dict | None) -> dict:
    """The two concise blocks: Source acquisition and Document sweep ([label, value] pairs; None = unknown)."""
    acq_live, sweep_live = view.get("acquisition") or {}, view.get("sweep") or {}
    acq = (diag or {}).get("acquisition", {}).get("summary") or {}
    sweep = (diag or {}).get("document_sweep", {}).get("summary") or {}
    applicable = (view.get("counters") or {}).get("applicable_fields") or acq.get("applicable_fields")
    turns = acq_live.get("research_turns") or acq.get("turns")
    coverage = acq_live.get("scoped_coverage_pct", acq.get("final_scoped_coverage_pct"))
    acquisition = [("Research turn", _frac(turns, acq_live.get("max_turns") or acq.get("max_turns"))),
                   ("Search calls", _search_text(acq, acq_live)),
                   ("Useful docs", acq_live.get("useful_documents", acq.get("useful_documents"))),
                   *official_pairs(acq_live, acq),
                   ("Target market", acq_live.get("target_market_documents", acq.get("target_market_documents"))),
                   ("Candidate fields", _frac(acq_live.get("candidate_fields", acq_live.get(
                       "fields_with_candidates", acq.get("candidate_fields"))), applicable)),
                   ("Scoped coverage", f"{coverage}%" if coverage is not None else None)]
    out: dict = {"acquisition": [[k, v] for k, v in acquisition if v is not None], "sweep": None, "sweep_skipped": None}
    if not sweep and not sweep_live:
        return out
    if sweep.get("skipped"):
        out["sweep_skipped"] = sweep["skipped"]
        return out
    latency = sweep.get("latency_ms") or sweep_live.get("document_sweep_latency_ms")
    pairs = [("Fields entering", sweep.get("fields_entered", sweep_live.get("fields_entering"))),
             ("Fields resolved", sweep.get("fields_resolved", sweep_live.get("fields_resolved"))),
             ("Model calls", sweep.get("model_calls", sweep_live.get("document_sweep_calls"))),
             ("Latency", f"{round(latency / 1000, 1)} s" if latency else None),
             ("Deterministic misses recovered", sweep.get("deterministic_harvest_misses_recovered")),
             ("Timeouts", sweep.get("timeouts", sweep_live.get("document_sweep_timeouts")))]
    out["sweep"] = [[k, v] for k, v in pairs if v is not None]
    return out


def turn_rows(diag: dict) -> list[dict]:
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


def sweep_call_rows(diag: dict) -> list[dict]:
    return [{"call": c["sweep_call_number"], "model": (c.get("model") or {}).get("configured_model"),
             "resolved model": (c.get("model") or {}).get("resolved_model"),
             "model calls": c["model_call_count"], "latency s": round(c["latency_ms"] / 1000, 1)
             if c.get("latency_ms") else None, "timeout": c["timeout"], "retries": c["retries"],
             "in tok": c["input_tokens"], "out tok": c["output_tokens"],
             "fields in": c["fields_entering"], "resolved": c["fields_resolved"],
             "packet chars": c["input"]["packet_chars"], "docs": c["input"]["cached_documents_available"],
             "candidates": c["input"]["candidates_supplied"], "evidence +": c["evidence_admitted"],
             "evidence −": c["evidence_rejected"], "malformed": c["malformed_field_outputs"],
             "classification": ", ".join(f"{k}:{v}" for k, v in c["deterministic_classification"].items()),
             "ok": c["call_success"]} for c in diag["document_sweep"]["calls"]]


def sweep_field_rows(diag: dict) -> list[dict]:
    return [{"call": c["sweep_call_number"], **{k: (", ".join(map(str, v)) if isinstance(v, list) and k ==
                                                    "classifications" else v)
                                                for k, v in f.items() if k not in ("evidence_admitted",
                                                                                   "evidence_rejected")},
             "evidence +": len(f["evidence_admitted"]), "evidence −": len(f["evidence_rejected"]),
             "rejection reasons": "; ".join(",".join(r.get("reasons") or []) for r in f["evidence_rejected"])}
            for c in diag["document_sweep"]["calls"] for f in c["fields"]]


def detailed_diagnostics(diag: dict | None) -> dict | None:
    """The Detailed diagnostics block of one vehicle: summary text, acquisition turns, sweep calls / fields, raw."""
    if not diag:
        return None
    return {"schema": diag.get("schema"), "configured_models": diag.get("configured_models") or {},
            "summary_text": diag.get("summary_text"), "turns": turn_rows(diag), "sweep_calls": sweep_call_rows(diag),
            "sweep_fields": sweep_field_rows(diag), "raw": diag}


def _domain(url) -> str:
    from urllib.parse import urlparse

    host = urlparse(str(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def region_label(region: dict | None) -> str:
    """"table:0:col:2 target (AWD 486 כ"ס) catalog 2 -> 1"."""
    if not region:
        return ""
    before, after = region.get("candidates_before") or [], region.get("candidates_after") or []
    parts = [str(region.get("region_id") or ""), str(region.get("status") or "")]
    if region.get("identity_text"):
        parts.append(f"({str(region['identity_text'])[:60]})")
    if before or after:
        parts.append(f"catalog {len(before)} → {len(after)}")
    if region.get("blocked_by"):
        parts.append("blocked: " + ",".join(region["blocked_by"]))
    return " ".join(p for p in parts if p)


def rejected_label(rejected: dict | None) -> str:
    if not rejected:
        return ""
    return f"{rejected.get('reason')}: {rejected.get('note')}" if rejected.get("note") else str(rejected.get("reason"))


def binding_rows(items: list[dict]) -> list[dict]:
    """One row per replayed item: field · value · source · column header / identity · level recorded -> now ·
    gap / veto · basis now (binding-v4 rules) · variant map region · rejected now · would be ok."""
    rows = []
    for r in items:
        column = " · ".join(str(x) for x in dict.fromkeys(
            [r.get("column_header"), r.get("effective_column_identity") or r.get("column_identity")]) if x)
        if not column and r.get("candidate_match_count"):
            column = f"{r['candidate_match_count']} candidates" + (" (ambiguous)" if r.get("candidate_match_ambiguous")
                                                                  else "")
        now = "missing document" if r.get("missing_document") else r.get("replay_error") or r.get("binding_level_now")
        recorded = r.get("binding_level_recorded") if r["kind"] == "evidence" else "candidate"
        rows.append({"field": r.get("field"), "value": str(r.get("value")), "source": _domain(r.get("source_url")),
                     "column header / identity": column, "level recorded → now": f"{recorded} → {now}",
                     "match now": r.get("variant_match_now"),
                     "gap / veto": ", ".join(r.get("binding_veto_now") or r.get("binding_gap_now") or []),
                     # binding-v4: the rule that raised the level and the Document Variant Map region that proves it
                     "basis now": " · ".join(dict.fromkeys(x for x in [r.get("binding_basis_now"),
                                                                        *(r.get("binding_rules_now") or [])] if x)),
                     "variant map region": region_label(r.get("variant_map_region_now")),
                     "rejected now": rejected_label(r.get("rejected_now")),
                     "year": (r.get("year_context") or {}).get("status"), "would be ok": r.get("would_be_ok")})
    return rows


# --- benchmark (observation metrics) -----------------------------------------------------------------------------------

PER_VEHICLE_COLS = ["vehicle", "status", "result_source", "final_output", "finalization_status", "stop_reason",
                    "primary_research_turns", "primary_research_stop_reason", "primary_research_documents_added",
                    "primary_research_candidate_fields", "primary_research_no_artifact_turns",
                    "primary_research_minimum_acquisition_met", "primary_research_stop_deferred_count",
                    "primary_research_under_acquired_turns", "primary_research_extended_turns",
                    "primary_research_scoped_coverage_pct",
                    "document_sweep_calls", "document_sweep_chunks", "document_sweep_packet_chars",
                    "document_sweep_estimated_input_tokens", "document_sweep_fields", "document_sweep_candidates",
                    "document_sweep_latency_ms", "document_sweep_timeouts", "document_sweep_output_tokens",
                    "requested_fields", "fields_failed_primary", "fields_retried", "fields_recovered",
                    "fields_still_failed", "field_retry_attempts", "field_recovery_model_calls",
                    "field_recovery_turns_used", "fields_not_attempted_due_to_budget", "fields_conflicting_final",
                    "field_recovery_early_resolutions", "field_recovery_turn_budget_skipped_by_early_resolution",
                    "duplicate_evidence_suppressed", "recovery_mode", "tail_fields_at_start", "tail_fields_resolved",
                    "tail_fields_remaining", "tail_model_calls", "tail_search_calls", "cluster_attempts",
                    "no_novelty_stops", "budget_extensions", "conflicts_normalized_without_search",
                    "portable_facts_accepted", "portable_facts_rejected", "fields_resolved_per_tail_turn",
                    "fields_resolved_per_tail_search", "tail_cost_usd", "cost_per_tail_field_resolved",
                    "verified_fact_cache_hits", "verified_facts_recorded", "negative_route_cache_hits",
                    "candidate_cache_hits", "training_feedback_examples",
                    "coverage_pct", "target_filled", "target_fields", "fields_with_value", "extra_fields",
                    "evidence_items", "evidence_rejected", "evidence_variant_exact", "evidence_variant_unclear",
                    "evidence_variant_different", "evidence_unbound", "evidence_official_source",
                    "evidence_aggregator_source", "consistency_checks_suspicious", "evidence_with_market",
                    "fields_israel_direct", "fields_foreign_direct",
                    "fields_inferred", "fields_unresolved", "cited_ids_not_in_evidence", "unique_sources",
                    "unique_domains", "documents_opened", "research_steps",
                    "tool_calls", "tool_errors", "document_cache_hits", "search_cache_hits", "search_api_calls",
                    "search_credits", "search_budget_exhausted",
                    "duplicate_searches", "duplicate_fetches", "api_errors", "api_attempts", "chat_attempts",
                    "search_attempts", "timeout_count", "unknown_usage_attempts", "conflicts_reported",
                    "additional_findings", "level3_topics", "duration_s", "model_latency_s", "model_calls",
                    "research_model_calls", "finalizer_model_calls", "prompt_tokens", "completion_tokens",
                    "cached_tokens", "finalizer_input_chars", "finalizer_prompt_tokens",
                    "finalizer_completion_tokens", "finalizer_latency_s", "research_model", "finalizer_model",
                    "cost_tokens_usd", "cost_search_usd", "cost_usd"]


def metrics_for(results: list[dict], vehicles_by_id: dict[str, dict], cache) -> list[dict]:
    """Each run's cost uses the pricing recorded with that run."""
    from ..benchmark import compute_metrics

    return [compute_metrics(r, vehicles_by_id.get(r["record_id"]), cache) for r in results]


def benchmark_view(results: list[dict], vehicles_by_id: dict[str, dict], labels: dict[str, str], cache) -> dict:
    """The Benchmark tab of one run: observation metrics per vehicle, their aggregate, tool usage by name."""
    from ..benchmark import aggregate
    from ..pricing import UNKNOWN_USAGE_NOTE

    metrics = metrics_for(results, vehicles_by_id, cache)
    if not metrics:
        return {"vehicles": 0, "aggregate": None, "columns": [], "rows": [], "tool_usage": [], "cost_note": None}
    agg = aggregate(metrics)
    rows = [{**m, "vehicle": labels.get(m["record_id"], m["record_id"])} for m in metrics]
    columns = [c for c in PER_VEHICLE_COLS if any(c in r for r in rows)]
    return {"vehicles": agg["vehicles"], "aggregate": agg, "columns": columns,
            "rows": [{c: r.get(c) for c in columns} for r in rows],
            "tool_usage": [{"vehicle": labels.get(m["record_id"], m["record_id"]), **(m.get("tool_calls_by_name") or {})}
                           for m in metrics],
            "cost_note": UNKNOWN_USAGE_NOTE if not agg["cost_complete"] else "Recorded API cost from returned usage."}


def batch_rows(runs_dir: Path, vehicles_by_id: dict[str, dict], cache) -> list[dict]:
    """"Compare batches": one aggregate row per recorded batch (run folder with a batch.json)."""
    from ..benchmark import aggregate
    from ..storage.run_loader import load_runs
    from ..storage.run_log import list_batches

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
            "no_final_json": agg["runs_without_final_output"],
            "statuses": ", ".join(f"{k}:{v}" for k, v in agg["statuses"].items()),
            "coverage_mean_%": agg["coverage_pct_mean"], "fields_mean": agg["fields_with_value_mean"],
            "extra_fields_mean": agg["extra_fields_mean"], "evidence_mean": agg["evidence_items_mean"],
            "docs_mean": agg["documents_opened_mean"], "steps_mean": agg["research_steps_mean"],
            "sweep_calls_mean": agg["document_sweep_calls_mean"],
            "sweep_packet_chars_mean": agg["document_sweep_packet_chars_mean"],
            "sweep_timeouts_total": agg["document_sweep_timeouts_total"],
            "tool_calls_mean": agg["tool_calls_mean"], "cache_hit_%": agg["document_cache_hit_rate_pct"],
            "conflicts_total": agg["conflicts_reported_total"], "findings_total": agg["additional_findings_total"],
            "time_mean_s": agg["duration_s_mean"], "searches_total": agg["search_api_calls_total"],
            "search_credits_total": agg.get("search_credits_total"),
            "timeouts_total": agg["timeout_count_total"], "unknown_usage_total": agg["unknown_usage_attempts_total"],
            "tokens_total": agg["total_tokens_total"], "cost_usd_recorded": agg["cost_usd_total"],
        })
    return rows


def identity_anchors_view(events: list[dict]) -> dict | None:
    """The run view's identity anchors (identity anchors PR): the approval route and code family (identity_fingerprint),
    the international variant per open-data source and the offers per field (admitted / shadow / vetoed / reference),
    the importer spec sheets found. None for a run before them."""
    from ..gov_registry import fingerprint_event

    fp = next((e for e in events if e.get("kind") == "identity_fingerprint"), None)
    od = next((e for e in events if e.get("kind") == "open_data_match"), None)
    sheets = next((e for e in events if e.get("kind") == "spec_sheet_discovery"), None)
    if not (fp or od or sheets):
        return None
    out: dict = {}
    if fp:
        view = fingerprint_event({k: v for k, v in fp.items() if k not in ("kind", "seq", "ts", "t")})
        out["fingerprint"] = {k: view.get(k) for k in ("tozeret_cd", "degem_cd", "type_code", "year", "code_family",
                                                       "code_years", "approval_route", "homologation",
                                                       "equivalent_codes", "catalog", "other_codes_known")}
    if od:
        out["open_data"] = {
            **{k: od.get(k) for k in ("mode", "route", "route_detail", "level", "level_basis", "designation",
                                      "lead_source", "error")},
            "sources": [{"source": name, "status": (src or {}).get("status"), "candidates": (src or {}).get("candidates"),
                         "designation": (src or {}).get("designation"),
                         "keys_matched": ", ".join((src or {}).get("keys_matched") or []),
                         "keys_unknown": ", ".join((src or {}).get("keys_unknown") or []),
                         "vetoes": ", ".join(f"{k} {v}" for k, v in sorted(((src or {}).get("veto_counts") or {})
                                                                            .items()))}
                        for name, src in (od.get("sources") or {}).items()],
            "offers": [{"field": o.get("field"), "source": o.get("source"), "value": o.get("value"),
                        "status": "admitted" if o.get("admitted") else (
                            "shadow" if o.get("status") == "offered" else o.get("status")),
                        "definition": o.get("definition"), "identified_by": o.get("identified_by"),
                        "reason": o.get("reason")} for o in od.get("offers") or []]}
    if sheets:
        out["spec_sheets"] = {k: sheets.get(k) for k in ("domains", "allowed", "searches", "fetches", "sheets",
                                                         "candidates", "skipped")}
    return out

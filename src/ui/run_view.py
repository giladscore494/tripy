"""Per-vehicle run views: live progress, inputs, tool calls, documents, evidence, results.

Runs without a final result.json (crash, Ctrl+C, finalization failure) are shown
from the trace reconstructed out of events.jsonl, never hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from ..app_config import redact, redact_obj
from ..pricing import UNKNOWN_USAGE_NOTE
from ..schemas import iter_fields
from ..storage import trace
from ..storage.cache import DocumentCache
from ..storage.run_loader import document_text, run_document_metas
from ..storage.run_log import load_events, load_input
from . import labels_he as he
from .live_state import candidate_table_rows, feed_line

STATUS_ICON = {"completed": "✅", "max_steps_finalized": "⏱️", "no_new_research_finalized": "⏱️",
               "acquisition_sufficient_finalized": "⏱️",
               "completed_unparsed": "⚠️", "recovered_finalized": "♻️", "finalization_failed": "🟠",
               "research_failed": "❌", "interrupted": "⏹️", "incomplete": "🟡", "error": "❌",
               "finalization_pending": "⏳"}
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


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _key(result: dict, name: str) -> str:
    return f"{name}_{result.get('batch_id')}_{result.get('record_id')}"


def _frame(records: list[dict]) -> pd.DataFrame:
    """DataFrame whose object columns are text, so mixed values (2295 vs "229,990 ILS") render cleanly."""
    df = pd.DataFrame(records)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: v if v is None or isinstance(v, str)
                                  else json.dumps(v, ensure_ascii=False, default=str))
    return df


def has_research(result: dict) -> bool:
    return bool(result.get("tool_calls") or result.get("evidence") or result.get("documents")
                or (result.get("usage") or {}).get("model_calls"))


def live_listener(placeholder, max_lines: int = 40) -> Callable[[str, dict], None]:
    """A RunLog listener rendering a rolling text feed (single-run / debug use; it calls Streamlit, so use it
    only on the script thread). Batches use the live dashboard (src/ui/live_dashboard.py) instead."""
    lines: list[str] = []

    def listen(kind: str, event: dict) -> None:
        line = feed_line(kind, event)
        if line:
            lines.append(line)
        placeholder.code("\n".join(lines[-max_lines:]), language=None)

    return listen


def evidence_reused_line(event: dict) -> str:
    return feed_line("evidence_reused", event) or ""


def tool_call_line(event: dict) -> str:
    """Live-feed line for a tool call; field-recovery calls name their field and attempt."""
    return feed_line("tool_call", event) or ""


def model_turn_line(event: dict) -> str:
    """Live-feed line for a model turn, e.g.
    🧠 field_recovery · battery_usable_kwh · attempt 1/2 · turn 2/4 · tokens 7263"""
    return feed_line("model_response", event) or ""


def _human_view(output) -> None:
    if not isinstance(output, dict):
        st.info("The model's final answer was not a JSON object; see the JSON tab for the raw text.")
        return
    if output.get("summary"):
        st.markdown(f"**Summary.** {output['summary']}")
    if isinstance(output.get("variant_identity"), dict):
        st.markdown("**Variant identity (as reported by the model)**")
        st.json(output["variant_identity"], expanded=False)
    rows = []
    for name, entry in iter_fields(output):
        rows.append({"field": name, "value": _short(entry.get("value", entry.get("values")), 200),
                     "unit": entry.get("unit"), "market": entry.get("market"), "provenance": entry.get("provenance"),
                     "alternatives": _short(entry.get("alternatives"), 200) if entry.get("alternatives") else None,
                     "notes": _short(entry.get("notes") or "", 300),
                     "evidence": ", ".join(map(str, entry.get("evidence_ids") or []))})
    if rows:
        st.markdown("**Fields**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    conflicts = output.get("conflicts")
    if conflicts:
        st.markdown("**Conflicts reported by the model**")
        st.dataframe(pd.DataFrame([{k: _short(v, 300) for k, v in c.items()} if isinstance(c, dict) else {"item": c}
                                   for c in conflicts]), hide_index=True, width="stretch")
    summary = output.get("provenance_summary")
    if isinstance(summary, dict) and summary:
        st.markdown("**Provenance (as reported by the model)**")
        labels = {"israeli_market_values": "Israeli-market values", "foreign_market_values": "Foreign-market values",
                  "inferred_variant_mappings": "Inferred variant mappings", "conflicts": "Conflicts",
                  "unresolved_fields": "Unresolved / null"}
        for key, label in labels.items():
            if summary.get(key):
                st.markdown(f"- **{label}:** {_short(summary[key], 600)}")
    findings = output.get("additional_findings")
    if findings:
        st.markdown("**Additional findings**")
        for item in findings:
            st.markdown("- " + _short(item, 600))
    level3 = output.get("level3")
    if level3:
        st.markdown("**Level 3 (open research)**")
        st.json(level3, expanded=False)
    known = {"vehicle_id", "summary", "fields", "conflicts", "additional_findings", "level3", "research_trace",
             "variant_identity", "provenance_summary"}
    extra = {k: v for k, v in output.items() if k not in known}
    if extra:
        st.markdown("**Other keys returned by the model**")
        st.json(extra, expanded=False)
    if output.get("research_trace"):
        with st.container(border=True):
            st.markdown("**Research trace (model's own account)**")
            for step in output["research_trace"]:
                st.markdown("- " + _short(step, 400))


def no_output_message(result: dict) -> str:
    reason = NO_OUTPUT_REASON.get(result.get("status"), "no final JSON was produced")
    return f"Not produced — {reason}."


def _source_urls(result: dict, metas: list[dict]) -> list[str]:
    urls: list[str] = []
    for item in result.get("evidence") or []:
        if item.get("source_url") and item["source_url"] not in urls:
            urls.append(item["source_url"])
    for meta in metas:
        url = meta.get("final_url") or meta.get("url")
        if url and url not in urls:
            urls.append(url)
    return urls


def render_partial_research(result: dict, runs_dir: Path, cache: DocumentCache | None, *, key: str) -> None:
    """Everything the research produced, without inventing structured values."""
    bundle = result.get("research_bundle") or {}
    metas = run_document_metas(result, runs_dir, cache)
    st.caption("Research material as collected. These are not final structured fields and no value was derived "
               "from them by code.")
    if result.get("evidence"):
        st.markdown(f"**Stored evidence** ({len(result['evidence'])})")
        st.dataframe(_frame(result["evidence"]), hide_index=True, width="stretch")
    else:
        st.caption("No evidence stored.")
    facts = bundle.get("candidate_facts") or {}
    if facts:
        st.markdown("**Candidate facts (grouped by field, as stored)**")
        st.dataframe(pd.DataFrame([{"field": f, "values": _short([v.get("value") for v in values], 300),
                                    "evidence": ", ".join(str(v.get("evidence_id")) for v in values)}
                                   for f, values in facts.items()]), hide_index=True, width="stretch")
    if bundle.get("fields_with_multiple_stored_values"):
        st.markdown("Fields with more than one stored value: "
                    + ", ".join(bundle["fields_with_multiple_stored_values"]))
    if bundle.get("model_noted_conflicts"):
        st.markdown("**Conflicts mentioned during research**")
        for note in bundle["model_noted_conflicts"]:
            st.markdown(f"- _{note.get('source')}_: {_short(note.get('text'), 400)}")
    responses = [r for r in (result.get("model_responses") or _responses_from_events(result, runs_dir))
                 if (r.get("content") or "").strip()]
    if responses:
        st.markdown("**Model response excerpts**")
        for r in responses[-5:]:
            st.markdown(f"- seq {r.get('seq')} ({r.get('phase')}): {_short(r.get('content'), 500)}")
    last = result.get("last_model_content") or bundle.get("last_model_content")
    if last:
        st.markdown("**Last successful model response**")
        st.text_area("last model response", last, height=150, key=f"{key}_last", label_visibility="collapsed")
    urls = _source_urls(result, metas)
    if urls:
        st.markdown(f"**URLs** ({len(urls)})")
        st.markdown("\n".join(f"- {u}" for u in urls[:100]))
    if metas:
        st.markdown(f"**Documents** ({len(metas)})")
        render_documents_table(metas)
    render_target_status(bundle)
    errors = [e for e in (result.get("api_errors") or [])]
    if result.get("error") or errors:
        st.markdown("**Errors**")
        if result.get("error"):
            st.error(redact(result["error"]))
        if errors:
            st.dataframe(pd.DataFrame([{k: redact(_short(v, 200)) for k, v in e.items() if k not in ("headers",)}
                                       for e in errors]), hide_index=True, width="stretch")
    if bundle:
        with st.expander("Partial research bundle (JSON)"):
            st.json(bundle, expanded=False)


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


def render_target_status(bundle: dict) -> None:
    lines = target_status_lines(bundle)
    if lines["no_evidence"]:
        st.markdown("**Targets with no stored evidence** (Level 2): " + ", ".join(lines["no_evidence"]))
    if lines["unresolved"]:
        st.markdown("**Targets still unresolved** (Level 2, current state; evidence may exist):")
        st.markdown("\n".join(f"- {line}" for line in lines["unresolved"]))
    if lines["level3"]:
        st.markdown("**Level 3 topics not researched / without evidence:** " + ", ".join(lines["level3"]))


EVIDENCE_COLUMNS = ("evidence_id", "field", "value", "unit", "variant_match", "binding_level", "binding_veto", "market",
                    "market_basis", "source_authority", "source_domain", "entailment", "condition", "valid_as_of",
                    "quote", "document_id", "model_variant_claim", "model_market_claim", "note")


def render_evidence(result: dict, runs_dir: Path) -> None:
    """Admitted evidence with its server-side provenance, rejected requests and cross-field QA checks."""
    evidence = result.get("evidence") or []
    admission = result.get("evidence_admission") or {}
    if admission:
        st.caption(f"Admitted {admission.get('admitted', len(evidence))} · rejected {admission.get('rejected', 0)} · "
                   f"variant match {admission.get('admitted_by_variant_match') or {}} · "
                   f"authority {admission.get('admitted_by_source_authority') or {}}")
    if evidence:
        rows = [{k: item.get(k) for k in EVIDENCE_COLUMNS if k in item} for item in evidence]
        st.dataframe(_frame(rows), hide_index=True, width="stretch")
        st.caption("variant_match / binding_level / market / source_authority are computed by the runtime from the "
                   "source; the model's claims are kept as model_*_claim. Notes are commentary, never evidence.")
    else:
        st.caption("No evidence stored.")
    events = load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", "")) if runs_dir else []
    rejected = [{"field": e.get("field"), "value": e.get("value"), "reasons": ", ".join(e.get("reasons") or []),
                 "document_id": e.get("document_id"), "quote": (e.get("request") or {}).get("quote")}
                for e in events if e.get("kind") == "evidence_rejected"]
    if rejected:
        st.markdown(f"**Rejected by the admission gate** ({len(rejected)})")
        st.dataframe(_frame(rejected), hide_index=True, width="stretch")
    checks = (result.get("consistency_checks") or {}).get("checks") or []
    if checks:
        st.markdown("**Cross-field consistency checks** (QA signals only; they never change a value)")
        st.dataframe(_frame(checks), hide_index=True, width="stretch")


def _responses_from_events(result: dict, runs_dir: Path) -> list[dict]:
    return trace.model_responses(load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", "")))


def _status_banner(result: dict) -> None:
    if result.get("status") == "finalization_pending":
        st.info(he.FINALIZATION_PENDING_MESSAGE_HE)
        st.caption(f"python -m src.cli --finalize-existing --batch-id {result.get('batch_id')} "
                   f"--record-id {result.get('record_id')}")
    if result.get("interrupted"):
        # A script-control interruption (Stop / rerun / Ctrl+C), not a research, API or tool failure.
        st.info(result.get("interruption_message") or "Run interrupted. All completed research/evidence was "
                "preserved. The result below is partial.")
    if result.get("synthesized"):
        st.warning(result.get("banner") or "This run did not produce a final result.json.")
        facts = [f"status **{result.get('status')}**", f"started {result.get('started_at')}",
                 f"latest event {result.get('latest_event_at')}",
                 f"last successful step {result.get('last_successful_step')}",
                 f"{result.get('research_steps')} model turn(s)", f"{len(result.get('tool_calls') or [])} tool call(s)"]
        st.markdown(" · ".join(facts))
        if result.get("end_state"):
            st.caption(result["end_state"])
    if result.get("recovered"):
        rec = result.get("recovery") or {}
        st.info(f"Recovered from a prior research run: finalized {rec.get('recovered_at')} with "
                f"{rec.get('finalizer_model')} (prior status {rec.get('prior_status')}). The web research was not "
                "repeated.")
    if result.get("cost_note"):
        st.caption("💲 " + result["cost_note"])


def render_vehicle(result: dict, label: str, runs_dir: Path, cache: DocumentCache) -> None:
    status = result.get("status")
    icon = STATUS_ICON.get(status, "•")
    suffix = " · no result.json" if result.get("synthesized") else " · recovered" if result.get("recovered") else ""
    duration = result.get("duration_s")
    with st.expander(f"{icon} {label} · {status} · {duration}s{suffix}", expanded=bool(result.get("synthesized"))):
        _status_banner(result)
        tabs = st.tabs(["Human view", "JSON", "Partial research", "Evidence", "Tool calls", "Documents",
                        "Model responses", "Level 1.5 input", "Run config & cost", "API attempts", "Events",
                        "Field recovery", "מועמדים (Candidates)"])
        with tabs[0]:
            if result.get("output") is not None:
                if result.get("error"):
                    st.error(redact(result["error"]))
                _human_view(result.get("output"))
            else:
                st.markdown("**Final structured result**")
                st.warning(no_output_message(result))
                if result.get("error"):
                    st.error(redact(result["error"]))
                if has_research(result):
                    st.caption("See the Partial research tab for everything the research collected.")
        with tabs[1]:
            st.caption(f"parse: {result.get('parse_note')}")
            if result.get("output") is not None:
                st.json(result["output"], expanded=True)
            st.text_area("Raw final text", result.get("raw_final_text") or "", height=200, key=_key(result, "raw"))
        with tabs[2]:
            render_partial_research(result, runs_dir, cache, key=_key(result, "partial"))
        with tabs[3]:
            render_evidence(result, runs_dir)
        with tabs[4]:
            if result.get("tool_calls"):
                df = _frame(result["tool_calls"])
                df["arguments"] = df["arguments"].map(lambda a: _short(a, 300))
                st.dataframe(df, hide_index=True, width="stretch")
            else:
                st.caption("No tool calls.")
        with tabs[5]:
            render_documents_table(run_document_metas(result, runs_dir, cache))
        with tabs[6]:
            responses = result.get("model_responses") or _responses_from_events(result, runs_dir)
            st.caption(f"{len(responses)} successful model response(s)")
            for r in responses:
                usage = r.get("usage") or {}
                title = (f"seq {r.get('seq')} · {r.get('phase')} · {r.get('tool_calls')} tool call(s) · "
                         f"{usage.get('prompt_tokens', '?')}/{usage.get('completion_tokens', '?')} tokens · "
                         f"{r.get('latency_ms')} ms")
                with st.expander(title):
                    if r.get("content"):
                        st.markdown("**Content**")
                        st.text(r["content"])
                    if r.get("reasoning_content"):
                        st.markdown("**Reasoning content**")
                        st.text(r["reasoning_content"])
        with tabs[7]:
            payload = load_input(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
            st.json(payload or {}, expanded=False)
        with tabs[8]:
            if result.get("api_error"):
                st.error("Raw GLM API error")
                st.json(redact_obj(result["api_error"]), expanded=True)
            st.markdown(f"**Research model:** {result.get('research_model') or result.get('model')} · "
                        f"**Finalizer model:** {result.get('finalizer_model')} · "
                        f"**Stop reason:** {result.get('stop_reason')} · "
                        f"**Prompt version:** {result.get('prompt_version')}")
            if result.get("finalization"):
                st.markdown("**Finalization**")
                st.json({k: v for k, v in result["finalization"].items() if k not in ("raw_text", "repair_text")},
                        expanded=False)
            st.markdown("**Effective configuration**")
            st.json(result.get("effective_config") or result.get("glm_config") or {}, expanded=False)
            st.markdown("**Usage, search calls and cost**")
            st.caption(UNKNOWN_USAGE_NOTE if (result.get("api_stats") or {}).get("unknown_usage_attempts")
                       else "Recorded API cost from returned usage.")
            st.json({"usage": result.get("usage"), "usage_research": result.get("usage_research"),
                     "usage_finalizer": result.get("usage_finalizer"),
                     "search_api_calls": result.get("search_api_calls"), "pricing": result.get("pricing"),
                     "pricing_finalizer": result.get("pricing_finalizer"), "cost": result.get("cost"),
                     "cost_details": result.get("cost_details"), "research_tracking": {
                         k: v for k, v in (result.get("research_tracking") or {}).items() if k != "turns"},
                     "documents_dir": result.get("documents_dir")}, expanded=False)
        with tabs[9]:
            st.json(result.get("api_stats") or {}, expanded=True)
            errors = result.get("api_errors") or []
            if errors:
                st.dataframe(pd.DataFrame([{k: redact(_short(v, 200)) for k, v in e.items() if k != "headers"}
                                           for e in errors]), hide_index=True, width="stretch")
            else:
                st.caption("No failed API attempts.")
        with tabs[11]:
            render_field_recovery(result, runs_dir)
        with tabs[12]:
            render_candidates(result, runs_dir)
        with tabs[10]:
            events = load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
            st.caption(f"{len(events)} events in events.jsonl")
            kinds = sorted({e["kind"] for e in events})
            chosen = st.multiselect("Kinds", kinds, default=[k for k in kinds if k != "tool_result"],
                                    key=_key(result, "kinds"))
            for event in events:
                if event["kind"] in chosen:
                    st.json(redact_obj(event), expanded=False)


def render_candidates(result: dict, runs_dir: Path) -> None:
    """Deterministic candidate matrix (Hebrew table) + layered-pipeline metrics + raw candidate JSON."""
    events = load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
    started = trace.first_event(events, "run_started") or {}
    specs = started.get("requested_field_specs") or []
    summary = result.get("candidate_summary") or {}
    st.caption("מועמדים הם ערכים שהקוד איתר במסמכים. הם אינם ראיות ואינם משנים את מצב השדה; רק ראיה שנשמרה "
               "על ידי המודל נחשבת. כיסוי מועמדים אינו מדד לנכונות.")
    if summary:
        cols = st.columns(4)
        cols[0].metric("מסמכים שנסרקו", summary.get("documents_harvested", 0))
        cols[1].metric("מועמדים", summary.get("candidate_count_total", 0))
        cols[2].metric("שדות עם מועמדים", summary.get("candidate_fields_total", 0))
        cols[3].metric("נפתרו בבדיקת המסמכים", summary.get("document_sweep_fields_resolved", 0))
    rows = candidate_table_rows(events, specs, started.get("vehicle_label")) if specs else []
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No deterministic candidates were recorded for this run.")
    with st.expander("Raw candidates / layered metrics (JSON)"):
        st.json({"candidate_summary": summary, "primary_research": result.get("primary_research"),
                 "document_sweep": result.get("document_sweep"),
                 "candidates": [e for e in events if e.get("kind") == "candidates_harvested"]}, expanded=False)


def render_field_recovery(result: dict, runs_dir: Path | None = None) -> None:
    """Requested-field states after primary research and after targeted retries."""
    recovery = result.get("field_recovery")
    requested = result.get("requested_fields") or {}
    st.caption(f"{len(requested)} requested enrichment field(s). Fields with a usable candidate after primary "
               "research, or marked not applicable, are never retried. States come from the model's own evidence "
               "and declarations; this is not a fact check.")
    if not recovery:
        st.caption("Field detection did not run for this run (older run, or research did not finish).")
        return
    if recovery.get("error"):
        st.error(redact(recovery["error"]))
    cols = st.columns(4)
    cols[0].metric("Failed after primary", len(recovery.get("queue") or []))
    cols[1].metric("Retried", len(recovery.get("fields_retried") or []))
    cols[2].metric("Recovered", len(recovery.get("fields_recovered") or []))
    cols[3].metric("Retry attempts", recovery.get("attempt_count") or 0)
    budget = recovery.get("field_recovery_turn_budget")
    st.caption(f"Recovery turns used: {recovery.get('field_recovery_turns_used', recovery.get('turns', 0))}"
               + (f" of {budget} (remaining {recovery.get('field_recovery_turns_remaining')})" if budget
                  else " (no cap)"))
    if recovery.get("stopped"):
        st.caption(f"Retries stopped early: {recovery['stopped']}")
    if recovery.get("fields_not_attempted_due_to_budget"):
        st.warning("Not attempted because the recovery turn budget was exhausted: "
                   + ", ".join(recovery["fields_not_attempted_due_to_budget"])
                   + (f" (cut short: {recovery['field_cut_short_by_budget']})"
                      if recovery.get("field_cut_short_by_budget") else ""))
    primary = {f["field"]: f for f in recovery.get("evaluation_primary") or []}
    final = {f["field"]: f for f in recovery.get("evaluation_final") or []}
    attempts: dict[str, int] = {}
    for a in recovery.get("attempts") or []:
        for name in a.get("fields") or [a.get("field")]:     # a cluster attempt covers several fields
            attempts[name] = attempts.get(name, 0) + 1
    if recovery.get("mode") == "cluster":
        st.caption(f"Clustered tail recovery · cluster attempts {recovery.get('cluster_attempts', 0)} · "
                   f"tail fields {recovery.get('tail_fields_at_start', 0)} → resolved "
                   f"{recovery.get('tail_fields_resolved', 0)} · tail searches {recovery.get('tail_search_calls', 0)} · "
                   f"no-novelty stops {recovery.get('no_novelty_stops', 0)} · turn extensions "
                   f"{recovery.get('budget_extensions', 0)} · conflicts normalized without search "
                   f"{recovery.get('conflicts_normalized_without_search', 0)} · portable facts accepted / rejected "
                   f"{recovery.get('portable_facts_accepted', 0)} / {recovery.get('portable_facts_rejected', 0)}")
        if recovery.get("triage"):
            st.dataframe(pd.DataFrame([{"field": f, **v} for f, v in recovery["triage"].items()]),
                         hide_index=True, width="stretch")
    current = recovery.get("current_states") or {}
    rows = [{"field": name, "primary_state": (primary.get(name) or {}).get("state"),
             "retry_attempts": attempts.get(name, 0), "last_attempt_state": (final.get(name) or {}).get("state"),
             "current_state": current.get(name, (final.get(name) or {}).get("state")),
             "evidence": ", ".join(str(i) for i in (final.get(name) or primary.get(name) or {}).get("evidence_ids") or []),
             "markets": ", ".join((final.get(name) or {}).get("markets") or []),
             "info": ", ".join((final.get(name) or {}).get("info") or [])}
            for name in (primary or final)]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if recovery.get("attempts"):
        st.markdown("**Retry attempts**")
        st.dataframe(pd.DataFrame([{"field": a.get("field"), "attempt": a.get("attempt"),
                                    "before": a.get("state_before"), "after": a.get("state_after"),
                                    "turns": a.get("turns"), "early_resolved": a.get("early_resolved"),
                                    "mode": a.get("mode"), "stop": a.get("stop"),
                                    "searches": a.get("search_provider_calls"),
                                    "resolved": ", ".join(a.get("fields_resolved") or []),
                                    "prior_excerpts": a.get("prior_excerpt_items"),
                                    "prior_excerpt_chars": a.get("prior_excerpt_chars"),
                                    "packet_chars": a.get("packet_chars"),
                                    "rereads_after_excerpt": a.get("document_rereads_after_prior_excerpt"),
                                    "reply": _short(a.get("reply") or a.get("reply_text"), 300),
                                    "error": a.get("error")} for a in recovery["attempts"]]),
                     hide_index=True, width="stretch")
        st.caption(f"prior excerpts supplied: {recovery.get('prior_excerpt_items', 0)} · "
                   f"prior excerpt chars: {recovery.get('prior_excerpt_chars', 0):,} · "
                   f"attempts with prior excerpts: {recovery.get('attempts_with_prior_excerpts', 0)}")
    if runs_dir is not None:
        started = [e for e in load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
                   if e.get("kind") == "field_recovery_started" and e.get("prior_excerpts")]
        for event in started:
            with st.expander(f"Prior excerpts · {event.get('field')} · attempt {event.get('attempt')} · "
                             f"{event.get('prior_excerpt_items')} item(s), {event.get('prior_excerpt_chars')} chars"):
                st.json(event["prior_excerpts"], expanded=False)


def render_documents_table(metas: list[dict]) -> None:
    if not metas:
        st.caption("No documents.")
        return
    cols = ["document_id", "kind", "url", "final_url", "status", "content_type", "extraction_path", "bytes",
            "text_chars", "pages", "truncated", "fetched_at"]
    st.dataframe(pd.DataFrame([{c: m.get(c) for c in cols} for m in metas]), hide_index=True,
                 width="stretch")


def batch_document_metas(results: list[dict], runs_dir: Path, cache: DocumentCache | None) -> list[tuple[dict, dict]]:
    """(meta, owning result) for every document any run of the batch touched, first occurrence wins."""
    seen: dict[str, tuple[dict, dict]] = {}
    for result in results:
        for meta in run_document_metas(result, runs_dir, cache):
            seen.setdefault(meta["document_id"], (meta, result))
    return list(seen.values())


def render_documents_tab(cache: DocumentCache, results: list[dict], runs_dir: Path) -> None:
    all_docs = cache.list_documents()
    batch_docs = batch_document_metas(results, runs_dir, cache)
    owners = {meta["document_id"]: result for meta, result in batch_docs}
    st.caption(f"Shared cache: {len(all_docs)} documents · this batch touched {len(batch_docs)} · "
               f"session cache stats {cache.stats}")
    scope = st.radio("Show", ["This batch", "Entire cache"], horizontal=True, key="docs_scope")
    metas = [m for m, _ in batch_docs] if scope == "This batch" else all_docs
    render_documents_table(metas)
    if metas:
        doc_id = st.selectbox("Preview document", [m["document_id"] for m in metas], key="doc_preview")
        meta = next((m for m in metas if m["document_id"] == doc_id), None) or cache.get(doc_id) or {}
        st.json(meta, expanded=False)
        text = document_text(doc_id, owners.get(doc_id), runs_dir, cache)
        st.text_area("Extracted text", text[:50000] if text else "(text not available in the cache or run folder)",
                     height=300, key=f"doc_text_{doc_id}")


def render_results_tab(results: list[dict], labels: dict[str, str], runs_dir: Path,
                       cache: DocumentCache | None = None) -> None:
    rows = []
    for result in results:
        for name, entry in iter_fields(result.get("output")):
            rows.append({"vehicle": labels.get(result["record_id"], result["record_id"]), "field": name,
                         "value": _short(entry.get("value", entry.get("values")), 200), "unit": entry.get("unit"),
                         "market": entry.get("market"), "provenance": entry.get("provenance"),
                         "notes": _short(entry.get("notes") or "", 200),
                         "evidence": ", ".join(map(str, entry.get("evidence_ids") or []))})
    without_output = [r for r in results if r.get("output") is None]
    if rows:
        df = pd.DataFrame(rows)
        st.caption("Values exactly as the model returned them. No reliability pass/fail is applied.")
        view = st.radio("Layout", ["Long", "Wide (vehicle × field)"], horizontal=True, key="results_layout")
        if view.startswith("Wide"):
            st.dataframe(df.pivot_table(index="vehicle", columns="field", values="value", aggfunc="first"),
                         width="stretch")
        else:
            vehicle = st.selectbox("Vehicle", ["All"] + sorted(df["vehicle"].unique()), key="results_vehicle")
            st.dataframe(df if vehicle == "All" else df[df["vehicle"] == vehicle], hide_index=True,
                         width="stretch")
    elif not results:
        st.caption("No runs in this batch yet.")
        return
    for result in without_output:
        label = labels.get(result["record_id"], result["record_id"])
        st.subheader(label)
        st.markdown("**Final structured result**")
        st.warning(no_output_message(result))
        if has_research(result):
            st.markdown("**Partial research**")
            render_partial_research(result, runs_dir, cache, key=_key(result, "results_partial"))
        else:
            st.caption("No research was recorded for this run.")

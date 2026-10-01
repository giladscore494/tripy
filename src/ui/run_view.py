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

from ..pricing import UNKNOWN_USAGE_NOTE
from ..schemas import iter_fields
from ..storage import trace
from ..storage.cache import DocumentCache
from ..storage.run_loader import document_text, run_document_metas
from ..storage.run_log import load_events, load_input

STATUS_ICON = {"completed": "✅", "max_steps_finalized": "⏱️", "no_new_research_finalized": "⏱️",
               "completed_unparsed": "⚠️", "recovered_finalized": "♻️", "finalization_failed": "🟠",
               "research_failed": "❌", "interrupted": "⏹️", "incomplete": "🟡", "error": "❌"}
FAILED_STATUSES = ("research_failed", "finalization_failed", "interrupted", "incomplete", "error")
NO_OUTPUT_REASON = {
    "finalization_failed": "finalization failed",
    "interrupted": "the run was interrupted",
    "incomplete": "the run ended without a result.json (finalization did not complete)",
    "research_failed": "research failed",
    "error": "the run failed",
    "completed_unparsed": "the final answer could not be parsed as JSON",
}


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _key(result: dict, name: str) -> str:
    return f"{name}_{result.get('batch_id')}_{result.get('record_id')}"


def has_research(result: dict) -> bool:
    return bool(result.get("tool_calls") or result.get("evidence") or result.get("documents")
                or (result.get("usage") or {}).get("model_calls"))


def live_listener(placeholder, max_lines: int = 40) -> Callable[[str, dict], None]:
    """Return a RunLog listener that renders a rolling feed of tool activity."""
    lines: list[str] = []

    def listen(kind: str, event: dict) -> None:
        if kind == "tool_call":
            lines.append(f"🔧 step {event.get('step')} · {event.get('name')}({_short(event.get('arguments'), 140)})")
        elif kind == "tool_result":
            result = event.get("result") or {}
            if isinstance(result, dict) and result.get("error"):
                lines.append(f"   ↳ error: {result.get('error')} {_short(result.get('message', ''), 120)}")
            elif isinstance(result, dict) and "results" in result:
                lines.append(f"   ↳ {len(result['results'])} results{' (cache)' if result.get('cache_hit') else ''}")
            elif isinstance(result, dict) and result.get("document_id"):
                hit = " (cache)" if result.get("cache_hit") else ""
                lines.append(f"   ↳ {result['document_id']} {result.get('status', '')} "
                             f"{_short(result.get('final_url') or result.get('url') or '', 90)}{hit}")
        elif kind == "duplicate_work":
            lines.append(f"   ↺ repeated work: {_short(event.get('note'), 120)}")
        elif kind == "evidence":
            ev = event.get("evidence", {})
            lines.append(f"📌 {ev.get('evidence_id')} {ev.get('field')} = {_short(ev.get('value'), 60)}")
        elif kind == "model_response":
            usage = event.get("usage") or {}
            calls = event.get("tool_calls") or []
            lines.append(f"🧠 {event.get('phase', 'research')} turn · {len(calls)} tool call(s) · "
                         f"tokens {usage.get('total_tokens', '?')}")
        elif kind == "research_stopped":
            lines.append(f"🛑 research stopped: {event.get('reason')} after {event.get('steps')} step(s)")
        elif kind == "finalization_started":
            lines.append(f"🧾 finalization with {event.get('model')} · bundle {event.get('finalizer_input_chars')} chars")
        elif kind == "api_error":
            lines.append(f"⚠️ API {event.get('request_kind') or event.get('endpoint')} attempt "
                         f"{event.get('attempt')}/{event.get('max_attempts', '?')}: "
                         f"{event.get('status') or event.get('error')} {_short(event.get('body') or '', 160)}")
        elif kind in ("error", "finalization_failed"):
            lines.append(f"❌ {event.get('message') or event.get('error')}")
        elif kind == "interrupted":
            lines.append(f"⏹️ interrupted during {event.get('phase')}")
        placeholder.code("\n".join(lines[-max_lines:]), language=None)

    return listen


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
        st.dataframe(pd.DataFrame(result["evidence"]), hide_index=True, width="stretch")
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
    if bundle.get("unresolved_targets"):
        st.markdown("**Targets with no stored evidence:** " + ", ".join(bundle["unresolved_targets"]))
    errors = [e for e in (result.get("api_errors") or [])]
    if result.get("error") or errors:
        st.markdown("**Errors**")
        if result.get("error"):
            st.error(result["error"])
        if errors:
            st.dataframe(pd.DataFrame([{k: _short(v, 200) for k, v in e.items() if k not in ("headers",)}
                                       for e in errors]), hide_index=True, width="stretch")
    if bundle:
        with st.expander("Partial research bundle (JSON)"):
            st.json(bundle, expanded=False)


def _responses_from_events(result: dict, runs_dir: Path) -> list[dict]:
    return trace.model_responses(load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", "")))


def _status_banner(result: dict) -> None:
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
                        "Model responses", "Level 1.5 input", "Run config & cost", "API attempts", "Events"])
        with tabs[0]:
            if result.get("output") is not None:
                if result.get("error"):
                    st.error(result["error"])
                _human_view(result.get("output"))
            else:
                st.markdown("**Final structured result**")
                st.warning(no_output_message(result))
                if result.get("error"):
                    st.error(result["error"])
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
            if result.get("evidence"):
                st.dataframe(pd.DataFrame(result["evidence"]), hide_index=True, width="stretch")
            else:
                st.caption("No evidence stored.")
        with tabs[4]:
            if result.get("tool_calls"):
                df = pd.DataFrame(result["tool_calls"])
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
                st.json(result["api_error"], expanded=True)
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
                st.dataframe(pd.DataFrame([{k: _short(v, 200) for k, v in e.items() if k != "headers"}
                                           for e in errors]), hide_index=True, width="stretch")
            else:
                st.caption("No failed API attempts.")
        with tabs[10]:
            events = load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
            st.caption(f"{len(events)} events in events.jsonl")
            kinds = sorted({e["kind"] for e in events})
            chosen = st.multiselect("Kinds", kinds, default=[k for k in kinds if k != "tool_result"],
                                    key=_key(result, "kinds"))
            for event in events:
                if event["kind"] in chosen:
                    st.json(event, expanded=False)


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

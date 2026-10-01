"""Per-vehicle run views: live progress, inputs, tool calls, documents, evidence, results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from ..schemas import iter_fields
from ..storage.cache import DocumentCache
from ..storage.run_log import load_events, load_input

STATUS_ICON = {"completed": "✅", "max_steps_finalized": "⏱️", "completed_unparsed": "⚠️", "error": "❌"}


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


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
        elif kind == "evidence":
            ev = event.get("evidence", {})
            lines.append(f"📌 {ev.get('evidence_id')} {ev.get('field')} = {_short(ev.get('value'), 60)}")
        elif kind == "model_response":
            usage = event.get("usage") or {}
            calls = event.get("tool_calls") or []
            lines.append(f"🧠 model turn · {len(calls)} tool call(s) · tokens {usage.get('total_tokens', '?')}")
        elif kind == "error":
            lines.append(f"❌ {event.get('message')}")
        placeholder.code("\n".join(lines[-max_lines:]), language=None)

    return listen


def _human_view(output) -> None:
    if not isinstance(output, dict):
        st.info("The model's final answer was not a JSON object; see the JSON tab for the raw text.")
        return
    if output.get("summary"):
        st.markdown(f"**Summary.** {output['summary']}")
    rows = []
    for name, entry in iter_fields(output):
        rows.append({"field": name, "value": _short(entry.get("value", entry.get("values")), 200),
                     "unit": entry.get("unit"), "notes": _short(entry.get("notes") or "", 300),
                     "evidence": ", ".join(map(str, entry.get("evidence_ids") or []))})
    if rows:
        st.markdown("**Fields**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    conflicts = output.get("conflicts")
    if conflicts:
        st.markdown("**Conflicts reported by the model**")
        st.dataframe(pd.DataFrame([{k: _short(v, 300) for k, v in c.items()} if isinstance(c, dict) else {"item": c}
                                   for c in conflicts]), hide_index=True, width="stretch")
    findings = output.get("additional_findings")
    if findings:
        st.markdown("**Additional findings**")
        for item in findings:
            st.markdown("- " + _short(item, 600))
    level3 = output.get("level3")
    if level3:
        st.markdown("**Level 3 (open research)**")
        st.json(level3, expanded=False)
    known = {"vehicle_id", "summary", "fields", "conflicts", "additional_findings", "level3", "research_trace"}
    extra = {k: v for k, v in output.items() if k not in known}
    if extra:
        st.markdown("**Other keys returned by the model**")
        st.json(extra, expanded=False)
    if output.get("research_trace"):
        with st.container(border=True):
            st.markdown("**Research trace (model's own account)**")
            for step in output["research_trace"]:
                st.markdown("- " + _short(step, 400))


def render_vehicle(result: dict, label: str, runs_dir: Path, cache: DocumentCache) -> None:
    icon = STATUS_ICON.get(result.get("status"), "•")
    with st.expander(f"{icon} {label} · {result.get('status')} · {result.get('duration_s')}s", expanded=False):
        tabs = st.tabs(["Human view", "JSON", "Evidence", "Tool calls", "Documents", "Level 1.5 input", "Events"])
        with tabs[0]:
            if result.get("error"):
                st.error(result["error"])
            _human_view(result.get("output"))
        with tabs[1]:
            st.caption(f"parse: {result.get('parse_note')}")
            if result.get("output") is not None:
                st.json(result["output"], expanded=True)
            st.text_area("Raw final text", result.get("raw_final_text") or "", height=200,
                         key=f"raw_{result.get('batch_id')}_{result.get('record_id')}")
        with tabs[2]:
            if result.get("evidence"):
                st.dataframe(pd.DataFrame(result["evidence"]), hide_index=True, width="stretch")
            else:
                st.caption("No evidence stored.")
        with tabs[3]:
            if result.get("tool_calls"):
                df = pd.DataFrame(result["tool_calls"])
                df["arguments"] = df["arguments"].map(lambda a: _short(a, 300))
                st.dataframe(df, hide_index=True, width="stretch")
            else:
                st.caption("No tool calls.")
        with tabs[4]:
            metas = [cache.get(d) for d in result.get("documents", [])]
            render_documents_table([m for m in metas if m])
        with tabs[5]:
            payload = load_input(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
            st.json(payload or {}, expanded=False)
        with tabs[6]:
            events = load_events(runs_dir, result.get("batch_id", ""), result.get("record_id", ""))
            st.caption(f"{len(events)} events in events.jsonl")
            kinds = sorted({e["kind"] for e in events})
            chosen = st.multiselect("Kinds", kinds, default=[k for k in kinds if k != "tool_result"],
                                    key=f"kinds_{result.get('batch_id')}_{result.get('record_id')}")
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


def render_documents_tab(cache: DocumentCache, results: list[dict]) -> None:
    all_docs = cache.list_documents()
    batch_docs = {d for r in results for d in r.get("documents", [])}
    st.caption(f"Shared cache: {len(all_docs)} documents · this batch touched {len(batch_docs)} · "
               f"session cache stats {cache.stats}")
    scope = st.radio("Show", ["This batch", "Entire cache"], horizontal=True, key="docs_scope")
    metas = [m for m in all_docs if scope == "Entire cache" or m["document_id"] in batch_docs]
    render_documents_table(metas)
    if metas:
        doc_id = st.selectbox("Preview document", [m["document_id"] for m in metas], key="doc_preview")
        meta = cache.get(doc_id) or {}
        st.json(meta, expanded=False)
        st.text_area("Extracted text", cache.read_text(doc_id)[:50000], height=300, key=f"doc_text_{doc_id}")


def render_results_tab(results: list[dict], labels: dict[str, str]) -> None:
    rows = []
    for result in results:
        for name, entry in iter_fields(result.get("output")):
            rows.append({"vehicle": labels.get(result["record_id"], result["record_id"]), "field": name,
                         "value": _short(entry.get("value", entry.get("values")), 200), "unit": entry.get("unit"),
                         "notes": _short(entry.get("notes") or "", 200),
                         "evidence": ", ".join(map(str, entry.get("evidence_ids") or []))})
    if not rows:
        st.caption("No structured fields in this batch yet.")
        return
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

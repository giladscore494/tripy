"""TRIPY research dashboard components (Streamlit).

Restrained, Streamlit-native layout: phase status instead of fake percentages, plain-language failures with
technical details in expanders, long text wrapped, nothing wider than the screen on a phone.
Every value shown comes from durable run state (run_state.json) or the engine's own events/results.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from ..runstate.model import (ACTIVE_STATUSES, CANCELLED, COMPLETED, FAILED, INTERRUPTED, STAGE_LABELS,
                              STATUS_LABELS, RunRecord)
from ..runstate.pipeline import RUNNING
from ..runstate.repository import parse_ts
from ..schemas import iter_fields

CSS = """
<style>
/* readable, contained, mobile-friendly */
.block-container {padding-top: 3.2rem; padding-bottom: 3rem; max-width: 1200px;}
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li,
[data-testid="stCaptionContainer"] {overflow-wrap: anywhere; word-break: break-word;}
pre, code {white-space: pre-wrap !important; overflow-wrap: anywhere;}
.tripy-brand {font-size: 1.9rem; font-weight: 700; letter-spacing: .02em; line-height: 1.1; margin: 0;}
.tripy-sub {opacity: .65; margin: .1rem 0 1.2rem 0;}
.tripy-kicker {font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
               opacity: .6; margin: 0 0 .2rem 0;}
.tripy-title {font-size: 1.15rem; font-weight: 600; margin: 0 0 .3rem 0; overflow-wrap: anywhere;}
.tripy-status {display: inline-block; font-weight: 600; margin: .1rem 0 .6rem 0;}
.tripy-status.active {color: #1f6feb;} .tripy-status.ok {color: #1a7f37;}
.tripy-status.bad {color: #cf222e;} .tripy-status.muted {opacity: .7;}
.tripy-pipeline {list-style: none; padding: 0; margin: .2rem 0 .8rem 0;}
.tripy-pipeline li {display: flex; gap: .6rem; align-items: baseline; padding: .18rem 0;}
.tripy-pipeline .icon {width: 1.1rem; text-align: center; flex: none;}
.tripy-pipeline .label {flex: 1 1 auto; min-width: 0;}
.tripy-pipeline .meta {opacity: .6; font-size: .85rem; white-space: nowrap;}
.tripy-pipeline li.running {font-weight: 600;} .tripy-pipeline li.running .icon {color: #1f6feb;}
.tripy-pipeline li.done .icon {color: #1a7f37;} .tripy-pipeline li.failed .icon {color: #cf222e;}
.tripy-pipeline li.waiting, .tripy-pipeline li.not_run, .tripy-pipeline li.skipped {opacity: .55;}
.tripy-pipeline .note {display: block; opacity: .6; font-size: .8rem; font-weight: 400;}
.tripy-facts {display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr)); gap: .4rem .9rem;
              margin: .2rem 0 .8rem 0;}
.tripy-facts div {min-width: 0;}
.tripy-facts .k {font-size: .75rem; opacity: .6; text-transform: uppercase; letter-spacing: .04em;}
.tripy-facts .v {font-size: 1.05rem; font-weight: 600; overflow-wrap: anywhere;}
.tripy-kv {display: grid; grid-template-columns: max-content 1fr; gap: .15rem .8rem; font-size: .92rem;}
.tripy-kv .k {opacity: .6;} .tripy-kv .v {overflow-wrap: anywhere; min-width: 0;}
.tripy-check {display: flex; justify-content: space-between; gap: .6rem; font-size: .92rem; padding: .08rem 0;}
.tripy-check .ok {color: #1a7f37;} .tripy-check .warning {color: #9a6700;} .tripy-check .error {color: #cf222e;}
.tripy-activity {opacity: .75; font-size: .9rem; margin: -.3rem 0 .6rem 0; overflow-wrap: anywhere;}
@media (max-width: 640px) {
  .block-container {padding-left: 1rem; padding-right: 1rem;}
  .tripy-brand {font-size: 1.6rem;}
}
</style>
"""

ENGINE_STATE_LABELS = {"ok": "Verified", "not_applicable": "Not applicable", "missing": "Not found",
                       "unresolved": "Unresolved", "conflicting": "Conflicting",
                       "foreign_market_only": "Foreign market only", "variant_not_exact": "Variant not exact",
                       "weak_provenance": "Weak source"}


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else "—"


# --- pure helpers (no Streamlit) ----------------------------------------------------------------------

def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def elapsed_s(record: RunRecord, now: datetime | None = None) -> float | None:
    """Active: wall time since start. Finished: the summed execution time of its jobs (the research run plus any
    finalization retry), so idle time between a failure and a retry is not counted."""
    jobs = [(parse_ts(j.get("started_at")), parse_ts(j.get("finished_at"))) for j in record.jobs or []]
    if not record.active and jobs and all(a and b for a, b in jobs):
        return sum(max(0.0, (b - a).total_seconds()) for a, b in jobs)
    start = parse_ts(record.started_at or record.created_at)
    if start is None:
        return None
    end = parse_ts(record.finished_at) if record.finished_at and not record.active else None
    end = end or now or datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def fmt_time(value: str | None) -> str:
    ts = parse_ts(value)
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "—"


def status_class(status: str) -> str:
    if status in ACTIVE_STATUSES:
        return "active"
    if status == COMPLETED:
        return "ok"
    if status == FAILED:
        return "bad"
    return "muted"


def status_text(record: RunRecord) -> str:
    if record.active:
        return f"● {STATUS_LABELS.get(record.status, record.status)}"
    icon = {COMPLETED: "✓", FAILED: "✕", CANCELLED: "■", INTERRUPTED: "■"}.get(record.status, "•")
    return f"{icon} {STATUS_LABELS.get(record.status, record.status)}"


def resolved_text(record: RunRecord) -> str | None:
    """'26 / 37' from the final report (sum over vehicles), else None."""
    vehicles = ((record.report or {}).get("vehicles") or {}).values()
    pairs = [(v.get("resolved_fields"), v.get("applicable_fields")) for v in vehicles]
    pairs = [(r, a) for r, a in pairs if r is not None and a]
    if not pairs:
        return None
    return f"{sum(r for r, _ in pairs)} / {sum(a for _, a in pairs)}"


def history_label(record: RunRecord) -> str:
    started = parse_ts(record.started_at or record.created_at)
    bits = [record.label, STATUS_LABELS.get(record.status, record.status)]
    if started:
        bits.insert(1, started.strftime("%b %d %H:%M"))
    duration = elapsed_s(record)
    if duration is not None and not record.active:
        bits.append(fmt_duration(duration))
    resolved = resolved_text(record)
    if resolved:
        bits.append(f"{resolved} fields")
    return " · ".join(bits)


def pretty_field(name: str) -> str:
    text = str(name).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def result_rows(result: dict) -> list[dict]:
    """The final result table: every field the model returned, with its evidence-backed state."""
    output = result.get("output")
    states = ((result.get("research_bundle") or {}).get("field_states") or {})
    rows = []
    for name, entry in iter_fields(output):
        value = entry.get("value", entry.get("values"))
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        state = (states.get(name) or {}).get("state")
        rows.append({"Field": pretty_field(name), "Value": "—" if value in (None, "") else str(value),
                     "Unit": entry.get("unit") or "", "Market": entry.get("market") or "",
                     "Evidence state": ENGINE_STATE_LABELS.get(state, state or "—"),
                     "Provenance": entry.get("provenance") or "",
                     "Evidence": len(entry.get("evidence_ids") or [])})
    order = {"Verified": 0, "Conflicting": 2}
    rows.sort(key=lambda r: (order.get(r["Evidence state"], 1), r["Value"] == "—"))
    return rows


# --- Streamlit components ---------------------------------------------------------------------------

def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def render_header() -> None:
    st.markdown('<div class="tripy-brand">TRIPY</div><div class="tripy-sub">AI Vehicle Research</div>',
                unsafe_allow_html=True)


def render_checks(checks: list, reach: tuple[bool, str] | None = None) -> None:
    rows = []
    for check in checks:
        status = check.status
        if check.name == "GLM" and check.level == "ok" and reach is not None:
            status = "Connected" if reach[0] else "Configured · unreachable"
        rows.append(f'<div class="tripy-check"><span>{esc(check.name)}</span>'
                    f'<span class="{esc(check.level)}">{esc(status)}</span></div>')
    st.markdown("".join(rows), unsafe_allow_html=True)
    problems = [c for c in checks if c.level != "ok" and c.detail]
    for check in problems:
        (st.error if check.level == "error" else st.warning)(f"**{check.name}:** {check.detail}")


def render_pipeline(view: dict) -> None:
    items = []
    for stage in view["stages"]:
        meta = ""
        if stage["state"] == RUNNING:
            meta = "Running" + (f" · {fmt_duration(stage['duration_s'])}" if stage["duration_s"] else "")
        elif stage["state"] == "done":
            meta = fmt_duration(stage["duration_s"]) if stage["duration_s"] is not None else "Done"
        else:
            meta = stage["state_label"]
        note = f'<span class="note">{esc(stage["note"])}</span>' if stage.get("note") else ""
        items.append(f'<li class="{esc(stage["state"])}"><span class="icon">{esc(stage["icon"])}</span>'
                     f'<span class="label">{esc(stage["label"])}{note}</span><span class="meta">{esc(meta)}</span></li>')
    st.markdown(f'<ul class="tripy-pipeline">{"".join(items)}</ul>', unsafe_allow_html=True)


def facts_html(pairs: list[tuple[str, object]]) -> str:
    cells = "".join(f'<div><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>'
                    for k, v in pairs if v is not None)
    return f'<div class="tripy-facts">{cells}</div>'


def counter_pairs(counters: dict) -> list[tuple[str, object]]:
    def frac(a, b):
        return f"{a} / {b}" if a is not None and b else (a if a is not None else None)

    return [("Sources", counters.get("sources")),
            ("Target-market sources", counters.get("target_market_sources")),
            ("Candidates", counters.get("candidates")),
            ("Candidate fields", frac(counters.get("candidate_fields"), counters.get("applicable_fields"))),
            ("Resolved fields", frac(counters.get("resolved_fields"), counters.get("applicable_fields"))),
            ("Research turns", counters.get("research_turns")),
            ("Searches", counters.get("searches"))]


def render_counters(counters: dict) -> None:
    st.markdown(facts_html(counter_pairs(counters)), unsafe_allow_html=True)


def _known(value):
    return "—" if value is None else value


def render_status_panel(record: RunRecord, views: list[dict]) -> None:
    """The right-hand 'current run' facts."""
    rows = status_panel_rows(record, views)
    body = "".join(f'<div class="k">{esc(k)}</div><div class="v">{esc(v)}</div>' for k, v in rows)
    st.markdown(f'<p class="tripy-kicker">Current run</p><div class="tripy-kv">{body}</div>', unsafe_allow_html=True)


def status_panel_rows(record: RunRecord, views: list[dict]) -> list[tuple]:
    """(label, value) rows of the status panel."""
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


def render_failure(info: dict, *, key: str, can_act: bool) -> str | None:
    """Clean failure card. Returns 'finalize' / 'restart' when a button was pressed."""
    lines = [f"**{info['title']}**", "", f"**Reason:** {info['reason']}"]
    if info.get("preserved"):
        lines += ["", info["preserved"]]
    st.error("\n".join(lines))
    clicked = None
    cols = st.columns(len(info["actions"]) or 1)
    for col, action in zip(cols, info["actions"]):
        label = info["finalize_label"] if action == "finalize" else "Restart research"
        help_text = info["finalize_note"] if action == "finalize" else "Starts a new run of the same target."
        if col.button(label, key=f"{action}_{key}", disabled=not can_act, help=help_text,
                      type="primary" if action == "finalize" else "secondary", width="stretch"):
            clicked = action
    with st.expander("Technical details"):
        st.code(info["technical"], language="json", wrap_lines=True)
    return clicked


def render_result(result: dict) -> None:
    output = result.get("output")
    if not isinstance(output, dict):
        return
    if output.get("summary"):
        st.markdown(str(output["summary"]))
    rows = result_rows(result)
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Evidence": st.column_config.NumberColumn(width="small")})
    conflicts = output.get("conflicts")
    if conflicts:
        with st.expander(f"Conflicts reported ({len(conflicts)})"):
            for item in conflicts:
                st.markdown("- " + (json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item))
    findings = output.get("additional_findings")
    if findings:
        with st.expander(f"Additional findings ({len(findings)})"):
            for item in findings:
                st.markdown("- " + (json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item))


def render_history(records: list[RunRecord], selected: str | None, *, limit: int = 25) -> str | None:
    """Sidebar run history. Returns the run id the user opened."""
    opened = None
    if not records:
        st.caption("No runs yet.")
        return None
    for record in records[:limit]:
        label = ("▸ " if record.run_id == selected else "") + history_label(record)
        # no hover tooltip: it overlays the next entry and swallows clicks in a dense list
        if st.button(label, key=f"hist_{record.run_id}", width="stretch"):
            opened = record.run_id
    if len(records) > limit:
        st.caption(f"{len(records) - limit} older run(s) not shown.")
    return opened


def poll_interval_s(record: RunRecord | None) -> float | None:
    return 2.0 if record is not None and record.active else None


def events_path(runs_dir: Path, run_id: str, record_id: str) -> Path:
    return Path(runs_dir) / run_id / str(record_id) / "events.jsonl"

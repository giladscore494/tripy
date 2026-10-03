"""TRIPY — AI Vehicle Research (Streamlit entrypoint).

    browser ── Streamlit (this script: renders durable state, never runs research itself)
                  │ start / cancel / retry
                  ▼
               RunManager (src/jobs/manager.py: background threads, one per run, process-wide)
                  │ research_one / run_batch / finalize_existing_run (unchanged research engine)
                  ▼
               durable state under TRIPY_DATA_DIR: runs/<run_id>/run_state.json, batch.json, per-vehicle
               events.jsonl / result.json (checkpoint), shared cache/

Every browser session discovers runs from disk; st.session_state only remembers UI conveniences (the
submission nonce). A browser refresh, a closed tab or a websocket drop never stops a run.
"""

from __future__ import annotations

import os
import uuid

import streamlit as st

from src import access_control
from src.agent import PROMPT_VERSION
from src.app_config import (Check, allow_ui_api_key, blocking_errors, endpoint_reachable,
                            redact, validate_config)
from src.benchmark import HANDSHAKE_RECORD_ID, benchmark_vehicles, manufacturers, select_vehicles, vehicle_label
from src.db import Level15Error
from src.glm_client import GLMError
from src.jobs.manager import ResearchRequest, RunRejected, get_manager, shared_controller
from src.runstate.failures import explain
from src.runstate.model import COMPLETED, STATUS_LABELS
from src.runstate.pipeline import PipelineCache
from src.runstate.report import vehicle_report
from src.storage.paths import resolve_paths
from src.storage.run_loader import load_runs
from src.ui import benchmark_view, diagnostics_view, run_view
from src.ui import dashboard as ui
from src.ui.settings_panel import render_settings


def secret(name: str, default: str = "") -> str:
    """Environment first, then Streamlit secrets (if any are configured)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def vehicle_title(v: dict) -> str:
    return " · ".join(str(x) for x in (f"{v['manufacturer']} {v['model']}", v.get("year"), v.get("trim")) if x)


@st.cache_resource
def pipelines() -> PipelineCache:
    """Incremental event readers shared by every browser session of this process."""
    return PipelineCache()


st.set_page_config(page_title="TRIPY · AI Vehicle Research", page_icon="🔎", layout="wide")
ui.inject_css()

# Access control FIRST (src/access_control.py): in production nothing below runs (no paths, no RunManager, no run
# history, no settings, no Level 1.5 or GLM access) until this browser session has unlocked with TRIPY_ACCESS_TOKEN.
if not access_control.gate(secret):
    st.stop()

paths = resolve_paths(secret)
vehicles = benchmark_vehicles()
vehicles_by_id = {v["upstream_record_id"]: v for v in vehicles}
labels = {v["upstream_record_id"]: vehicle_label(v) for v in vehicles}
titles = {v["upstream_record_id"]: vehicle_title(v) for v in vehicles}
manager = get_manager(paths, controller=shared_controller(secret), vehicle_label=lambda rid: titles.get(rid, rid))
manager.watch_server_shutdown()
manager.maybe_reconcile()
st.session_state.setdefault("submit_nonce", uuid.uuid4().hex)

# --- sidebar: history and advanced settings -------------------------------------------------------------

records = manager.list_runs()
by_id = {r.run_id: r for r in records}
requested_run = st.query_params.get("run")
active_ids = [r.run_id for r in records if r.active]
default_run = active_ids[0] if active_ids else (records[0].run_id if records else None)
selected_id = requested_run if requested_run in by_id else default_run


def history_panel() -> None:
    """Run history from the durable repository (refreshes itself while any run is active)."""
    st.markdown("### Run history")
    fresh = manager.list_runs()
    opened = ui.render_history(fresh, selected_id)
    if opened and opened != requested_run:
        st.query_params["run"] = opened
        st.rerun()


with st.sidebar:
    access_control.render_logout(secret)
    st.fragment(run_every=5 if active_ids else None)(history_panel)()
    diagnostics_view.render_benchmark_export(paths.runs_dir, [r.run_id for r in records],
                                             {r.run_id: ui.history_label(r) for r in records})
    st.divider()
    settings = render_settings(secret, manager.controller, allow_ui_key=allow_ui_api_key(secret))

# --- configuration validation ----------------------------------------------------------------------------


def effective_lookup(name: str) -> str:
    """What the next run will use: the settings panel's model/key over the environment (presence only)."""
    if name == "GLM_MODEL":
        return settings.model_id
    if name == "GLM_API_KEY":
        return "set" if settings.api_key else ""
    if name == "SEARCH_BACKEND":
        return settings.search_backend
    return secret(name)


checks = validate_config(effective_lookup, paths)
if settings.extra_error:
    checks.append(Check("Configuration", "error", "Invalid value", settings.extra_error))
blocking = blocking_errors(checks)
reach = endpoint_reachable(settings.base_url) if settings.api_key and not blocking else None

# --- header, request and system status -------------------------------------------------------------------

ui.render_header()
left, right = st.columns([3, 2], gap="large")
with left:
    st.markdown('<p class="tripy-kicker">Research target</p>', unsafe_allow_html=True)
    mode_label = st.radio("Scope", ["One vehicle", "Manufacturer", "All 50"], horizontal=True,
                          label_visibility="collapsed", key="scope")
    if mode_label == "One vehicle":
        chosen_id = st.selectbox("Vehicle", list(labels), format_func=labels.get, key="vehicle",
                                 index=list(labels).index(HANDSHAKE_RECORD_ID))
        selection = select_vehicles(vehicles, "one", chosen_id)
        target_label = titles[chosen_id]
    elif mode_label == "Manufacturer":
        maker = st.selectbox("Manufacturer", manufacturers(vehicles), key="maker")
        selection = select_vehicles(vehicles, "manufacturer", maker)
        target_label = f"{maker} · {len(selection)} vehicles"
    else:
        selection = select_vehicles(vehicles, "all")
        target_label = f"Benchmark v1 · {len(selection)} vehicles"
    target_ids = {v["upstream_record_id"] for v in selection}
    busy = next((r for r in records if r.active and set(r.record_ids) & target_ids), None)
    start_clicked = st.button("Start research", type="primary", width="stretch",
                              disabled=bool(blocking or not selection or busy))
    st.caption(f"{len(selection)} vehicle(s) · prompt version {PROMPT_VERSION}"
               + (" · a run for this target is already active" if busy else ""))
with right:
    st.markdown('<p class="tripy-kicker">System</p>', unsafe_allow_html=True)
    ui.render_checks(checks, reach)
    if settings.api_key_from_ui:
        st.caption("Using an API key typed in this session (development only).")


def research_request(vehicle_list: list[dict], label: str, scope: str, key: str | None) -> ResearchRequest:
    return ResearchRequest(vehicles=vehicle_list, label=label, scope=scope, settings=settings.glm_settings(),
                           agent_cfg=settings.agent_config(secret), tool_cfg=settings.tool_config(secret),
                           pricing=settings.pricing, prompt_version=PROMPT_VERSION, data_source=settings.data_source,
                           dsn=settings.dsn, workers=settings.workers, chat_limits=settings.chat_limits,
                           search_limit=settings.search_limit, idempotency_key=key)


def launch(request: ResearchRequest) -> None:
    """Start a run (idempotent per submission) and open it. Errors are shown cleanly, never as tracebacks."""
    try:
        started = manager.start(request)
    except RunRejected as exc:
        st.warning(str(exc))
        if exc.existing_run_id:
            st.query_params["run"] = exc.existing_run_id
        return
    except (GLMError, Level15Error, ValueError) as exc:
        st.error(redact(f"Could not start the run: {exc}"))
        return
    st.session_state["submit_nonce"] = uuid.uuid4().hex
    st.query_params["run"] = started.run_id
    st.rerun()


if start_clicked:
    with left:
        launch(research_request(selection, target_label, mode_label, st.session_state["submit_nonce"]))

st.divider()

# --- the selected run (auto-refreshing while it is active) -------------------------------------------------


def _vehicle_views(record) -> list[dict]:
    out = []
    for item in record.target.get("vehicles") or [{"record_id": r, "label": r} for r in record.record_ids]:
        rid = str(item["record_id"])
        pipeline = pipelines().get(ui.events_path(paths.runs_dir, record.run_id, rid), rid,
                                   item.get("label") or titles.get(rid, rid))
        view = pipeline.view()
        view["_pipeline"] = pipeline
        out.append(view)
    return out


def _render_vehicle_live(view: dict) -> None:
    if view.get("activity"):
        st.markdown(f'<p class="tripy-activity">{ui.esc(view["activity"])}</p>', unsafe_allow_html=True)
    ui.render_pipeline(view)
    ui.render_counters(view["counters"])


def _failure_of(record, view: dict, result: dict | None) -> dict | None:
    rid = view["record_id"]
    vstate = (record.vehicles.get(rid) or {}).get("status")
    return explain(result, view, run_status=vstate, run_error=record.error if len(record.record_ids) == 1 else None,
                   can_finalize=not record.legacy)


def _render_failure(record, view: dict, info: dict) -> None:
    rid = view["record_id"]
    action = ui.render_failure(info, key=f"{record.run_id}_{rid}", can_act=not record.active)
    if action == "finalize":
        try:
            manager.retry_finalization(record.run_id, rid, settings.glm_settings())
        except (RunRejected, GLMError) as exc:
            st.warning(redact(str(exc)))
        else:
            st.rerun()
    elif action == "restart":
        vehicle = vehicles_by_id.get(rid)
        if vehicle is None:
            st.warning("This vehicle is not in the benchmark sample any more.")
        else:
            launch(research_request([vehicle], titles.get(rid, rid), "One vehicle", f"restart:{uuid.uuid4().hex}"))


def _render_vehicle_result(view: dict, result: dict | None, failed: bool) -> None:
    if not result:
        return
    if not failed:
        counters = view["counters"]
        if counters.get("applicable_fields") and not counters.get("resolved_fields"):
            st.info("Insufficient evidence: no requested field reached an evidence-backed state. The research trace "
                    "is under Technical details.")
        if (result.get("primary_research") or {}).get("stop_reason") == "hard_max_turns_under_acquired":
            st.warning("Few usable sources were found for this vehicle; the result may be thin.")
        if result.get("recovered"):
            st.caption("Finalized from the preserved research (no web research was repeated).")
    if result.get("output") is not None:
        st.markdown('<p class="tripy-kicker">Result</p>', unsafe_allow_html=True)
        ui.render_result(result)


def _render_report(record, views: list[dict], results_by_id: dict[str, dict]) -> None:
    saved = ((record.report or {}).get("vehicles") or {})
    for view in views:
        rid = view["record_id"]
        report = saved.get(rid) or vehicle_report(results_by_id.get(rid), view["_pipeline"])
        if len(views) > 1:
            st.markdown(f"**{ui.esc(view['title'])}**")
        durations = report.get("durations_s") or {}
        rows = [("Total duration", ui.fmt_duration(report.get("total_duration_s")))]
        rows += [(f"{label}", ui.fmt_duration(durations.get(key)))
                 for key, label in (("acquisition", "Acquisition"), ("harvest", "Harvest"), ("sweep", "Sweep"),
                                    ("recovery", "Recovery"), ("finalization", "Finalizer"))]
        by_stage = report.get("model_calls_by_stage") or {}
        rows += [("Search calls", report.get("search_calls")),
                 ("Model calls", " · ".join(f"{k} {v}" for k, v in by_stage.items() if v) or report.get("model_calls")),
                 ("Fetched documents", report.get("fetched_documents")),
                 ("Official documents", report.get("official_documents")),
                 ("Target-market documents", report.get("target_market_documents")),
                 ("Candidates", report.get("candidate_count")), ("Candidate fields", report.get("candidate_fields")),
                 ("Evidence admitted", report.get("evidence_admitted")),
                 ("Evidence rejected", report.get("evidence_rejected")),
                 ("Resolved fields", f"{report.get('resolved_fields')} / {report.get('applicable_fields')}"
                  if report.get("applicable_fields") else None),
                 ("Stop reason", report.get("stop_reason")),
                 ("Recorded cost", f"${report['cost_usd']:.4f}" if report.get("cost_usd") is not None else None)]
        body = "".join(f'<div class="k">{ui.esc(k)}</div><div class="v">{ui.esc(v)}</div>'
                       for k, v in rows if v is not None)
        st.markdown(f'<div class="tripy-kv">{body}</div>', unsafe_allow_html=True)


def _render_technical(record, views: list[dict], results: list[dict]) -> None:
    with st.expander("Technical details"):
        st.caption(f"Run {record.run_id} · data in {paths.runs_dir / record.run_id}")
        for view in views:      # acquisition / document-sweep diagnostics (src/diagnostics.py; observational)
            if len(views) > 1:
                st.markdown(f"**{ui.esc(view['title'])}**")
            diag = diagnostics_view.diagnostics_for(paths.runs_dir / record.run_id / view["record_id"])
            diagnostics_view.render_brief(view, diag)
            diagnostics_view.render_detailed(diag)
        if record.active:
            for view in views:
                live = view["_pipeline"].live
                if len(views) > 1:
                    st.markdown(f"**{ui.esc(view['title'])}**")
                rows = live.field_rows()
                if rows:
                    import pandas as pd
                    from src.ui import labels_he as he
                    st.dataframe(pd.DataFrame(rows, columns=he.FIELD_TABLE_COLUMNS_HE[::-1]), hide_index=True,
                                 width="stretch")
                st.code("\n".join(list(live.lines)[-60:]) or "—", language=None, wrap_lines=True)
            return
        tab_run, tab_docs, tab_results, tab_bench, tab_state = st.tabs(
            ["Diagnostics", "Documents", "Results", "Benchmark", "Run state"])
        with tab_run:
            if not results:
                st.caption("No vehicle run folder has input.json, events.jsonl or result.json yet.")
            incomplete = [r for r in results if r.get("synthesized")]
            if incomplete:
                st.warning(f"{len(incomplete)} run(s) did not produce a final result.json; they are shown from their "
                           "recovered research trace.")
            for result in results:
                run_view.render_vehicle(result, labels.get(result["record_id"], result["record_id"]), paths.runs_dir,
                                        manager.cache)
        with tab_docs:
            run_view.render_documents_tab(manager.cache, results, paths.runs_dir)
        with tab_results:
            run_view.render_results_tab(results, labels, paths.runs_dir, manager.cache)
        with tab_bench:
            benchmark_view.render_benchmark(results, vehicles_by_id, labels, manager.cache, paths.runs_dir)
        with tab_state:
            st.json({k: v for k, v in record.to_dict().items() if k != "report"}, expanded=False)


def run_panel(run_id: str | None) -> None:
    record = manager.get(run_id) if run_id else None
    if record is None:
        st.caption("No research yet. Choose a target and press Start research; progress appears here and survives "
                   "page refreshes.")
        return
    seen_key = f"seen_status_{record.run_id}"
    previous = st.session_state.get(seen_key)
    st.session_state[seen_key] = record.status
    if previous is not None and previous != record.status and not record.active:
        st.rerun()             # the run just finished: rebuild the whole page (result, history, polling off)
    views = _vehicle_views(record)
    results = [] if record.active else load_runs(paths.runs_dir, record.run_id, cache=manager.cache)
    results_by_id = {str(r.get("record_id")): r for r in results}
    main, side = st.columns([3, 2], gap="large")
    with main:
        st.markdown(f'<p class="tripy-kicker">{"Active run" if record.active else "Run"}</p>'
                    f'<p class="tripy-title">{ui.esc(record.label)}</p>'
                    f'<span class="tripy-status {ui.status_class(record.status)}">{ui.esc(ui.status_text(record))}'
                    f'</span>', unsafe_allow_html=True)
        for note in record.notes:
            if not str(note).startswith("reconciled"):
                st.caption(str(note))
        if record.active and manager.is_executing(record.run_id):
            if st.button("Stop run", key=f"stop_{record.run_id}",
                         help="Stops at the next safe point; everything completed so far is preserved."):
                manager.cancel(record.run_id)
                st.rerun()
        many = len(views) > 1
        for i, view in enumerate(views):
            rid = view["record_id"]
            vstatus = (record.vehicles.get(rid) or {}).get("status") or record.status
            container = st.expander(f"{view['title']} · {STATUS_LABELS.get(vstatus, vstatus)}",
                                    expanded=(i == 0)) if many else st.container()
            with container:
                result = None if record.active else results_by_id.get(rid)
                failure = None if record.active else _failure_of(record, view, result)
                if failure is not None:
                    _render_failure(record, view, failure)
                _render_vehicle_live(view)
                if not record.active:
                    _render_vehicle_result(view, result, failed=failure is not None)
        if not record.active and record.status != COMPLETED and record.error and not views:
            st.error(f"**{record.error.get('message')}**")
    with side:
        ui.render_status_panel(record, views)
        if not record.active:
            with st.expander("Run report"):
                _render_report(record, views, results_by_id)
    _render_technical(record, views, results)


interval = ui.poll_interval_s(by_id.get(selected_id) if selected_id else None)
st.fragment(run_every=interval)(run_panel)(selected_id)

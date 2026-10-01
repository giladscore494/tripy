"""MILO — GLM Web Research Benchmark v1 (Streamlit shell).

Supabase Level 1.5 -> benchmark loader -> GLM agent loop -> research tools ->
evidence store -> structured result -> this UI. One process, no backend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st

from src.agent import PROMPT_VERSION, AgentConfig, run_vehicle
from src.benchmark import (benchmark_vehicles, compute_metrics, manufacturers, run_batch, select_vehicles,
                           vehicle_label)
from src.db import Level15Error, build_level15_payload, database_url, load_level15
from src.glm_client import (DEFAULT_BASE_URL, DEFAULT_CHAT_PATH, DEFAULT_SEARCH_ENGINE, DEFAULT_SEARCH_PATH,
                            GLMClient, GLMError, GLMSettings)
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, list_batches, load_results, new_batch_id, write_batch
from src.tools import ToolConfig
from src.ui import benchmark_view, run_view

ROOT = Path(__file__).resolve().parent
RUNS_DIR = Path(os.environ.get("MILO_RUNS_DIR", ROOT / "runs"))
CACHE_DIR = Path(os.environ.get("MILO_CACHE_DIR", RUNS_DIR / "_cache"))


def secret(name: str, default: str = "") -> str:
    """Environment first, then Streamlit secrets (if any are configured)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def float_secret(name: str) -> float:
    try:
        return float(secret(name, "0") or 0)
    except ValueError:
        return 0.0


@st.cache_resource
def shared_cache(path: str) -> DocumentCache:
    return DocumentCache(path)


st.set_page_config(page_title="MILO GLM Benchmark v1", page_icon="🔎", layout="wide")
cache = shared_cache(str(CACHE_DIR))
vehicles = benchmark_vehicles()
vehicles_by_id = {v["upstream_record_id"]: v for v in vehicles}
labels = {v["upstream_record_id"]: vehicle_label(v) for v in vehicles}

# --- Sidebar: configuration ----------------------------------------------------

with st.sidebar:
    st.header("GLM")
    model_id = st.text_input("Model id", value=secret("GLM_MODEL"),
                             help="Any GLM chat model id. Nothing in the code assumes a specific model.")
    base_url = st.text_input("API base URL", value=secret("GLM_BASE_URL", DEFAULT_BASE_URL))
    chat_path = st.text_input("Chat endpoint", value=secret("GLM_CHAT_PATH", DEFAULT_CHAT_PATH),
                              help="Path relative to the base URL, or a full URL.")
    env_key = secret("GLM_API_KEY")
    api_key = env_key or st.text_input("API key", type="password", help="Or set GLM_API_KEY.")
    st.caption("API key: " + ("from environment/secrets" if env_key else "entered here" if api_key else "missing"))
    search_backend = st.selectbox("search_web backend", ["glm", "duckduckgo"],
                                  index=0 if secret("SEARCH_BACKEND", "glm") == "glm" else 1,
                                  help="glm = GLM web_search API; duckduckgo = keyless HTML search.")
    search_path = st.text_input("GLM search endpoint", value=secret("GLM_SEARCH_PATH", DEFAULT_SEARCH_PATH),
                                disabled=search_backend != "glm", help="Path relative to the base URL, or a full URL.")
    search_engine = st.text_input("GLM search engine", value=secret("GLM_SEARCH_ENGINE", DEFAULT_SEARCH_ENGINE),
                                  disabled=search_backend != "glm")

    st.header("Agent")
    max_steps = st.slider("Max model steps per vehicle", 5, 80, int(secret("AGENT_MAX_STEPS", "30") or 30))
    tool_chars = st.number_input("Max chars per tool result sent to model", 2000, 60000, 12000, step=1000)
    include_level3 = st.checkbox("Include Level 3 open research", value=True)
    use_temp = st.checkbox("Set temperature")
    temperature = st.slider("Temperature", 0.0, 1.5, 0.6, 0.05, disabled=not use_temp) if use_temp else None
    max_tokens = st.number_input("max_tokens per model turn (0 = provider default)", 0, 131072, 0, step=1024)
    extra_raw = st.text_area("Extra request JSON (merged into every chat payload)", value=secret("GLM_EXTRA_BODY", ""),
                             placeholder='{"thinking": {"type": "enabled"}}', height=80)

    st.header("Cost")
    price_in = st.number_input("USD per 1M input tokens", 0.0, 100.0, float_secret("GLM_PRICE_INPUT_PER_MTOK"),
                               step=0.05, format="%.3f")
    price_out = st.number_input("USD per 1M output tokens", 0.0, 100.0, float_secret("GLM_PRICE_OUTPUT_PER_MTOK"),
                                step=0.05, format="%.3f")

    st.header("Level 1.5 data")
    dsn = secret("DATABASE_URL") or database_url()
    data_source = st.selectbox("Source", ["auto", "database", "snapshot"],
                               help="auto = Supabase when DATABASE_URL is set, else the frozen snapshot.")
    st.caption("DATABASE_URL: " + ("configured" if dsn else "not set → snapshot"))

    st.header("View")
    batches = list_batches(RUNS_DIR)
    batch_ids = [b["batch_id"] for b in batches]
    current = st.session_state.get("batch_id")
    if current and current not in batch_ids:
        batch_ids.insert(0, current)
    view_batch = st.selectbox("Batch", batch_ids, index=batch_ids.index(current) if current in batch_ids else 0,
                              placeholder="No runs yet") if batch_ids else None

# --- Header and selection ---------------------------------------------------------

st.title("MILO — GLM Web Research Benchmark v1")
st.caption("How far can GLM get with the full Level 1.5 record and strong web tools? "
           "No verifier, no domain allowlist, no confidence gate: everything the model does is logged and shown.")

mode_label = st.radio("Scope", ["One vehicle", "Manufacturer", "All 50"], horizontal=True)
if mode_label == "One vehicle":
    chosen_id = st.selectbox("Vehicle", list(labels), format_func=labels.get)
    selection = select_vehicles(vehicles, "one", chosen_id)
elif mode_label == "Manufacturer":
    maker = st.selectbox("Manufacturer", manufacturers(vehicles))
    selection = select_vehicles(vehicles, "manufacturer", maker)
else:
    selection = select_vehicles(vehicles, "all")
st.caption(f"{len(selection)} vehicle(s) selected · prompt version {PROMPT_VERSION}")

extra_body: dict = {}
extra_error = ""
if extra_raw.strip():
    try:
        extra_body = json.loads(extra_raw)
        if not isinstance(extra_body, dict):
            extra_error, extra_body = "Extra request JSON must be an object.", {}
    except ValueError as exc:
        extra_error = f"Extra request JSON is invalid: {exc}"
if extra_error:
    st.error(extra_error)

missing = [name for name, ok in (("model id", model_id), ("API key", api_key)) if not ok]
if missing:
    st.info("To run research, provide: " + ", ".join(missing) + ".")
run_clicked = st.button("Run Research", type="primary", disabled=bool(missing or extra_error or not selection))

# --- Run -----------------------------------------------------------------------------

if run_clicked:
    try:
        load = load_level15([v["upstream_record_id"] for v in selection], source=data_source, dsn=dsn)
    except Level15Error as exc:
        st.error(str(exc))
        st.stop()
    if load.missing:
        st.warning(f"Level 1.5 rows not found for: {', '.join(load.missing)}")
    try:
        client = GLMClient(GLMSettings(api_key=api_key, base_url=base_url, model=model_id,
                                       chat_path=chat_path or DEFAULT_CHAT_PATH,
                                       search_path=search_path or DEFAULT_SEARCH_PATH,
                                       search_engine=search_engine or DEFAULT_SEARCH_ENGINE))
    except GLMError as exc:
        st.error(str(exc))
        st.stop()

    batch_id = new_batch_id(f"{model_id}-{mode_label.split()[0].lower()}")
    agent_cfg = AgentConfig(max_steps=int(max_steps), max_tool_output_chars=int(tool_chars),
                            temperature=temperature, max_tokens=int(max_tokens) or None,
                            include_level3=include_level3, extra_body=extra_body)
    tool_cfg = ToolConfig(search_backend=search_backend)
    write_batch(RUNS_DIR, batch_id, {
        "batch_id": batch_id, "model": model_id, "base_url": base_url, "chat_path": chat_path,
        "prompt_version": PROMPT_VERSION,
        "search_backend": search_backend, "search_engine": search_engine if search_backend == "glm" else None,
        "search_path": search_path if search_backend == "glm" else None,
        "level15_source": load.source, "level15_note": load.note, "selection": mode_label,
        "record_ids": [v["upstream_record_id"] for v in selection],
        "agent_config": agent_cfg.__dict__, "tool_config": tool_cfg.__dict__,
    })
    st.session_state["batch_id"] = batch_id
    rows_by_id = {str(r["upstream_record_id"]): r for r in load.rows}
    progress = st.progress(0.0, text=f"Batch {batch_id} · Level 1.5 from {load.source}")
    done = {"n": 0}

    def run_one(vehicle: dict, row: dict) -> dict:
        with st.status(labels[vehicle["upstream_record_id"]], expanded=True) as status_box:
            feed = st.empty()
            log = RunLog(RUNS_DIR, batch_id, vehicle["upstream_record_id"],
                         listener=run_view.live_listener(feed))
            result = run_vehicle(row, build_level15_payload(row), client=client, cache=cache, run_log=log,
                                 config=agent_cfg, tool_config=tool_cfg, vehicle_meta=vehicle,
                                 batch_id=batch_id, ordinal=vehicle["ordinal"])
            result["metrics"] = compute_metrics(result, vehicle, cache, price_in, price_out)
            result["level15_source"] = load.source
            log.write_result(result)
            m = result["metrics"]
            status_box.update(
                label=f"{labels[vehicle['upstream_record_id']]} · {result['status']} · {m['fields_with_value']} fields"
                      f" · {m['tool_calls']} tools · {result['duration_s']}s",
                state="error" if result["status"] == "error" else "complete", expanded=False)
        return result

    def on_done(vehicle: dict, result: dict) -> None:
        done["n"] += 1
        progress.progress(done["n"] / len(selection), text=f"{done['n']}/{len(selection)} done")

    run_batch(selection, rows_by_id, run_one, on_done=on_done)
    view_batch = batch_id

# --- Views ---------------------------------------------------------------------------

results = load_results(RUNS_DIR, view_batch) if view_batch else []
if view_batch:
    st.divider()
    st.subheader(f"Batch {view_batch}")
tab_run, tab_docs, tab_results, tab_bench = st.tabs(["Run", "Documents", "Results", "Benchmark"])
with tab_run:
    if not results:
        st.caption("No results to show yet. Choose a scope and press Run Research.")
    for result in results:
        run_view.render_vehicle(result, labels.get(result["record_id"], result["record_id"]), RUNS_DIR, cache)
with tab_docs:
    run_view.render_documents_tab(cache, results)
with tab_results:
    run_view.render_results_tab(results, labels)
with tab_bench:
    benchmark_view.render_benchmark(results, vehicles_by_id, labels, cache, RUNS_DIR, price_in, price_out)

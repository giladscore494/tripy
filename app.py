"""MILO — GLM Web Research Benchmark v1 (Streamlit shell).

Supabase Level 1.5 -> benchmark loader -> GLM agent loop -> research tools ->
evidence store -> structured result -> this UI. One process, no backend.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import streamlit as st

from src.agent import PROMPT_VERSION, agent_config_from_env, tool_config_from_env
from src.benchmark import (HANDSHAKE_RECORD_ID, batch_observability, benchmark_vehicles, manufacturers,
                           research_one, select_vehicles, start_batch, vehicle_client_factory, vehicle_label)
from src.concurrency import (DEFAULT_SEARCH_MAX_INFLIGHT, SEARCH_PRIME_PROVIDER_LIMIT, ConcurrencyController,
                             batch_max_workers_from_env)
from src.db import Level15Error, database_url, load_level15
from src.glm_client import (DEFAULT_BASE_URL, DEFAULT_CHAT_MAX_ATTEMPTS, DEFAULT_CHAT_PATH, DEFAULT_CHAT_TIMEOUT_S,
                            DEFAULT_SEARCH_ENGINE, DEFAULT_SEARCH_MAX_ATTEMPTS, DEFAULT_SEARCH_PATH, GLMError,
                            GLMSettings)
from src.storage.cache import DocumentCache
from src.pricing import default_pricing
from src.storage.run_loader import load_runs
from src.storage.run_log import list_batches, load_batch, new_batch_id, update_batch
from src.ui import benchmark_view, run_view
from src.ui.live_dashboard import run_live_batch
from src.ui.live_state import BatchLiveState

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


def pricing_defaults(model: str) -> dict:
    """Official defaults for the model id, then env / Streamlit-secrets overrides."""
    pricing = default_pricing(model)
    for name, key in (("GLM_PRICE_INPUT_PER_MTOK", "input_per_mtok"), ("GLM_PRICE_OUTPUT_PER_MTOK", "output_per_mtok"),
                      ("GLM_PRICE_WEB_SEARCH_PER_CALL", "web_search_per_call")):
        raw = secret(name).strip()
        if raw:
            try:
                pricing[key] = float(raw)
                pricing["source"] = "environment override"
            except ValueError:
                pass
    return pricing


@st.cache_resource
def shared_cache(path: str) -> DocumentCache:
    return DocumentCache(path)


@st.cache_resource
def shared_controller() -> ConcurrencyController:
    """One controller per process: Z.ai limits are per account, so every batch/session shares the pools."""
    return ConcurrencyController.from_env(secret)


def vehicle_title(v: dict) -> str:
    return " · ".join(str(x) for x in (f"{v['manufacturer']} {v['model']}", v.get("year"), v.get("trim")) if x)


st.set_page_config(page_title="MILO GLM Benchmark v1", page_icon="🔎", layout="wide")
cache = shared_cache(str(CACHE_DIR))
vehicles = benchmark_vehicles()
vehicles_by_id = {v["upstream_record_id"]: v for v in vehicles}
labels = {v["upstream_record_id"]: vehicle_label(v) for v in vehicles}

# --- Sidebar: configuration ----------------------------------------------------

with st.sidebar:
    st.header("GLM")
    model_id = st.text_input("Research model id", value=secret("GLM_MODEL"),
                             help="GLM_MODEL, e.g. glm-5.3-flash. Used for every research/tool turn and sent "
                                  "unchanged as the API `model`.")
    finalizer_model_id = st.text_input("Finalizer model id (optional)", value=secret("GLM_FINALIZER_MODEL"),
                                       help="GLM_FINALIZER_MODEL. Used only for the compact finalization call; "
                                            "empty = the research model.")
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

    chat_attempts = st.number_input("Chat max attempts (total, 1 = no retry)", 1, 6,
                                    int(secret("GLM_CHAT_MAX_ATTEMPTS") or DEFAULT_CHAT_MAX_ATTEMPTS),
                                    help="GLM_CHAT_MAX_ATTEMPTS. Each attempt is logged; a timed-out attempt may "
                                         "still be billed by the provider.")
    search_attempts = st.number_input("Search max attempts (total)", 1, 6,
                                      int(secret("GLM_SEARCH_MAX_ATTEMPTS") or DEFAULT_SEARCH_MAX_ATTEMPTS))
    chat_timeout = st.number_input("Chat read timeout per attempt (s)", 30, 1800,
                                   int(float(secret("GLM_CHAT_TIMEOUT_S") or DEFAULT_CHAT_TIMEOUT_S)), step=30)

    st.header("Concurrency")
    controller = shared_controller()
    workers = st.number_input("Vehicle workers (vehicles researched at once)", 1, 50,
                              min(50, batch_max_workers_from_env(secret)),
                              help="BATCH_MAX_WORKERS (default 50). Not a request limit: the model and search "
                                   "pools below guard every HTTP request.")

    def chat_limit_input(model: str, label: str) -> int:
        provider = controller.provider_limit_for(model)
        current = controller.limit_for(model)
        value = st.number_input(f"{label} in-flight requests ({model})", 1, provider or 50, min(current, provider or 50),
                                key=f"chat_limit_{model}",
                                help="Concurrent chat/completions requests for this exact model id. A slot is held "
                                     "only while an HTTP attempt is in flight.")
        st.caption(f"configured operational limit: {value} · provider limit: "
                   + (str(provider) if provider else "unknown model (conservative fallback "
                      f"{controller.unknown_model_limit}; check the account limit)"))
        return int(value)

    chat_limits = {}
    if model_id:
        chat_limits[model_id] = chat_limit_input(model_id, "Research model")
    if finalizer_model_id.strip() and finalizer_model_id.strip().lower() != model_id.strip().lower():
        chat_limits[finalizer_model_id.strip()] = chat_limit_input(finalizer_model_id.strip(), "Finalizer model")
    search_limit = st.number_input("Search-Prime in-flight requests", 1, SEARCH_PRIME_PROVIDER_LIMIT,
                                   min(controller.search.limit, SEARCH_PRIME_PROVIDER_LIMIT) or DEFAULT_SEARCH_MAX_INFLIGHT,
                                   help="GLM_SEARCH_MAX_INFLIGHT (provider limit 5).")
    st.caption(f"configured operational limit: {search_limit} · provider limit: {SEARCH_PRIME_PROVIDER_LIMIT}")

    st.header("Agent")
    env_agent = agent_config_from_env(env=secret)
    max_steps = st.slider("Research budget (model turns per vehicle)", 3, 80, int(env_agent.max_steps),
                          help="AGENT_MAX_STEPS. When reached, research stops and a compact finalization runs.")
    idle_turns = st.number_input("Finalize after N turns with no new research artifact (0 = off)", 0, 20,
                                 int(env_agent.no_new_research_turns), help="AGENT_NO_NEW_RESEARCH_TURNS")
    tool_chars = st.number_input("Max chars per tool result sent to model", 1000, 60000,
                                 int(env_agent.max_tool_output_chars), step=500)
    recovery_on = st.checkbox("Targeted retry of failed requested fields", value=env_agent.field_recovery_enabled,
                              help="FIELD_RECOVERY_ENABLED. After primary research, each requested enrichment field "
                                   "without a usable candidate gets a focused retry before finalization.")
    recovery_attempts = st.number_input("Retry attempts per failed field", 0, 5,
                                        int(env_agent.field_recovery_max_attempts), disabled=not recovery_on,
                                        help="FIELD_RECOVERY_MAX_ATTEMPTS (a field's own recovery_attempts wins)")
    recovery_steps = st.number_input("Model turns per retry attempt", 1, 20, int(env_agent.field_recovery_max_steps),
                                     disabled=not recovery_on, help="FIELD_RECOVERY_MAX_STEPS")
    recovery_total = st.number_input("Total recovery turns per vehicle (0 = no cap)", 0, 400,
                                     int(env_agent.field_recovery_max_total_steps), disabled=not recovery_on,
                                     help="FIELD_RECOVERY_MAX_TOTAL_STEPS (default 24): hard cap across all fields, "
                                          "attempts and turns; the run still finalizes when it is reached.")
    include_level3 = st.checkbox("Include Level 3 open research", value=bool(env_agent.include_level3),
                                 help="INCLUDE_LEVEL3 (default off): Level 3 topics compete with the Level 2 "
                                      "benchmark for research turns, so they are opt-in.")
    use_temp = st.checkbox("Set temperature")
    temperature = st.slider("Temperature", 0.0, 1.5, 0.6, 0.05, disabled=not use_temp) if use_temp else None
    max_tokens = st.number_input("max_tokens per model turn (0 = provider default)", 0, 131072, 0, step=1024)
    thinking_options = ["provider default", "enabled", "disabled"]
    thinking_env = secret("GLM_THINKING").strip().lower()
    thinking_choice = st.selectbox("Thinking / reasoning", thinking_options,
                                   index=thinking_options.index(thinking_env) if thinking_env in thinking_options else 0,
                                   help='Sends {"thinking": {"type": ...}}; "provider default" sends nothing.')
    thinking = "" if thinking_choice == "provider default" else thinking_choice
    extra_raw = st.text_area("Extra request JSON (merged into every chat payload)", value=secret("GLM_EXTRA_BODY", ""),
                             placeholder='{"thinking": {"type": "enabled"}}', height=80)

    st.header("Cost")
    price_defaults = pricing_defaults(model_id)
    st.caption(f"Defaults: {price_defaults['source']}")
    price_in = st.number_input("USD per 1M input tokens", 0.0, 100.0, price_defaults["input_per_mtok"],
                               step=0.05, format="%.3f", key=f"price_in_{model_id}")
    price_out = st.number_input("USD per 1M output tokens", 0.0, 100.0, price_defaults["output_per_mtok"],
                                step=0.05, format="%.3f", key=f"price_out_{model_id}")
    price_search = st.number_input("USD per GLM web_search call", 0.0, 10.0, price_defaults["web_search_per_call"],
                                   step=0.005, format="%.3f", key="price_search")
    edited = (price_in, price_out, price_search) != (price_defaults["input_per_mtok"],
                                                     price_defaults["output_per_mtok"],
                                                     price_defaults["web_search_per_call"])
    pricing = {"input_per_mtok": price_in, "output_per_mtok": price_out, "web_search_per_call": price_search,
               "source": "edited in UI" if edited else price_defaults["source"]}

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
    chosen_id = st.selectbox("Vehicle", list(labels), format_func=labels.get,
                             index=list(labels).index(HANDSHAKE_RECORD_ID))
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
    settings = GLMSettings(api_key=api_key, base_url=base_url, model=model_id,
                           finalizer_model=finalizer_model_id.strip(), chat_path=chat_path or DEFAULT_CHAT_PATH,
                           search_path=search_path or DEFAULT_SEARCH_PATH,
                           search_engine=search_engine or DEFAULT_SEARCH_ENGINE, timeout_s=float(chat_timeout),
                           chat_max_attempts=int(chat_attempts), search_max_attempts=int(search_attempts))
    for model, limit in chat_limits.items():
        controller.set_model_limit(model, limit)
    controller.set_search_limit(int(search_limit))
    cancel_event = threading.Event()
    make_client = vehicle_client_factory(settings, controller, cancel_event)
    try:
        client = make_client()   # validates the settings; every vehicle worker gets its own client
    except GLMError as exc:
        st.error(str(exc))
        st.stop()

    batch_id = new_batch_id(f"{model_id}-{mode_label.split()[0].lower()}", RUNS_DIR)
    agent_cfg = agent_config_from_env(env=secret, max_steps=int(max_steps), max_tool_output_chars=int(tool_chars),
                                      no_new_research_turns=int(idle_turns), temperature=temperature,
                                      field_recovery_enabled=bool(recovery_on),
                                      field_recovery_max_attempts=int(recovery_attempts),
                                      field_recovery_max_steps=int(recovery_steps),
                                      field_recovery_max_total_steps=int(recovery_total),
                                      max_tokens=int(max_tokens) or None, include_level3=include_level3,
                                      thinking=thinking, extra_body=extra_body)
    tool_cfg = tool_config_from_env(env=secret, search_backend=search_backend)
    rows_by_id = {str(r["upstream_record_id"]): r for r in load.rows}
    batch_workers = max(1, min(int(workers), len(selection)))
    start_batch(RUNS_DIR, batch_id, client=client, agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing,
                vehicles=selection, level15_source=load.source, level15_note=load.note, selection=mode_label,
                prompt_version=PROMPT_VERSION,
                concurrency={"batch_max_workers": batch_workers,
                             **controller.config([model_id, finalizer_model_id.strip() or model_id])})
    st.session_state["batch_id"] = batch_id
    st.caption(f"Batch {batch_id} · Level 1.5 from {load.source} · {batch_workers} vehicle worker(s)")
    finalizer = client.finalizer_model
    state = BatchLiveState([(v["upstream_record_id"], vehicle_title(v)) for v in selection
                            if v["upstream_record_id"] in rows_by_id],
                           pricing=pricing, controller=controller,
                           pricing_finalizer=pricing if finalizer == model_id else default_pricing(finalizer))

    def run_one(vehicle: dict, row: dict) -> dict:
        """Runs on a worker thread: its own GLM client and HTTP session, events only to the queue."""
        return research_one(vehicle, row, client=make_client(), cache=cache, runs_dir=RUNS_DIR, batch_id=batch_id,
                            agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing, level15_source=load.source,
                            listener=state.listener_for(vehicle["upstream_record_id"]), cancel_event=cancel_event)

    stats: dict = {}
    cache_before = cache.stats_snapshot()
    with controller.observe() as observation:
        try:
            run_live_batch(selection, rows_by_id, run_one, state, max_workers=batch_workers,
                           cancel_event=cancel_event, stats=stats)
        finally:
            update_batch(RUNS_DIR, batch_id, {"concurrency_observed": batch_observability(
                stats, observation, cache_before, cache.stats_snapshot())})
    view_batch = batch_id

# --- Views ---------------------------------------------------------------------------

# Every started run of the batch: result.json when present, otherwise reconstructed from events.jsonl.
results = load_runs(RUNS_DIR, view_batch, cache=cache) if view_batch else []
if view_batch:
    st.divider()
    st.subheader(f"Batch {view_batch}")
tab_run, tab_docs, tab_results, tab_bench = st.tabs(["Run", "Documents", "Results", "Benchmark"])
with tab_run:
    if not view_batch:
        st.caption("No results to show yet. Choose a scope and press Run Research.")
    elif not results:
        declared = (load_batch(RUNS_DIR, view_batch) or {}).get("record_ids") or []
        st.caption(f"Batch {view_batch} lists {len(declared)} vehicle(s), but no run folder has input.json, "
                   "events.jsonl or result.json yet.")
    incomplete = [r for r in results if r.get("synthesized")]
    if incomplete:
        st.warning(f"{len(incomplete)} run(s) in this batch did not produce a final result.json; they are shown "
                   "from their recovered research trace.")
    for result in results:
        run_view.render_vehicle(result, labels.get(result["record_id"], result["record_id"]), RUNS_DIR, cache)
with tab_docs:
    run_view.render_documents_tab(cache, results, RUNS_DIR)
with tab_results:
    run_view.render_results_tab(results, labels, RUNS_DIR, cache)
with tab_bench:
    benchmark_view.render_benchmark(results, vehicles_by_id, labels, cache, RUNS_DIR)

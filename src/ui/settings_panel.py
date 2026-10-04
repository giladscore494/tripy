"""Advanced research settings (sidebar expander).

The same knobs, defaults (environment / Streamlit secrets first) and semantics as the previous sidebar; they
only apply to the NEXT run started from this browser session. Production secrets are never typed here: the API
key text box exists only in development (or with TRIPY_ALLOW_UI_API_KEY=true).
"""

from __future__ import annotations

from typing import Callable

import streamlit as st

from ..concurrency import SEARCH_PRIME_PROVIDER_LIMIT
from ..run_profiles import NAMED_PROFILES, PROFILE_LABELS, env_overrides
# The settings object, its defaults and its assembly are framework-neutral (src/run_settings.py) so the HTTP API
# (src/api) builds the same object; they stay importable from here.
from ..run_settings import (BOUNDS, EFFORT_OPTIONS, EFFORT_PHASES, UISettings, agent_defaults, assemble_settings,
                            chat_limit_default, chat_limits_for, dsn_default, effort_default,
                            extra_json_disables_thinking, glm_defaults, pricing_defaults, pricing_from,
                            search_limit_default, workers_default)
from ..run_settings import merge_effort_settings  # noqa: F401  (re-exported: imported from here by tests)


def render_reasoning_efforts(secret: Callable[[str], str]) -> dict[str, str]:
    """One "Reasoning effort" select per phase (replaces the thinking selects: thinking cannot be disabled on these
    models; a configured "disabled" is mapped to effort low)."""
    st.caption("Reasoning effort per phase (sent as `reasoning_effort`). Defaults: environment, else research high and "
               "everything else low.")
    columns = st.columns(len(EFFORT_PHASES))
    out = {}
    for column, (phase, label) in zip(columns, EFFORT_PHASES):
        default = effort_default(secret, phase)
        with column:
            out[phase] = st.selectbox(f"{label} reasoning effort", EFFORT_OPTIONS, key=f"cfg_effort_{phase}",
                                      index=EFFORT_OPTIONS.index(default),
                                      help=f"GLM_{phase.upper()}_REASONING_EFFORT / GLM_REASONING_EFFORT")
    return out


def render_settings(secret: Callable[[str], str], controller, *, allow_ui_key: bool) -> UISettings:
    env_key = secret("GLM_API_KEY")
    g = glm_defaults(secret)
    b = BOUNDS
    with st.expander("Advanced settings", expanded=False):
        st.caption("Applied to the next run you start. Defaults come from the server's environment variables.")
        st.markdown("**Model**")
        model_id = st.text_input("Research model id", value=g["model_id"], key="cfg_model",
                                 help="GLM_MODEL, e.g. glm-5.3-flash. Used for every research/tool turn.")
        finalizer_model_id = st.text_input("Finalizer model id (optional)", value=g["finalizer_model_id"],
                                           key="cfg_finalizer", help="GLM_FINALIZER_MODEL; empty = the research model.")
        base_url = st.text_input("API base URL", value=g["base_url"], key="cfg_base")
        chat_path = st.text_input("Chat endpoint", value=g["chat_path"], key="cfg_chat")
        api_key, from_ui = env_key, False
        if not env_key and allow_ui_key:
            api_key = st.text_input("API key (development only)", type="password", key="cfg_key",
                                    help="Production reads GLM_API_KEY from the environment.")
            from_ui = bool(api_key)
        search_backend = st.selectbox("Search backend", ["glm", "duckduckgo"], key="cfg_backend",
                                      index=0 if g["search_backend"] == "glm" else 1,
                                      help="glm = GLM web_search API; duckduckgo = keyless HTML search.")
        search_path = st.text_input("GLM search endpoint", value=g["search_path"],
                                    disabled=search_backend != "glm", key="cfg_spath")
        search_engine = st.text_input("GLM search engine", value=g["search_engine"],
                                      disabled=search_backend != "glm", key="cfg_engine")
        chat_attempts = st.number_input("Chat max attempts (total, 1 = no retry)", *b["chat_attempts"],
                                        g["chat_attempts"], key="cfg_ca")
        search_attempts = st.number_input("Search max attempts (total)", *b["search_attempts"], g["search_attempts"],
                                          key="cfg_sa")
        chat_timeout = st.number_input("Chat read timeout per attempt (s)", *b["chat_timeout"], g["chat_timeout"],
                                       step=30, key="cfg_timeout")

        st.markdown("**Concurrency** (provider limits are shared by every run in this server)")
        workers = st.number_input("Vehicle workers (vehicles researched at once)", *b["workers"],
                                  workers_default(secret), key="cfg_workers",
                                  help="BATCH_MAX_WORKERS. Not a request limit: the pools below guard every request.")

        def chat_limit_input(model: str, label: str) -> int:
            default, maximum, provider = chat_limit_default(controller, model)
            value = st.number_input(f"{label} in-flight requests ({model})", 1, maximum, default,
                                    key=f"chat_limit_{model}")
            st.caption(f"provider limit: {provider or 'unknown model (conservative fallback)'}")
            return int(value)

        chat_limits = chat_limits_for(controller, model_id, finalizer_model_id, chat_limit_input)
        search_limit = st.number_input("Search-Prime in-flight requests", 1, SEARCH_PRIME_PROVIDER_LIMIT,
                                       search_limit_default(controller), key="cfg_search_limit")

        st.markdown("**Research budget**")
        a = agent_defaults(secret)
        max_steps = st.slider("Primary research turns (source acquisition)", *b["max_steps"], a["max_steps"],
                              key="cfg_turns", help="PRIMARY_RESEARCH_MAX_TURNS / AGENT_MAX_STEPS")
        no_artifact = st.number_input("Stop research after N turns that acquire nothing new (0 = off)",
                                      *b["no_artifact"], a["no_artifact"], key="cfg_noart")
        idle_turns = st.number_input("Finalize after N turns with no new research artifact (0 = off)",
                                     *b["idle_turns"], a["idle_turns"], key="cfg_idle")
        tool_chars = st.number_input("Max chars per tool result sent to model", *b["tool_chars"], a["tool_chars"],
                                     step=500, key="cfg_toolchars")
        recovery_on = st.checkbox("Tail recovery of unresolved requested fields", value=a["recovery_on"],
                                  key="cfg_recovery", help="FIELD_RECOVERY_ENABLED")
        recovery_attempts = st.number_input("Retry attempts per failed field", *b["recovery_attempts"],
                                            a["recovery_attempts"], disabled=not recovery_on, key="cfg_rattempts")
        recovery_steps = st.number_input("Model turns per retry attempt", *b["recovery_steps"], a["recovery_steps"],
                                         disabled=not recovery_on, key="cfg_rsteps")
        recovery_total = st.number_input("Total recovery turns per vehicle (0 = no cap)", *b["recovery_total"],
                                         a["recovery_total"], disabled=not recovery_on, key="cfg_rtotal")
        include_level3 = st.checkbox("Include Level 3 open research", value=a["include_level3"], key="cfg_l3")
        use_temp = st.checkbox("Set temperature", key="cfg_usetemp")
        temperature = st.slider("Temperature", 0.0, 1.5, 0.6, 0.05, key="cfg_temp") if use_temp else None
        max_tokens = st.number_input("max_tokens per model turn (0 = provider default)", *b["max_tokens"], 0,
                                     step=1024, key="cfg_maxtok")
        efforts = render_reasoning_efforts(secret)
        extra_raw = st.text_area("Extra request JSON (merged into every chat payload)", value=a["extra_raw"],
                                 placeholder='{"custom_flag": true}', height=80, key="cfg_extra")
        if extra_json_disables_thinking(extra_raw):
            st.warning('This JSON contains thinking.type = "disabled". It is stripped from every request (the '
                       "provider rejects it: these models always think); requests use reasoning effort low instead "
                       "unless an effort is set above.")

        st.markdown("**Experiment settings** (used by the Custom run profile; a named profile sets these itself)")
        mode_options = ["contract", "legacy"]
        acquisition_mode = st.selectbox("Acquisition mode", mode_options, key="cfg_acq_mode",
                                        index=mode_options.index(a["acquisition_mode"]),
                                        help="ACQUISITION_MODE. contract = primary research is source acquisition only; "
                                             "legacy = the previous behaviour (benchmark baseline).")
        card_choice = st.selectbox("Document card", ["off", "on"], index=1 if a["card_choice"] == "on" else 0,
                                   key="cfg_card", help="ACQUISITION_DOCUMENT_CARD (contract research only): fetch "
                                   "results carry server-computed scheduling metadata, never evidence.")
        site_map_choice = st.selectbox("Importer site map", ["on", "off"], index=0 if a["site_map_choice"] == "on" else 1,
                                       key="cfg_site_map", help="SITE_MAP: contract acquisition and re-acquisition get "
                                       "ranked real URLs from the official sites' sitemaps (discovery only).")
        grounded_choice = st.selectbox("Grounded candidates", ["on", "off"],
                                       index=0 if a["grounded_choice"] == "on" else 1, key="cfg_grounded",
                                       help="GROUNDED_CANDIDATES: one no-tool call per top document for open fields "
                                       "without an admissible candidate; the model points at a span, code cuts the "
                                       "quote. Candidates only (adjudication + admission decide).")
        recovery_options = ["reacquire", "cluster", "legacy"]
        recovery_mode = st.selectbox("Recovery mode", recovery_options, key="cfg_recovery_mode",
                                     index=recovery_options.index(a["recovery_mode"]),
                                     help="RECOVERY_MODE. reacquire = per cluster a targeted acquisition episode, "
                                          "harvest, grounded candidates and adjudication; cluster = the tool-using "
                                          "cluster recovery agent; legacy = per-field retries.")
        sweep_attempts = st.number_input("Document sweep max attempts", *b["sweep_attempts"], a["sweep_attempts"],
                                         key="cfg_sweep_attempts",
                                         help="GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS (total HTTP attempts per sweep request).")
        sweep_fields = st.number_input("Document sweep max fields per chunk", *b["sweep_fields"], a["sweep_fields"],
                                       key="cfg_sweep_fields", help="DOCUMENT_SWEEP_MAX_FIELDS")
        sweep_candidates = st.number_input("Document sweep max candidates per chunk", *b["sweep_candidates"],
                                           a["sweep_candidates"], key="cfg_sweep_cands",
                                           help="DOCUMENT_SWEEP_MAX_CANDIDATES")
        overridden = env_overrides(secret)
        if overridden:
            st.caption("Environment values that differ from the code defaults (named profiles ignore them; Custom "
                       "uses them as the defaults above):")
            st.code("\n".join(o["text"] for o in overridden), language=None)
        else:
            st.caption("No experiment-relevant environment variable differs from its code default.")
        st.caption("With a named run profile (" + ", ".join(PROFILE_LABELS[p] for p in NAMED_PROFILES)
                   + ") the research turn ceiling, minimum acquisition base, document sweep mode and limits, reasoning "
                     "efforts, recovery max attempts, the deterministic final assembly, the site map, grounded "
                     "candidates and the recovery mode come from the profile (code defaults), not from the values "
                     "above.")

        st.markdown("**Cost reporting**")
        price_defaults = pricing_defaults(model_id, secret)
        st.caption(f"Defaults: {price_defaults['source']}")
        price_in = st.number_input("USD per 1M input tokens", 0.0, 100.0, price_defaults["input_per_mtok"],
                                   step=0.05, format="%.3f", key=f"price_in_{model_id}")
        price_out = st.number_input("USD per 1M output tokens", 0.0, 100.0, price_defaults["output_per_mtok"],
                                    step=0.05, format="%.3f", key=f"price_out_{model_id}")
        price_search = st.number_input("USD per GLM web_search call", 0.0, 10.0, price_defaults["web_search_per_call"],
                                       step=0.005, format="%.3f", key="price_search")
        pricing = pricing_from(price_defaults, price_in, price_out, price_search)

        st.markdown("**Level 1.5 data**")
        dsn = dsn_default(secret)
        data_source = st.selectbox("Source", ["auto", "database", "snapshot"], key="cfg_source",
                                   help="auto = the database when DATABASE_URL is set, else the frozen snapshot.")
        st.caption("DATABASE_URL: " + ("configured" if dsn else "not set → snapshot"))

    return assemble_settings(
        model_id=model_id, finalizer_model_id=finalizer_model_id, base_url=base_url, chat_path=chat_path,
        api_key=api_key, api_key_from_ui=from_ui, search_backend=search_backend, search_path=search_path,
        search_engine=search_engine, chat_attempts=chat_attempts, search_attempts=search_attempts,
        chat_timeout=chat_timeout, workers=workers, chat_limits=chat_limits, search_limit=search_limit,
        max_steps=max_steps, no_artifact=no_artifact, idle_turns=idle_turns, tool_chars=tool_chars,
        recovery_on=recovery_on, recovery_attempts=recovery_attempts, recovery_steps=recovery_steps,
        recovery_total=recovery_total, include_level3=include_level3, temperature=temperature, max_tokens=max_tokens,
        efforts=efforts, extra_raw=extra_raw, acquisition_mode=acquisition_mode, card_choice=card_choice,
        site_map_choice=site_map_choice, grounded_choice=grounded_choice, recovery_mode=recovery_mode,
        sweep_attempts=sweep_attempts, sweep_fields=sweep_fields, sweep_candidates=sweep_candidates, pricing=pricing,
        data_source=data_source, dsn=dsn, env_overrides=overridden)

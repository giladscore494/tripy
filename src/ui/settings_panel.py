"""Advanced research settings (sidebar expander).

The same knobs, defaults (environment / Streamlit secrets first) and semantics as the previous sidebar; they
only apply to the NEXT run started from this browser session. Production secrets are never typed here: the API
key text box exists only in development (or with TRIPY_ALLOW_UI_API_KEY=true).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import streamlit as st

from ..agent import agent_config_from_env, tool_config_from_env
from ..phase_settings import (PHASE_DEFAULTS, PROVIDER_DEFAULT, REASONING_EFFORTS, extra_body_disables_thinking,
                              parse_effort)
from ..run_profiles import CUSTOM, NAMED_PROFILES, PROFILE_LABELS, build_agent_config, env_overrides
from ..concurrency import SEARCH_PRIME_PROVIDER_LIMIT, DEFAULT_SEARCH_MAX_INFLIGHT, batch_max_workers_from_env
from ..glm_client import (DEFAULT_BASE_URL, DEFAULT_CHAT_MAX_ATTEMPTS, DEFAULT_CHAT_PATH, DEFAULT_CHAT_TIMEOUT_S,
                          DEFAULT_SEARCH_ENGINE, DEFAULT_SEARCH_MAX_ATTEMPTS, DEFAULT_SEARCH_PATH, GLMSettings)
from ..pricing import default_pricing


@dataclass
class UISettings:
    model_id: str
    finalizer_model_id: str
    base_url: str
    chat_path: str
    api_key: str
    api_key_from_ui: bool
    search_backend: str
    search_path: str
    search_engine: str
    chat_attempts: int
    search_attempts: int
    chat_timeout: float
    workers: int
    chat_limits: dict
    search_limit: int
    agent_overrides: dict
    pricing: dict
    data_source: str
    dsn: str
    extra_error: str = ""
    notes: list = field(default_factory=list)
    env_overrides: list = field(default_factory=list)   # env values differing from the code defaults (Part G)

    def glm_settings(self) -> GLMSettings:
        """A fresh settings object per run (the API key stays in memory; never persisted)."""
        return GLMSettings(api_key=self.api_key, base_url=self.base_url, model=self.model_id,
                           finalizer_model=self.finalizer_model_id.strip(), chat_path=self.chat_path or DEFAULT_CHAT_PATH,
                           search_path=self.search_path or DEFAULT_SEARCH_PATH,
                           search_engine=self.search_engine or DEFAULT_SEARCH_ENGINE, timeout_s=float(self.chat_timeout),
                           chat_max_attempts=int(self.chat_attempts), search_max_attempts=int(self.search_attempts))

    def agent_config(self, secret: Callable[[str], str], profile: str = CUSTOM):
        """The run's AgentConfig: env defaults, these Advanced settings (phase settings merged per phase and key over
        the env ones), then a named run profile's values, which ignore env (src/run_profiles.py)."""
        return build_agent_config(secret, self.agent_overrides, profile)

    def tool_config(self, secret: Callable[[str], str]):
        return tool_config_from_env(env=secret, search_backend=self.search_backend)


def _int(raw: str, default: int) -> int:
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(raw: str, default: float) -> float:
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def pricing_defaults(model: str, secret: Callable[[str], str]) -> dict:
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


EFFORT_PHASES = (("research", "Research"), ("document_sweep", "Document sweep"), ("recovery", "Recovery"),
                 ("finalizer", "Finalizer"))
EFFORT_OPTIONS = ["provider default"] + list(REASONING_EFFORTS)


def effort_default(secret: Callable[[str], str], phase: str) -> str:
    """The select's default: GLM_<PHASE>_REASONING_EFFORT, else GLM_REASONING_EFFORT, else the code default."""
    for raw in (secret(f"GLM_{phase.upper()}_REASONING_EFFORT"), secret("GLM_REASONING_EFFORT")):
        value = parse_effort(raw)
        if value:
            return "provider default" if value == PROVIDER_DEFAULT else value
    return PHASE_DEFAULTS.get(phase, {}).get("reasoning_effort") or "provider default"


def merge_effort_settings(phases: dict, efforts: dict[str, str]) -> dict:
    """The UI's per-phase reasoning effort merged into its phase settings ("provider default" = send no effort)."""
    out = {phase: dict(values) for phase, values in phases.items()}
    for phase, choice in efforts.items():
        out.setdefault(phase, {})["reasoning_effort"] = PROVIDER_DEFAULT if choice == "provider default" else choice
    return out


def extra_json_disables_thinking(raw: str) -> bool:
    try:
        return extra_body_disables_thinking(json.loads(raw)) if (raw or "").strip() else False
    except ValueError:
        return False


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
    with st.expander("Advanced settings", expanded=False):
        st.caption("Applied to the next run you start. Defaults come from the server's environment variables.")
        st.markdown("**Model**")
        model_id = st.text_input("Research model id", value=secret("GLM_MODEL"), key="cfg_model",
                                 help="GLM_MODEL, e.g. glm-5.3-flash. Used for every research/tool turn.")
        finalizer_model_id = st.text_input("Finalizer model id (optional)", value=secret("GLM_FINALIZER_MODEL"),
                                           key="cfg_finalizer", help="GLM_FINALIZER_MODEL; empty = the research model.")
        base_url = st.text_input("API base URL", value=secret("GLM_BASE_URL") or DEFAULT_BASE_URL, key="cfg_base")
        chat_path = st.text_input("Chat endpoint", value=secret("GLM_CHAT_PATH") or DEFAULT_CHAT_PATH, key="cfg_chat")
        api_key, from_ui = env_key, False
        if not env_key and allow_ui_key:
            api_key = st.text_input("API key (development only)", type="password", key="cfg_key",
                                    help="Production reads GLM_API_KEY from the environment.")
            from_ui = bool(api_key)
        search_backend = st.selectbox("Search backend", ["glm", "duckduckgo"], key="cfg_backend",
                                      index=0 if (secret("SEARCH_BACKEND") or "glm") == "glm" else 1,
                                      help="glm = GLM web_search API; duckduckgo = keyless HTML search.")
        search_path = st.text_input("GLM search endpoint", value=secret("GLM_SEARCH_PATH") or DEFAULT_SEARCH_PATH,
                                    disabled=search_backend != "glm", key="cfg_spath")
        search_engine = st.text_input("GLM search engine", value=secret("GLM_SEARCH_ENGINE") or DEFAULT_SEARCH_ENGINE,
                                      disabled=search_backend != "glm", key="cfg_engine")
        chat_attempts = st.number_input("Chat max attempts (total, 1 = no retry)", 1, 6,
                                        _int(secret("GLM_CHAT_MAX_ATTEMPTS"), DEFAULT_CHAT_MAX_ATTEMPTS), key="cfg_ca")
        search_attempts = st.number_input("Search max attempts (total)", 1, 6,
                                          _int(secret("GLM_SEARCH_MAX_ATTEMPTS"), DEFAULT_SEARCH_MAX_ATTEMPTS),
                                          key="cfg_sa")
        chat_timeout = st.number_input("Chat read timeout per attempt (s)", 30, 1800,
                                       int(_float(secret("GLM_CHAT_TIMEOUT_S"), DEFAULT_CHAT_TIMEOUT_S)), step=30,
                                       key="cfg_timeout")

        st.markdown("**Concurrency** (provider limits are shared by every run in this server)")
        workers = st.number_input("Vehicle workers (vehicles researched at once)", 1, 50,
                                  min(50, batch_max_workers_from_env(secret)), key="cfg_workers",
                                  help="BATCH_MAX_WORKERS. Not a request limit: the pools below guard every request.")

        def chat_limit_input(model: str, label: str) -> int:
            provider = controller.provider_limit_for(model)
            current = controller.limit_for(model)
            value = st.number_input(f"{label} in-flight requests ({model})", 1, provider or 50,
                                    min(current, provider or 50), key=f"chat_limit_{model}")
            st.caption(f"provider limit: {provider or 'unknown model (conservative fallback)'}")
            return int(value)

        chat_limits = {}
        if model_id:
            chat_limits[model_id] = chat_limit_input(model_id, "Research model")
        if finalizer_model_id.strip() and finalizer_model_id.strip().lower() != model_id.strip().lower():
            chat_limits[finalizer_model_id.strip()] = chat_limit_input(finalizer_model_id.strip(), "Finalizer model")
        search_limit = st.number_input("Search-Prime in-flight requests", 1, SEARCH_PRIME_PROVIDER_LIMIT,
                                       min(controller.search.limit, SEARCH_PRIME_PROVIDER_LIMIT)
                                       or DEFAULT_SEARCH_MAX_INFLIGHT, key="cfg_search_limit")

        st.markdown("**Research budget**")
        env_agent = agent_config_from_env(env=secret)
        max_steps = st.slider("Primary research turns (source acquisition)", 3, 80, int(env_agent.max_steps),
                              key="cfg_turns", help="PRIMARY_RESEARCH_MAX_TURNS / AGENT_MAX_STEPS")
        no_artifact = st.number_input("Stop research after N turns that acquire nothing new (0 = off)", 0, 20,
                                      int(env_agent.primary_research_no_artifact_stop), key="cfg_noart")
        idle_turns = st.number_input("Finalize after N turns with no new research artifact (0 = off)", 0, 20,
                                     int(env_agent.no_new_research_turns), key="cfg_idle")
        tool_chars = st.number_input("Max chars per tool result sent to model", 1000, 60000,
                                     int(env_agent.max_tool_output_chars), step=500, key="cfg_toolchars")
        recovery_on = st.checkbox("Tail recovery of unresolved requested fields", value=env_agent.field_recovery_enabled,
                                  key="cfg_recovery", help="FIELD_RECOVERY_ENABLED")
        recovery_attempts = st.number_input("Retry attempts per failed field", 0, 5,
                                            int(env_agent.field_recovery_max_attempts), disabled=not recovery_on,
                                            key="cfg_rattempts")
        recovery_steps = st.number_input("Model turns per retry attempt", 1, 20, int(env_agent.field_recovery_max_steps),
                                         disabled=not recovery_on, key="cfg_rsteps")
        recovery_total = st.number_input("Total recovery turns per vehicle (0 = no cap)", 0, 400,
                                         int(env_agent.field_recovery_max_total_steps), disabled=not recovery_on,
                                         key="cfg_rtotal")
        include_level3 = st.checkbox("Include Level 3 open research", value=bool(env_agent.include_level3),
                                     key="cfg_l3")
        use_temp = st.checkbox("Set temperature", key="cfg_usetemp")
        temperature = st.slider("Temperature", 0.0, 1.5, 0.6, 0.05, key="cfg_temp") if use_temp else None
        max_tokens = st.number_input("max_tokens per model turn (0 = provider default)", 0, 131072, 0, step=1024,
                                     key="cfg_maxtok")
        efforts = render_reasoning_efforts(secret)
        extra_raw = st.text_area("Extra request JSON (merged into every chat payload)", value=secret("GLM_EXTRA_BODY"),
                                 placeholder='{"custom_flag": true}', height=80, key="cfg_extra")
        if extra_json_disables_thinking(extra_raw):
            st.warning('This JSON contains thinking.type = "disabled". It is stripped from every request (the '
                       "provider rejects it: these models always think); requests use reasoning effort low instead "
                       "unless an effort is set above.")

        st.markdown("**Experiment settings** (used by the Custom run profile; a named profile sets these itself)")
        mode_options = ["contract", "legacy"]
        acquisition_mode = st.selectbox("Acquisition mode", mode_options, key="cfg_acq_mode",
                                        index=mode_options.index(env_agent.acquisition_mode)
                                        if env_agent.acquisition_mode in mode_options else 0,
                                        help="ACQUISITION_MODE. contract = primary research is source acquisition only; "
                                             "legacy = the previous behaviour (benchmark baseline).")
        card_choice = st.selectbox("Document card", ["off", "on"], index=1 if env_agent.acquisition_document_card else 0,
                                   key="cfg_card", help="ACQUISITION_DOCUMENT_CARD (contract research only): fetch "
                                   "results carry server-computed scheduling metadata, never evidence.")
        sweep_env = env_agent.phase_settings.get("document_sweep") or {}
        sweep_attempts = st.number_input("Document sweep max attempts", 1, 3, max(1, min(3, int(
            sweep_env.get("max_attempts") or PHASE_DEFAULTS["document_sweep"]["max_attempts"]))),
            key="cfg_sweep_attempts", help="GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS (total HTTP attempts per sweep request).")
        sweep_fields = st.number_input("Document sweep max fields per chunk", 1, 200,
                                       int(env_agent.document_sweep_max_fields), key="cfg_sweep_fields",
                                       help="DOCUMENT_SWEEP_MAX_FIELDS")
        sweep_candidates = st.number_input("Document sweep max candidates per chunk", 1, 500,
                                           int(env_agent.document_sweep_max_candidates), key="cfg_sweep_cands",
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
                     "efforts, recovery max attempts and the deterministic final assembly come from the profile (code "
                     "defaults), not from the values above.")

        st.markdown("**Cost reporting**")
        price_defaults = pricing_defaults(model_id, secret)
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

        st.markdown("**Level 1.5 data**")
        from ..db import database_url
        dsn = secret("DATABASE_URL") or database_url()
        data_source = st.selectbox("Source", ["auto", "database", "snapshot"], key="cfg_source",
                                   help="auto = the database when DATABASE_URL is set, else the frozen snapshot.")
        st.caption("DATABASE_URL: " + ("configured" if dsn else "not set → snapshot"))

    extra_body: dict = {}
    extra_error = ""
    if extra_raw.strip():
        try:
            extra_body = json.loads(extra_raw)
            if not isinstance(extra_body, dict):
                extra_error, extra_body = "Extra request JSON must be an object.", {}
        except ValueError as exc:
            extra_error = f"Extra request JSON is invalid: {exc}"
    overrides = dict(max_steps=int(max_steps), max_tool_output_chars=int(tool_chars),
                     no_new_research_turns=int(idle_turns), temperature=temperature,
                     primary_research_no_artifact_stop=int(no_artifact), field_recovery_enabled=bool(recovery_on),
                     field_recovery_max_attempts=int(recovery_attempts), field_recovery_max_steps=int(recovery_steps),
                     field_recovery_max_total_steps=int(recovery_total), max_tokens=int(max_tokens) or None,
                     include_level3=include_level3, extra_body=extra_body,
                     acquisition_mode=acquisition_mode, acquisition_document_card=card_choice == "on",
                     document_sweep_max_fields=int(sweep_fields), document_sweep_max_candidates=int(sweep_candidates),
                     # merged per phase / key over the env phase settings (None removes the env value)
                     phase_settings=merge_effort_settings(
                         {"document_sweep": {"max_attempts": int(sweep_attempts)}}, efforts))
    return UISettings(model_id=model_id.strip(), finalizer_model_id=finalizer_model_id, base_url=base_url,
                      chat_path=chat_path, api_key=api_key, api_key_from_ui=from_ui, search_backend=search_backend,
                      search_path=search_path, search_engine=search_engine, chat_attempts=int(chat_attempts),
                      search_attempts=int(search_attempts), chat_timeout=float(chat_timeout), workers=int(workers),
                      chat_limits=chat_limits, search_limit=int(search_limit), agent_overrides=overrides,
                      pricing=pricing, data_source=data_source, dsn=dsn, extra_error=extra_error,
                      env_overrides=overridden)

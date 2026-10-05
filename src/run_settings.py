"""Run settings of every launcher (framework-neutral): the HTTP API's runs and A/B series, the CLI. One settings object,
built by ONE code path (the former dashboard sidebar's semantics, kept exactly):

    defaults from the environment   glm_defaults / agent_defaults / effort_default / pricing_defaults / ...
    ranges                          BOUNDS (the per-run settings' ranges; env defaults are clamped into them)
    one settings object             assemble_settings(...) -> UISettings
    one research request            build_research_request(settings, ...) -> jobs.manager.ResearchRequest

`settings_from_env(lookup, controller)` is the server's configuration with no per-run override; settings_for_run adds
a client's validated per-run overrides (RUN_OVERRIDES) on top.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import PROMPT_VERSION, agent_config_from_env, tool_config_from_env
from .concurrency import DEFAULT_SEARCH_MAX_INFLIGHT, SEARCH_PRIME_PROVIDER_LIMIT, batch_max_workers_from_env
from .glm_client import (DEFAULT_BASE_URL, DEFAULT_CHAT_MAX_ATTEMPTS, DEFAULT_CHAT_PATH, DEFAULT_CHAT_TIMEOUT_S,
                         DEFAULT_SEARCH_ENGINE, DEFAULT_SEARCH_MAX_ATTEMPTS, DEFAULT_SEARCH_PATH, GLMSettings)
from .phase_settings import PHASE_DEFAULTS, PROVIDER_DEFAULT, REASONING_EFFORTS, extra_body_disables_thinking, \
    parse_effort
from .pricing import default_pricing
from .run_profiles import CUSTOM, build_agent_config, env_overrides

Secret = Callable[[str], str]

# (min, max) of every numeric setting: the sidebar's widget ranges, and the clamp applied to env defaults by the API
BOUNDS = {"chat_attempts": (1, 6), "search_attempts": (1, 6), "chat_timeout": (30, 1800), "workers": (1, 50),
          "max_steps": (3, 80), "no_artifact": (0, 20), "idle_turns": (0, 20), "tool_chars": (1000, 60000),
          "recovery_attempts": (0, 5), "recovery_steps": (1, 20), "recovery_total": (0, 400),
          "max_tokens": (0, 131072), "sweep_attempts": (1, 3), "sweep_fields": (1, 200),
          "sweep_candidates": (1, 500), "search_limit": (1, SEARCH_PRIME_PROVIDER_LIMIT), "search_credit_cap": (0, 500)}
SEARCH_CREDIT_CAP_DEFAULT = 40
ACQUISITION_MODES = ["contract", "legacy"]
RECOVERY_MODES = ["reacquire", "cluster", "legacy"]
EFFORT_PHASES = (("research", "Research"), ("document_sweep", "Document sweep"), ("recovery", "Recovery"),
                 ("finalizer", "Finalizer"))
EFFORT_OPTIONS = ["provider default"] + list(REASONING_EFFORTS)


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
    search_fallback_backend: str = "none"               # PR #46: answers a search only on 0 usable primary results
    search_credit_cap: int = SEARCH_CREDIT_CAP_DEFAULT  # PR #47 (A6): provider search credits per vehicle (0 = no cap)

    def glm_settings(self) -> GLMSettings:
        """A fresh settings object per run (the API key stays in memory; never persisted)."""
        return GLMSettings(api_key=self.api_key, base_url=self.base_url, model=self.model_id,
                           finalizer_model=self.finalizer_model_id.strip(), chat_path=self.chat_path or DEFAULT_CHAT_PATH,
                           search_path=self.search_path or DEFAULT_SEARCH_PATH,
                           search_engine=self.search_engine or DEFAULT_SEARCH_ENGINE, timeout_s=float(self.chat_timeout),
                           chat_max_attempts=int(self.chat_attempts), search_max_attempts=int(self.search_attempts))

    def agent_config(self, secret: Secret, profile: str = CUSTOM):
        """The run's AgentConfig: env defaults, these Advanced settings (phase settings merged per phase and key over
        the env ones), then a named run profile's values, which ignore env (src/run_profiles.py)."""
        return build_agent_config(secret, self.agent_overrides, profile)

    def tool_config(self, secret: Secret):
        fallback = "" if self.search_fallback_backend in ("", "none", self.search_backend) \
            else self.search_fallback_backend
        return tool_config_from_env(env=secret, search_backend=self.search_backend, search_fallback_backend=fallback,
                                    search_credit_cap=int(self.search_credit_cap))


# --- defaults --------------------------------------------------------------------------------------------------------

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


def clamp(name: str, value):
    low, high = BOUNDS[name]
    return max(low, min(high, value))


def glm_defaults(secret: Secret) -> dict:
    """Model / endpoint / attempt defaults exactly as the sidebar's inputs start (raw text: not stripped)."""
    return {"model_id": secret("GLM_MODEL"), "finalizer_model_id": secret("GLM_FINALIZER_MODEL"),
            "base_url": secret("GLM_BASE_URL") or DEFAULT_BASE_URL,
            "chat_path": secret("GLM_CHAT_PATH") or DEFAULT_CHAT_PATH,
            "search_backend": (secret("SEARCH_BACKEND") or "glm").strip().lower()
            if (secret("SEARCH_BACKEND") or "glm").strip().lower() in SEARCH_BACKENDS else "glm",
            "search_path": secret("GLM_SEARCH_PATH") or DEFAULT_SEARCH_PATH,
            "search_engine": secret("GLM_SEARCH_ENGINE") or DEFAULT_SEARCH_ENGINE,
            "chat_attempts": _int(secret("GLM_CHAT_MAX_ATTEMPTS"), DEFAULT_CHAT_MAX_ATTEMPTS),
            "search_attempts": _int(secret("GLM_SEARCH_MAX_ATTEMPTS"), DEFAULT_SEARCH_MAX_ATTEMPTS),
            "chat_timeout": int(_float(secret("GLM_CHAT_TIMEOUT_S"), DEFAULT_CHAT_TIMEOUT_S))}


def workers_default(secret: Secret) -> int:
    return min(50, batch_max_workers_from_env(secret))


def chat_limit_default(controller, model: str) -> tuple[int, int, int | None]:
    """(default, widget maximum, provider limit) of a model's in-flight requests."""
    provider = controller.provider_limit_for(model)
    return min(controller.limit_for(model), provider or 50), provider or 50, provider


def search_limit_default(controller) -> int:
    return min(controller.search.limit, SEARCH_PRIME_PROVIDER_LIMIT) or DEFAULT_SEARCH_MAX_INFLIGHT


def agent_defaults(secret: Secret) -> dict:
    """Research-budget and experiment defaults from the environment (agent_config_from_env)."""
    env_agent = agent_config_from_env(env=secret)
    sweep_env = env_agent.phase_settings.get("document_sweep") or {}
    return {
        "env_agent": env_agent,
        "max_steps": int(env_agent.max_steps), "no_artifact": int(env_agent.primary_research_no_artifact_stop),
        "idle_turns": int(env_agent.no_new_research_turns), "tool_chars": int(env_agent.max_tool_output_chars),
        "recovery_on": env_agent.field_recovery_enabled,
        "recovery_attempts": int(env_agent.field_recovery_max_attempts),
        "recovery_steps": int(env_agent.field_recovery_max_steps),
        "recovery_total": int(env_agent.field_recovery_max_total_steps),
        "include_level3": bool(env_agent.include_level3),
        "extra_raw": secret("GLM_EXTRA_BODY"),
        "acquisition_mode": env_agent.acquisition_mode if env_agent.acquisition_mode in ACQUISITION_MODES
        else ACQUISITION_MODES[0],
        "card_choice": "on" if env_agent.acquisition_document_card else "off",
        "site_map_choice": "on" if env_agent.site_map else "off",
        "grounded_choice": "on" if env_agent.grounded_candidates else "off",
        "recovery_mode": env_agent.recovery_mode if env_agent.recovery_mode in RECOVERY_MODES else RECOVERY_MODES[0],
        "sweep_attempts": max(1, min(3, int(sweep_env.get("max_attempts")
                                            or PHASE_DEFAULTS["document_sweep"]["max_attempts"]))),
        "sweep_fields": int(env_agent.document_sweep_max_fields),
        "sweep_candidates": int(env_agent.document_sweep_max_candidates),
    }


def effort_default(secret: Secret, phase: str) -> str:
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


def pricing_defaults(model: str, secret: Secret) -> dict:
    """Official defaults for the model id, then env overrides."""
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


def pricing_from(defaults: dict, price_in: float, price_out: float, price_search: float) -> dict:
    edited = (price_in, price_out, price_search) != (defaults["input_per_mtok"], defaults["output_per_mtok"],
                                                     defaults["web_search_per_call"])
    return {"input_per_mtok": price_in, "output_per_mtok": price_out, "web_search_per_call": price_search,
            "source": "edited in UI" if edited else defaults["source"]}


def dsn_default(secret: Secret) -> str:
    from .db import database_url
    return secret("DATABASE_URL") or database_url()


# --- one settings object ---------------------------------------------------------------------------------------------

def assemble_settings(*, model_id: str, finalizer_model_id: str, base_url: str, chat_path: str, api_key: str,
                      api_key_from_ui: bool, search_backend: str, search_path: str, search_engine: str,
                      chat_attempts: int, search_attempts: int, chat_timeout: float, workers: int, chat_limits: dict,
                      search_limit: int, max_steps: int, no_artifact: int, idle_turns: int, tool_chars: int,
                      recovery_on: bool, recovery_attempts: int, recovery_steps: int, recovery_total: int,
                      include_level3: bool, temperature: float | None, max_tokens: int, efforts: dict[str, str],
                      extra_raw: str, acquisition_mode: str, card_choice: str, site_map_choice: str,
                      grounded_choice: str, recovery_mode: str, sweep_attempts: int, sweep_fields: int,
                      sweep_candidates: int, pricing: dict, data_source: str, dsn: str,
                      env_overrides: list) -> UISettings:
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
                     site_map=site_map_choice == "on", grounded_candidates=grounded_choice == "on",
                     recovery_mode=recovery_mode,
                     document_sweep_max_fields=int(sweep_fields), document_sweep_max_candidates=int(sweep_candidates),
                     # merged per phase / key over the env phase settings (None removes the env value)
                     phase_settings=merge_effort_settings(
                         {"document_sweep": {"max_attempts": int(sweep_attempts)}}, efforts))
    return UISettings(model_id=model_id.strip(), finalizer_model_id=finalizer_model_id, base_url=base_url,
                      chat_path=chat_path, api_key=api_key, api_key_from_ui=api_key_from_ui,
                      search_backend=search_backend, search_path=search_path, search_engine=search_engine,
                      chat_attempts=int(chat_attempts), search_attempts=int(search_attempts),
                      chat_timeout=float(chat_timeout), workers=int(workers), chat_limits=chat_limits,
                      search_limit=int(search_limit), agent_overrides=overrides, pricing=pricing,
                      data_source=data_source, dsn=dsn, extra_error=extra_error, env_overrides=env_overrides)


def chat_limits_for(controller, model_id: str, finalizer_model_id: str,
                    pick: Callable[[str, str], int] | None = None) -> dict:
    """{model: in-flight limit} for the research model and a distinct finalizer model; `pick(model, label)` is the
    sidebar's input (default: the untouched default)."""
    pick = pick or (lambda model, _label: chat_limit_default(controller, model)[0])
    limits = {}
    if model_id:
        limits[model_id] = pick(model_id, "Research model")
    if finalizer_model_id.strip() and finalizer_model_id.strip().lower() != model_id.strip().lower():
        limits[finalizer_model_id.strip()] = pick(finalizer_model_id.strip(), "Finalizer model")
    return limits


def settings_from_env(secret: Secret, controller) -> UISettings:
    """The settings an untouched sidebar yields: every value is its environment default (numbers clamped to the
    sidebar's ranges, which the sidebar itself requires). Never takes an API key from a client: GLM_API_KEY only."""
    return settings_for_run(secret, controller, None)


# --- per-run overrides (the HTTP API's typed Advanced settings) ------------------------------------------------------
#
# The API's per-run settings are a strict allowlist of the sidebar's non-secret Advanced settings. Each entry names
# the assemble_settings keyword it feeds (efforts and prices are flattened: effort_<phase>, price_*), its widget range
# (BOUNDS) or options (the sidebar's), and the AgentConfig setting it ends up in, so the contract can say which ones a
# named run profile pins. Overrides apply to ONE run's settings object; nothing here mutates the environment or any
# process-wide object.
#
# Server-controlled (never accepted from a client): the provider endpoint / transport (base_url, chat_path,
# search_path), every credential (GLM_API_KEY, DATABASE_URL), the extra request JSON (arbitrary payload merged into
# every chat request), and the in-flight request limits: RunManager.start applies chat_limits / search_limit to the
# process-wide ConcurrencyController, so they are not per-run settings.

@dataclass(frozen=True)
class RunOverride:
    name: str
    kind: str                       # int | float | bool | choice | model | text
    label: str
    group: str
    low: float | None = None
    high: float | None = None
    options: tuple = ()
    target: Any = None              # AgentConfig field or ("phase", phase, key) the value feeds
    help: str = ""
    nullable: bool = False          # None is a meaningful value (temperature: not sent)
    allow_empty: bool = False       # "" is a meaningful value (finalizer model: the research model)
    step: float | None = None


def _bounded(name: str, kind: str, label: str, group: str, target: Any = None, help: str = "",
             step: float | None = None) -> RunOverride:
    low, high = BOUNDS[name]
    return RunOverride(name, kind, label, group, low, high, target=target, help=help, step=step)


MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}")
SEARCH_ENGINE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SEARCH_BACKENDS = ("glm", "serper", "gemini", "duckduckgo")       # src/tools/search_backends (PR #46)
SEARCH_FALLBACKS = ("none", *SEARCH_BACKENDS)
SEARCH_BACKEND_SETTINGS = ("search_backend", "search_fallback_backend")
DATA_SOURCES = ("auto", "database", "snapshot")
ON_OFF = ("on", "off")
PRICE_RANGES = {"price_in": (0.0, 100.0), "price_out": (0.0, 100.0), "price_search": (0.0, 10.0)}
TEMPERATURE_RANGE = (0.0, 1.5)

RUN_OVERRIDES: tuple[RunOverride, ...] = (
    RunOverride("model_id", "model", "Research model id", "Model", help="GLM_MODEL. Used for every research/tool turn."),
    RunOverride("finalizer_model_id", "model", "Finalizer model id", "Model", allow_empty=True,
                help="GLM_FINALIZER_MODEL; empty = the research model."),
    RunOverride("search_backend", "choice", "Search backend", "Model", options=SEARCH_BACKENDS,
                help="glm = Z.ai search-prime; serper = Google results (SERPER_API_KEY); gemini = Gemini grounding, "
                     "URLs only (GEMINI_API_KEY); duckduckgo = keyless HTML search. A backend without its key is "
                     "unavailable. A named profile uses its own default (Production: glm) unless you choose one here."),
    RunOverride("search_fallback_backend", "choice", "Fallback search backend", "Model", options=SEARCH_FALLBACKS,
                help="Answers a search only when the primary backend returned 0 usable results (logged as "
                     "search_fallback_used). none = no fallback."),
    RunOverride("search_engine", "text", "GLM search engine", "Model", help="GLM_SEARCH_ENGINE (glm backend only)."),
    _bounded("search_credit_cap", "int", "Search credits per vehicle (0 = no cap)", "Research budget",
             help="Provider search credits as the provider reports them (serper's `credits`). At the cap every further "
                  "uncached search of the vehicle is refused (search_budget_exhausted)."),
    _bounded("chat_attempts", "int", "Chat max attempts (total, 1 = no retry)", "Model"),
    _bounded("search_attempts", "int", "Search max attempts (total)", "Model"),
    _bounded("chat_timeout", "int", "Chat read timeout per attempt (s)", "Model", step=30),
    _bounded("workers", "int", "Vehicle workers (vehicles researched at once)", "Concurrency",
             help="BATCH_MAX_WORKERS. Not a request limit: the shared provider pools guard every request."),
    _bounded("max_steps", "int", "Primary research turns (source acquisition)", "Research budget", "max_steps",
             "PRIMARY_RESEARCH_MAX_TURNS / AGENT_MAX_STEPS"),
    _bounded("no_artifact", "int", "Stop research after N turns that acquire nothing new (0 = off)", "Research budget",
             "primary_research_no_artifact_stop"),
    _bounded("idle_turns", "int", "Finalize after N turns with no new research artifact (0 = off)", "Research budget",
             "no_new_research_turns"),
    _bounded("tool_chars", "int", "Max chars per tool result sent to model", "Research budget",
             "max_tool_output_chars", step=500),
    RunOverride("recovery_on", "bool", "Tail recovery of unresolved requested fields", "Research budget",
                target="field_recovery_enabled", help="FIELD_RECOVERY_ENABLED"),
    _bounded("recovery_attempts", "int", "Retry attempts per failed field", "Research budget",
             "field_recovery_max_attempts"),
    _bounded("recovery_steps", "int", "Model turns per retry attempt", "Research budget", "field_recovery_max_steps"),
    _bounded("recovery_total", "int", "Total recovery turns per vehicle (0 = no cap)", "Research budget",
             "field_recovery_max_total_steps"),
    RunOverride("include_level3", "bool", "Include Level 3 open research", "Research budget", target="include_level3"),
    RunOverride("temperature", "float", "Temperature", "Research budget", *TEMPERATURE_RANGE, target="temperature",
                nullable=True, step=0.05, help="null = not sent (provider default)."),
    _bounded("max_tokens", "int", "max_tokens per model turn (0 = provider default)", "Research budget", "max_tokens",
             step=1024),
    *(RunOverride(f"effort_{phase}", "choice", f"{label} reasoning effort", "Reasoning effort",
                  options=tuple(EFFORT_OPTIONS), target=("phase", phase, "reasoning_effort"),
                  help=f"GLM_{phase.upper()}_REASONING_EFFORT / GLM_REASONING_EFFORT")
      for phase, label in EFFORT_PHASES),
    RunOverride("acquisition_mode", "choice", "Acquisition mode", "Experiment", options=tuple(ACQUISITION_MODES),
                target="acquisition_mode", help="ACQUISITION_MODE. contract = primary research is source acquisition "
                                                "only; legacy = the previous behaviour (benchmark baseline)."),
    RunOverride("card_choice", "choice", "Document card", "Experiment", options=("off", "on"),
                target="acquisition_document_card", help="ACQUISITION_DOCUMENT_CARD (contract research only)."),
    RunOverride("site_map_choice", "choice", "Importer site map", "Experiment", options=ON_OFF, target="site_map",
                help="SITE_MAP: ranked real URLs from the official sites' sitemaps (discovery only)."),
    RunOverride("grounded_choice", "choice", "Grounded candidates", "Experiment", options=ON_OFF,
                target="grounded_candidates", help="GROUNDED_CANDIDATES: candidates only (adjudication + admission "
                                                   "decide)."),
    RunOverride("recovery_mode", "choice", "Recovery mode", "Experiment", options=tuple(RECOVERY_MODES),
                target="recovery_mode", help="RECOVERY_MODE. reacquire / cluster / legacy."),
    _bounded("sweep_attempts", "int", "Document sweep max attempts", "Experiment",
             ("phase", "document_sweep", "max_attempts"), "GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS"),
    _bounded("sweep_fields", "int", "Document sweep max fields per chunk", "Experiment", "document_sweep_max_fields",
             "DOCUMENT_SWEEP_MAX_FIELDS"),
    _bounded("sweep_candidates", "int", "Document sweep max candidates per chunk", "Experiment",
             "document_sweep_max_candidates", "DOCUMENT_SWEEP_MAX_CANDIDATES"),
    RunOverride("price_in", "float", "USD per 1M input tokens", "Cost reporting", *PRICE_RANGES["price_in"],
                step=0.05, help="Used only to report the run's cost."),
    RunOverride("price_out", "float", "USD per 1M output tokens", "Cost reporting", *PRICE_RANGES["price_out"],
                step=0.05, help="Used only to report the run's cost."),
    RunOverride("price_search", "float", "USD per GLM web_search call", "Cost reporting",
                *PRICE_RANGES["price_search"], step=0.005, help="Used only to report the run's cost."),
    RunOverride("data_source", "choice", "Level 1.5 source", "Level 1.5 data", options=DATA_SOURCES,
                help="auto = the database when DATABASE_URL is set, else the frozen snapshot."),
)
RUN_OVERRIDES_BY_NAME = {o.name: o for o in RUN_OVERRIDES}
SERVER_CONTROLLED = (
    ("base_url", "API base URL", "Provider endpoint: server configuration (GLM_BASE_URL)."),
    ("chat_path", "Chat endpoint", "Provider endpoint: server configuration (GLM_CHAT_PATH)."),
    ("search_path", "GLM search endpoint", "Provider endpoint: server configuration (GLM_SEARCH_PATH)."),
    ("api_key", "API key", "Credential: GLM_API_KEY is read from the server environment only."),
    ("dsn", "Level 1.5 database", "Credential: DATABASE_URL is read from the server environment only."),
    ("extra_raw", "Extra request JSON", "Arbitrary payload merged into every chat request: server configuration "
                                        "(GLM_EXTRA_BODY)."),
    ("chat_limits", "Model in-flight requests", "Process-wide: RunManager.start applies it to the shared "
                                                "ConcurrencyController, so it is not a per-run setting."),
    ("search_limit", "Search-Prime in-flight requests", "Process-wide: RunManager.start applies it to the shared "
                                                        "ConcurrencyController, so it is not a per-run setting."),
)


def run_setting_defaults(secret: Secret) -> dict:
    """Every per-run setting's default: exactly what the sidebar's widgets start with (numbers clamped to BOUNDS)."""
    g = glm_defaults(secret)
    a = agent_defaults(secret)
    pricing = pricing_defaults(g["model_id"], secret)
    return {
        "model_id": g["model_id"], "finalizer_model_id": g["finalizer_model_id"], "search_backend": g["search_backend"],
        "search_fallback_backend": (secret("SEARCH_FALLBACK_BACKEND") or "none").strip().lower()
        if (secret("SEARCH_FALLBACK_BACKEND") or "none").strip().lower() in SEARCH_FALLBACKS else "none",
        "search_engine": g["search_engine"], "chat_attempts": clamp("chat_attempts", g["chat_attempts"]),
        "search_attempts": clamp("search_attempts", g["search_attempts"]),
        "chat_timeout": clamp("chat_timeout", g["chat_timeout"]), "workers": clamp("workers", workers_default(secret)),
        "max_steps": clamp("max_steps", a["max_steps"]), "no_artifact": clamp("no_artifact", a["no_artifact"]),
        "idle_turns": clamp("idle_turns", a["idle_turns"]), "tool_chars": clamp("tool_chars", a["tool_chars"]),
        "recovery_on": a["recovery_on"], "recovery_attempts": clamp("recovery_attempts", a["recovery_attempts"]),
        "recovery_steps": clamp("recovery_steps", a["recovery_steps"]),
        "recovery_total": clamp("recovery_total", a["recovery_total"]), "include_level3": a["include_level3"],
        "temperature": None, "max_tokens": 0,
        **{f"effort_{phase}": effort_default(secret, phase) for phase, _ in EFFORT_PHASES},
        "acquisition_mode": a["acquisition_mode"], "card_choice": a["card_choice"],
        "site_map_choice": a["site_map_choice"], "grounded_choice": a["grounded_choice"],
        "recovery_mode": a["recovery_mode"], "sweep_attempts": a["sweep_attempts"],
        "sweep_fields": clamp("sweep_fields", a["sweep_fields"]),
        "sweep_candidates": clamp("sweep_candidates", a["sweep_candidates"]),
        "price_in": pricing["input_per_mtok"], "price_out": pricing["output_per_mtok"],
        "price_search": pricing["web_search_per_call"], "data_source": "auto",
        "search_credit_cap": SEARCH_CREDIT_CAP_DEFAULT}


def _number(spec: RunOverride, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{spec.name} must be a number")
    if spec.kind == "int":
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{spec.name} must be an integer")
            value = int(value)
    else:
        value = float(value)
    if not spec.low <= value <= spec.high:
        raise ValueError(f"{spec.name} must be between {spec.low:g} and {spec.high:g}")
    return value


def validate_overrides(overrides: dict | None) -> dict:
    """The accepted per-run overrides (None values dropped: not overridden). ValueError for an unknown setting or a
    value outside the sidebar's range / options; the message never echoes the submitted value."""
    out = {}
    for name, value in (overrides or {}).items():
        spec = RUN_OVERRIDES_BY_NAME.get(name)
        if spec is None:
            raise ValueError(f"{str(name)[:60]!r} is not a per-run setting")
        if value is None:
            continue
        if spec.kind in ("int", "float"):
            value = _number(spec, value)
        elif spec.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif spec.kind == "choice":
            if value not in spec.options:
                raise ValueError(f"{name} must be one of: {', '.join(spec.options)}")
        elif spec.kind in ("model", "text"):
            pattern = MODEL_ID_PATTERN if spec.kind == "model" else SEARCH_ENGINE_PATTERN
            if not isinstance(value, str) or not (pattern.fullmatch(value) or (spec.allow_empty and value == "")):
                raise ValueError(f"{name} is not a valid {'model id' if spec.kind == 'model' else 'identifier'}")
        out[name] = value
    return out


def settings_for_run(secret: Secret, controller, overrides: dict | None = None,
                     profile: str | None = None) -> UISettings:
    """ONE run's settings: the sidebar's defaults (run_setting_defaults) with the validated per-run `overrides`, through
    the same assemble_settings the sidebar uses. Model-dependent defaults (prices, in-flight limits) follow the chosen
    model, as the sidebar's do. Server-controlled values (endpoints, credentials, extra JSON, in-flight limits) always
    come from the server. A named run `profile` supplies its own search backend / fallback defaults
    (run_profiles.PROFILE_SEARCH) unless the run chooses them."""
    from .run_profiles import profile_search

    chosen = validate_overrides(overrides)
    g = glm_defaults(secret)
    a = agent_defaults(secret)
    v = {**run_setting_defaults(secret), **profile_search(profile), **chosen}
    pricing = pricing_defaults(v["model_id"], secret)
    prices = [chosen.get(name, pricing[key]) for name, key in (("price_in", "input_per_mtok"),
                                                              ("price_out", "output_per_mtok"),
                                                              ("price_search", "web_search_per_call"))]
    settings = assemble_settings(
        model_id=v["model_id"], finalizer_model_id=v["finalizer_model_id"], base_url=g["base_url"],
        chat_path=g["chat_path"], api_key=secret("GLM_API_KEY"), api_key_from_ui=False,
        search_backend=v["search_backend"], search_path=g["search_path"], search_engine=v["search_engine"],
        chat_attempts=v["chat_attempts"], search_attempts=v["search_attempts"], chat_timeout=v["chat_timeout"],
        workers=v["workers"], chat_limits=chat_limits_for(controller, v["model_id"], v["finalizer_model_id"]),
        search_limit=clamp("search_limit", search_limit_default(controller)),
        max_steps=v["max_steps"], no_artifact=v["no_artifact"], idle_turns=v["idle_turns"], tool_chars=v["tool_chars"],
        recovery_on=v["recovery_on"], recovery_attempts=v["recovery_attempts"], recovery_steps=v["recovery_steps"],
        recovery_total=v["recovery_total"], include_level3=v["include_level3"], temperature=v["temperature"],
        max_tokens=v["max_tokens"], efforts={phase: v[f"effort_{phase}"] for phase, _ in EFFORT_PHASES},
        extra_raw=a["extra_raw"], acquisition_mode=v["acquisition_mode"], card_choice=v["card_choice"],
        site_map_choice=v["site_map_choice"], grounded_choice=v["grounded_choice"], recovery_mode=v["recovery_mode"],
        sweep_attempts=v["sweep_attempts"], sweep_fields=v["sweep_fields"], sweep_candidates=v["sweep_candidates"],
        pricing=pricing_from(pricing, *prices), data_source=v["data_source"], dsn=dsn_default(secret),
        env_overrides=env_overrides(secret))
    settings.search_fallback_backend = v["search_fallback_backend"]
    settings.search_credit_cap = int(v["search_credit_cap"])
    return settings


def pinned_by_named_profiles() -> set[str]:
    """Per-run settings a named run profile replaces with its own / the code default (they apply to Custom only)."""
    from .run_profiles import PRODUCTION, pinned_values

    values, phases = pinned_values(PRODUCTION)       # every named profile pins the same set of settings
    pinned_phase_keys = {("phase", phase, key) for phase, keys in phases.items() for key in keys}
    return {o.name for o in RUN_OVERRIDES if o.target is not None
            and (o.target in pinned_phase_keys if isinstance(o.target, tuple) else o.target in values)}


def run_settings_contract(secret: Secret, controller) -> dict:
    """What a client may set per run: every allowed setting with its default, range / options and whether a named
    profile pins it; what stays server-controlled; the process-wide in-flight limits (read-only). No secret."""
    from .run_profiles import NAMED_PROFILES, PROFILE_LABELS

    from .run_profiles import PROFILE_SEARCH
    from .tools.search_backends import backend_status

    defaults = run_setting_defaults(secret)
    pinned = pinned_by_named_profiles()
    # PR #46: a backend without its key is listed as unavailable (the form disables it; a start selecting it is refused)
    unavailable = {name: st["reason"] for name, st in backend_status(lambda n: secret(n)).items()
                   if not st["available"] and name != "glm"}

    def profile_defaults(name: str) -> dict:
        return {p: values[name] for p, values in PROFILE_SEARCH.items()}
    model, finalizer = defaults["model_id"], defaults["finalizer_model_id"].strip()

    def limits(name: str) -> dict:
        current, maximum, provider = chat_limit_default(controller, name)
        return {"model": name, "current": current, "maximum": maximum, "provider_limit": provider}

    groups = list(dict.fromkeys(o.group for o in RUN_OVERRIDES))
    return {
        "settings": [{"name": o.name, "kind": o.kind, "label": o.label, "group": o.group, "help": o.help,
                      "default": defaults[o.name], "min": o.low, "max": o.high, "step": o.step,
                      "options": list(o.options), "nullable": o.nullable, "allow_empty": o.allow_empty,
                      "pinned_by_named_profile": o.name in pinned,
                      **({"unavailable_options": unavailable, "profile_defaults": profile_defaults(o.name)}
                         if o.name in SEARCH_BACKEND_SETTINGS else {})} for o in RUN_OVERRIDES],
        "groups": groups,
        "server_controlled": [{"name": n, "label": label, "reason": reason} for n, label, reason in SERVER_CONTROLLED],
        "concurrency": {
            "research_model": limits(model) if model else None,
            "finalizer_model": limits(finalizer) if finalizer and finalizer.lower() != model.lower() else None,
            "search": {"current": clamp("search_limit", search_limit_default(controller)),
                       "provider_limit": SEARCH_PRIME_PROVIDER_LIMIT},
            "note": "Shared by every run in this server process; not a per-run setting."},
        "named_profiles": [{"id": p, "label": PROFILE_LABELS[p]} for p in NAMED_PROFILES],
        "profile_note": "With a named run profile the settings marked pinned come from the profile (code defaults), "
                        "not from these values; the Custom profile uses them.",
        "env_overrides": env_overrides(secret)}


# --- one research request --------------------------------------------------------------------------------------------

def build_research_request(settings: UISettings, secret: Secret, vehicles: list[dict], label: str, scope: str,
                           key: str | None, profile: str):
    """The ResearchRequest every launcher hands to RunManager.start (the dashboard's Start / Restart, the API)."""
    from .jobs.manager import ResearchRequest

    return ResearchRequest(vehicles=vehicles, label=label, scope=scope, settings=settings.glm_settings(),
                           agent_cfg=settings.agent_config(secret, profile), tool_cfg=settings.tool_config(secret),
                           pricing=settings.pricing, prompt_version=PROMPT_VERSION, data_source=settings.data_source,
                           dsn=settings.dsn, workers=settings.workers, chat_limits=settings.chat_limits,
                           search_limit=settings.search_limit, idempotency_key=key)


# --- the checks that gate a start ------------------------------------------------------------------------------------

def settings_checks(settings: UISettings, secret: Secret, paths):
    """Configuration checks of the NEXT run (app_config.validate_config over what these settings will use: their
    model, API-key presence and search backend over the environment), plus an invalid extra-request JSON. The
    dashboard disables Start research and the API refuses a start while `app_config.blocking_errors` of these exist."""
    from .app_config import Check, validate_config

    def effective_lookup(name: str) -> str:
        if name == "GLM_MODEL":
            return settings.model_id
        if name == "GLM_API_KEY":
            return "set" if settings.api_key else ""
        if name == "SEARCH_BACKEND":
            return settings.search_backend
        if name == "SEARCH_FALLBACK_BACKEND":
            return settings.search_fallback_backend
        return secret(name)

    checks = validate_config(effective_lookup, paths)
    if settings.extra_error:
        checks.append(Check("Configuration", "error", "Invalid value", settings.extra_error))
    return checks

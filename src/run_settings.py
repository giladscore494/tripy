"""Run settings shared by every launcher (framework-neutral; never imports Streamlit).

The Streamlit sidebar (src/ui/settings_panel.py) and the HTTP API (src/api) start runs with the SAME settings object,
built by the SAME code:

    defaults from the environment   glm_defaults / agent_defaults / effort_default / pricing_defaults / ...
    widget ranges                   BOUNDS (the sidebar's number inputs use them; the API clamps env values to them)
    one settings object             assemble_settings(...) -> UISettings
    one research request            build_research_request(settings, ...) -> jobs.manager.ResearchRequest

`settings_from_env(lookup, controller)` is exactly what the sidebar yields when nobody touches it: the API has no
Advanced settings, so its runs use the server's configuration, like an untouched dashboard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

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
          "sweep_candidates": (1, 500), "search_limit": (1, SEARCH_PRIME_PROVIDER_LIMIT)}
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
        return tool_config_from_env(env=secret, search_backend=self.search_backend)


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
            "search_backend": "glm" if (secret("SEARCH_BACKEND") or "glm") == "glm" else "duckduckgo",
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
    g = glm_defaults(secret)
    a = agent_defaults(secret)
    pricing = pricing_defaults(g["model_id"], secret)
    return assemble_settings(
        model_id=g["model_id"], finalizer_model_id=g["finalizer_model_id"], base_url=g["base_url"],
        chat_path=g["chat_path"], api_key=secret("GLM_API_KEY"), api_key_from_ui=False,
        search_backend=g["search_backend"], search_path=g["search_path"], search_engine=g["search_engine"],
        chat_attempts=clamp("chat_attempts", g["chat_attempts"]),
        search_attempts=clamp("search_attempts", g["search_attempts"]),
        chat_timeout=clamp("chat_timeout", g["chat_timeout"]), workers=clamp("workers", workers_default(secret)),
        chat_limits=chat_limits_for(controller, g["model_id"], g["finalizer_model_id"]),
        search_limit=clamp("search_limit", search_limit_default(controller)),
        max_steps=clamp("max_steps", a["max_steps"]), no_artifact=clamp("no_artifact", a["no_artifact"]),
        idle_turns=clamp("idle_turns", a["idle_turns"]), tool_chars=clamp("tool_chars", a["tool_chars"]),
        recovery_on=a["recovery_on"], recovery_attempts=clamp("recovery_attempts", a["recovery_attempts"]),
        recovery_steps=clamp("recovery_steps", a["recovery_steps"]),
        recovery_total=clamp("recovery_total", a["recovery_total"]), include_level3=a["include_level3"],
        temperature=None, max_tokens=0, efforts={phase: effort_default(secret, phase) for phase, _ in EFFORT_PHASES},
        extra_raw=a["extra_raw"], acquisition_mode=a["acquisition_mode"], card_choice=a["card_choice"],
        site_map_choice=a["site_map_choice"], grounded_choice=a["grounded_choice"], recovery_mode=a["recovery_mode"],
        sweep_attempts=a["sweep_attempts"], sweep_fields=clamp("sweep_fields", a["sweep_fields"]),
        sweep_candidates=clamp("sweep_candidates", a["sweep_candidates"]),
        pricing=pricing_from(pricing, pricing["input_per_mtok"], pricing["output_per_mtok"],
                             pricing["web_search_per_call"]),
        data_source="auto", dsn=dsn_default(secret), env_overrides=env_overrides(secret))


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
        return secret(name)

    checks = validate_config(effective_lookup, paths)
    if settings.extra_error:
        checks.append(Check("Configuration", "error", "Invalid value", settings.extra_error))
    return checks

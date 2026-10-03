"""Per-phase inference settings: research, document_sweep, recovery and finalizer may each use their own model,
reasoning effort, thinking mode, max_tokens, temperature and read timeout. Anything not set inherits the run's global
settings, then the built-in phase default (PHASE_DEFAULTS).

    GLM_<PHASE>_REASONING_EFFORT  low | medium | high | max (empty: inherit GLM_REASONING_EFFORT, else the phase
                              default: research high, document_sweep / recovery / finalizer low). Sent as the
                              top-level `reasoning_effort` request field.
    GLM_<PHASE>_THINKING      enabled | disabled            (empty: inherit GLM_THINKING). "disabled" is never sent:
                              glm-5.3 models always think (HTTP 400, code 1210). It is mapped to "no thinking object +
                              reasoning_effort low" unless an effort is set explicitly (see thinking_disabled_mapping).
    GLM_<PHASE>_MAX_TOKENS    integer                       (empty: inherit)
    GLM_<PHASE>_TEMPERATURE   float                         (empty: inherit)
    GLM_<PHASE>_TIMEOUT_S     read timeout per HTTP attempt (empty: inherit GLM_CHAT_TIMEOUT_S; retries unchanged)
    GLM_<PHASE>_MAX_ATTEMPTS  total HTTP attempts per chat request (empty: the phase default, else
                              GLM_CHAT_MAX_ATTEMPTS; document_sweep and recovery default to 1: no identical retry
                              after a timeout)
    GLM_DOCUMENT_SWEEP_MODEL / GLM_RECOVERY_MODEL           (empty: GLM_MODEL)

PHASE is RESEARCH, DOCUMENT_SWEEP, RECOVERY or FINALIZER. The research model stays GLM_MODEL and the finalizer model
GLM_FINALIZER_MODEL (existing settings). Operational only: a phase setting never changes what is admitted or true.
"""

from __future__ import annotations

import os
from typing import Any, Callable

PHASES = ("research", "document_sweep", "recovery", "finalizer")
KEYS = ("model", "reasoning_effort", "thinking", "max_tokens", "temperature", "timeout_s", "max_attempts")
REASONING_EFFORTS = ("low", "medium", "high", "max")
# a stored (UI) choice meaning "send no reasoning_effort at all" (the provider's own default)
PROVIDER_DEFAULT = "provider_default"
# Built-in phase defaults (an env value overrides them). document_sweep / recovery: one HTTP attempt, i.e. no identical
# retry of a large request after a read timeout (the sweep's open fields flow to recovery; a failed recovery attempt
# ends that attempt only). reasoning_effort: the provider default is the heaviest level, which made the long-output
# phases hit the read timeout; only research reasons at "high".
PHASE_DEFAULTS: dict[str, dict] = {"research": {"reasoning_effort": "high"},
                                   "document_sweep": {"max_attempts": 1, "reasoning_effort": "low"},
                                   "recovery": {"max_attempts": 1, "reasoning_effort": "low"},
                                   "finalizer": {"reasoning_effort": "low"}}
# the effort a configured thinking "disabled" maps to when no effort is set explicitly
DISABLED_THINKING_EFFORT = "low"
# trace.phase_group(phase) -> settings phase
GROUP_TO_PHASE = {"research": "research", "document_sweep": "document_sweep", "field_recovery": "recovery",
                  "finalization": "finalizer"}
ENV_MODEL_PHASES = ("document_sweep", "recovery")     # research = GLM_MODEL, finalizer = GLM_FINALIZER_MODEL


def _parse(key: str, raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    try:
        if key == "max_tokens":
            return int(raw)
        if key == "max_attempts":
            return int(raw) if int(raw) >= 1 else None
        if key in ("temperature", "timeout_s"):
            return float(raw)
    except ValueError:
        return None
    if key == "thinking":
        return raw.lower() if raw.lower() in ("enabled", "disabled") else None
    if key == "reasoning_effort":
        return parse_effort(raw)
    return raw


def parse_effort(raw: str | None) -> str | None:
    """low | medium | high | max | provider_default, else None (empty / unknown = inherit)."""
    value = str(raw or "").strip().lower().replace(" ", "_")
    return value if value in REASONING_EFFORTS + (PROVIDER_DEFAULT,) else None


def phase_settings_from_env(env: Callable[[str], str | None] = os.environ.get) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for phase in PHASES:
        values = {}
        for key in KEYS:
            if key == "model" and phase not in ENV_MODEL_PHASES:
                continue
            value = _parse(key, env(f"GLM_{phase.upper()}_{key.upper()}") or "")
            if value is not None:
                values[key] = value
        if values:
            out[phase] = values
    return out


def clean(settings: dict | None) -> dict[str, dict]:
    """Only known phases and keys with a value."""
    out = {}
    for phase, values in (settings or {}).items():
        if phase in PHASES and isinstance(values, dict):
            # the research model is the client's model (GLM_MODEL): it names the run, its pricing and its logs
            kept = {k: v for k, v in values.items() if k in KEYS and v not in (None, "")
                    and not (phase == "research" and k == "model")}
            if kept:
                out[phase] = kept
    return out


def extra_body_disables_thinking(extra_body: dict | None) -> bool:
    thinking = (extra_body or {}).get("thinking") if isinstance(extra_body, dict) else None
    return isinstance(thinking, dict) and str(thinking.get("type") or "").strip().lower() == "disabled"


def thinking_disabled_mapping(thinking: str, explicit_effort: str | None, extra_body: dict | None = None) -> dict:
    """THE mapping of a configured thinking "disabled" (env, UI, a saved request, an old profile, GLM_EXTRA_BODY):
    send no thinking object and reasoning_effort low unless an effort is set explicitly. {thinking (never "disabled"),
    effort (None = not decided here), mapped (bool), source}."""
    if thinking == "disabled" or (not thinking and extra_body_disables_thinking(extra_body)):
        return {"thinking": "", "effort": explicit_effort or DISABLED_THINKING_EFFORT, "mapped": True,
                "source": "thinking" if thinking == "disabled" else "extra_body"}
    return {"thinking": thinking, "effort": explicit_effort, "mapped": False, "source": None}


def for_phase(config, phase_name: str | None) -> dict:
    """Resolved request settings of one model call: {phase, model (None = caller/client default), reasoning_effort
    (None = not sent), thinking ("" | "enabled", never "disabled"), max_tokens, temperature, timeout_s (None = client
    default), max_attempts (None = client default)}. reasoning_effort: the phase's own, else the global
    (GLM_REASONING_EFFORT), else the phase default; a configured thinking "disabled" is mapped (see
    thinking_disabled_mapping) and reported in `thinking_disabled_mapped`."""
    from .storage.trace import phase_group

    phase = GROUP_TO_PHASE.get(phase_group(phase_name), "research")
    own = clean(getattr(config, "phase_settings", None)).get(phase, {})
    defaults = PHASE_DEFAULTS.get(phase, {})
    own_effort = parse_effort(own.get("reasoning_effort"))
    global_effort = parse_effort(getattr(config, "reasoning_effort", "") or "")
    explicit = own_effort or global_effort
    thinking = str(own.get("thinking", getattr(config, "thinking", "") or "") or "").lower()
    mapping = thinking_disabled_mapping(thinking, explicit, getattr(config, "extra_body", None))
    effort = mapping["effort"] or defaults.get("reasoning_effort")
    source = ("phase" if own_effort else "global" if global_effort else
              "thinking_disabled" if mapping["mapped"] else "phase_default" if effort else None)
    return {
        "phase": phase,
        "model": own.get("model"),
        "reasoning_effort": None if effort == PROVIDER_DEFAULT else effort,
        "reasoning_effort_source": source,
        "thinking": mapping["thinking"],
        "thinking_disabled_mapped": mapping["mapped"],
        "thinking_disabled_source": mapping["source"],
        "max_tokens": own.get("max_tokens", getattr(config, "max_tokens", None)),
        "temperature": own.get("temperature", getattr(config, "temperature", None)),
        "timeout_s": own.get("timeout_s"),
        "max_attempts": own.get("max_attempts", defaults.get("max_attempts")),
        "overridden": sorted(own),
    }


def describe(config, defaults: dict) -> dict[str, dict]:
    """Effective settings of every phase for result.json / the UI (`defaults`: the global model ids, timeout and chat
    max attempts)."""
    out = {}
    for phase in PHASES:
        group = {"recovery": "field_recovery", "finalizer": "finalization"}.get(phase, phase)
        r = for_phase(config, group)
        default_model = defaults.get("finalizer_model") if phase == "finalizer" else defaults.get("research_model")
        out[phase] = {"model": r["model"] or default_model,
                      "reasoning_effort": r["reasoning_effort"] or PROVIDER_DEFAULT,
                      "thinking": r["thinking"] or "provider_default",
                      "max_tokens": r["max_tokens"] or "provider_default",
                      "temperature": r["temperature"] if r["temperature"] is not None else "provider_default",
                      "timeout_s": r["timeout_s"] or defaults.get("timeout_s"),
                      "max_attempts": r["max_attempts"] or defaults.get("chat_max_attempts"),
                      "overridden": r["overridden"]}
    return out

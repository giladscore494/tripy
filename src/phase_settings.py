"""Per-phase inference settings: research, document_sweep, recovery and finalizer may each use their own model,
thinking mode, max_tokens, temperature and read timeout. Anything not set inherits the run's global settings, so
the default behaviour is exactly the previous one (one configuration for every phase).

    GLM_<PHASE>_THINKING      enabled | disabled            (empty: inherit GLM_THINKING)
    GLM_<PHASE>_MAX_TOKENS    integer                       (empty: inherit)
    GLM_<PHASE>_TEMPERATURE   float                         (empty: inherit)
    GLM_<PHASE>_TIMEOUT_S     read timeout per HTTP attempt (empty: inherit GLM_CHAT_TIMEOUT_S; retries unchanged)
    GLM_<PHASE>_MAX_ATTEMPTS  total HTTP attempts per chat request (empty: the phase default, else
                              GLM_CHAT_MAX_ATTEMPTS; document_sweep defaults to 1: no identical retry after a timeout)
    GLM_DOCUMENT_SWEEP_MODEL / GLM_RECOVERY_MODEL           (empty: GLM_MODEL)

PHASE is RESEARCH, DOCUMENT_SWEEP, RECOVERY or FINALIZER. The research model stays GLM_MODEL and the finalizer model
GLM_FINALIZER_MODEL (existing settings). Operational only: a phase setting never changes what is admitted or true.
"""

from __future__ import annotations

import os
from typing import Any, Callable

PHASES = ("research", "document_sweep", "recovery", "finalizer")
KEYS = ("model", "thinking", "max_tokens", "temperature", "timeout_s", "max_attempts")
# Built-in phase defaults (an env value overrides them). document_sweep: one HTTP attempt, i.e. no identical retry of
# a large packet after a read timeout (the sweep's open fields flow to recovery instead).
PHASE_DEFAULTS: dict[str, dict] = {"document_sweep": {"max_attempts": 1}}
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
    return raw


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


def for_phase(config, phase_name: str | None) -> dict:
    """Resolved request settings of one model call: {phase, model (None = caller/client default), thinking,
    max_tokens, temperature, timeout_s (None = client default), max_attempts (None = client default)}."""
    from .storage.trace import phase_group

    phase = GROUP_TO_PHASE.get(phase_group(phase_name), "research")
    own = clean(getattr(config, "phase_settings", None)).get(phase, {})
    return {
        "phase": phase,
        "model": own.get("model"),
        "thinking": own.get("thinking", getattr(config, "thinking", "") or ""),
        "max_tokens": own.get("max_tokens", getattr(config, "max_tokens", None)),
        "temperature": own.get("temperature", getattr(config, "temperature", None)),
        "timeout_s": own.get("timeout_s"),
        "max_attempts": own.get("max_attempts", PHASE_DEFAULTS.get(phase, {}).get("max_attempts")),
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
                      "thinking": r["thinking"] or "provider_default",
                      "max_tokens": r["max_tokens"] or "provider_default",
                      "temperature": r["temperature"] if r["temperature"] is not None else "provider_default",
                      "timeout_s": r["timeout_s"] or defaults.get("timeout_s"),
                      "max_attempts": r["max_attempts"] or defaults.get("chat_max_attempts"),
                      "overridden": r["overridden"]}
    return out

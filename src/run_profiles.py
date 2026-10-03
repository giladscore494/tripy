"""Run profiles: the experiment configuration of ONE run, chosen in the UI per run instead of by editing env.

Environment variables stay the source of DEFAULTS (agent_config_from_env). A run profile or a UI choice wins over env
for that run:

    Production (default)        contract, card off, sweep thinking disabled, sweep max attempts 1, sweep 12 / 16
    Benchmark: Baseline         legacy,   card off, sweep thinking disabled, sweep max attempts 1, sweep 12 / 16
    Benchmark: Treatment        contract, card off, sweep thinking disabled, sweep max attempts 1, sweep 12 / 16
    Benchmark: Treatment + card contract, card on,  sweep thinking disabled, sweep max attempts 1, sweep 12 / 16
    Custom                      the Advanced settings as edited (their defaults come from env)

A NAMED profile also pins every experiment-relevant setting listed in ENV_OVERRIDE_VARS to its code default, so a
stale env value (e.g. DOCUMENT_SWEEP_MAX_FIELDS=30 left on a deployment) cannot leak into a profiled run. Everything
else (models, timeouts, recovery, other phases' settings, ...) comes from the Advanced settings / env as before.
UI phase settings are MERGED per phase and per key over the env phase settings, never replacing them.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Callable

PRODUCTION = "production"
BASELINE = "benchmark_baseline"
TREATMENT = "benchmark_treatment"
TREATMENT_CARD = "benchmark_treatment_card"
CUSTOM = "custom"
PROFILE_LABELS = {PRODUCTION: "Production", BASELINE: "Benchmark: Baseline", TREATMENT: "Benchmark: Treatment",
                  TREATMENT_CARD: "Benchmark: Treatment + card", CUSTOM: "Custom"}
PROFILES = tuple(PROFILE_LABELS)
ARMS = (BASELINE, TREATMENT, TREATMENT_CARD)          # the A/B launcher's arms

_SWEEP = {"document_sweep_max_fields": 12, "document_sweep_max_candidates": 16}
_SWEEP_PHASE = {"document_sweep": {"thinking": "disabled", "max_attempts": 1}}
NAMED_PROFILES: dict[str, dict] = {
    PRODUCTION: {"acquisition_mode": "contract", "acquisition_document_card": False, **_SWEEP},
    BASELINE: {"acquisition_mode": "legacy", "acquisition_document_card": False, **_SWEEP},
    TREATMENT: {"acquisition_mode": "contract", "acquisition_document_card": False, **_SWEEP},
    TREATMENT_CARD: {"acquisition_mode": "contract", "acquisition_document_card": True, **_SWEEP},
}

# (env variable, AgentConfig field | ("phase", phase, key), kind): the settings a named profile pins and the
# Advanced settings list when env differs from the code default
ENV_OVERRIDE_VARS: tuple[tuple[str, Any, str], ...] = (
    ("ACQUISITION_MODE", "acquisition_mode", "str"),
    ("ACQUISITION_DOCUMENT_CARD", "acquisition_document_card", "bool"),
    ("DOCUMENT_SWEEP_MAX_FIELDS", "document_sweep_max_fields", "int"),
    ("DOCUMENT_SWEEP_MAX_CANDIDATES", "document_sweep_max_candidates", "int"),
    ("DOCUMENT_SWEEP_MAX_PACKET_CHARS", "document_sweep_packet_max_chars", "int"),
    ("DOCUMENT_SWEEP_MAX_TURNS", "document_sweep_max_turns", "int"),
    ("DOCUMENT_SWEEP_CANDIDATES_PER_FIELD", "document_sweep_candidates_per_field", "int"),
    ("GLM_DOCUMENT_SWEEP_THINKING", ("phase", "document_sweep", "thinking"), "str"),
    ("GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS", ("phase", "document_sweep", "max_attempts"), "int"),
    ("PRIMARY_RESEARCH_MAX_TURNS", "max_steps", "int"),
    ("PRIMARY_RESEARCH_HARD_MAX_TURNS", "primary_research_hard_max_turns", "int"),
    ("PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS", "primary_research_min_base_documents", "int"),
    ("PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE", "primary_research_min_base_scoped_coverage", "float"),
)


def _agent_defaults() -> dict:
    from .agent import AgentConfig

    return {f.name: f.default for f in dataclasses.fields(AgentConfig) if f.default is not dataclasses.MISSING}


def code_default(target: Any) -> Any:
    """The code default of an AgentConfig field or of a phase setting ("" = provider default / inherit)."""
    if isinstance(target, tuple):
        from .phase_settings import PHASE_DEFAULTS

        return PHASE_DEFAULTS.get(target[1], {}).get(target[2], "")
    return _agent_defaults()[target]


def _parse(raw: str, kind: str) -> Any:
    from .agent import _env_bool

    raw = raw.strip()
    try:
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
    except ValueError:
        return raw
    if kind == "bool":
        parsed = _env_bool(raw)
        return raw if parsed is None else parsed
    return raw.lower()


def _same(value: Any, default: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(default, (int, float)):
        return float(value) == float(default)
    return value == default


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return "provider default" if value == "" else str(value)


def env_overrides(env: Callable[[str], str | None] = os.environ.get) -> list[dict]:
    """Every ENV_OVERRIDE_VARS variable whose env value differs from the code default (unset / empty = default):
    [{var, value, code_default, text: "VAR = value (code default X)"}]. Informational only."""
    out = []
    for var, target, kind in ENV_OVERRIDE_VARS:
        raw = (env(var) or "").strip()
        if not raw:
            continue
        value, default = _parse(raw, kind), code_default(target)
        if _same(value, default):
            continue
        out.append({"var": var, "value": raw, "code_default": _shown(default),
                    "text": f"{var} = {raw} (code default {_shown(default)})"})
    return out


def merge_phase_settings(base: dict | None, overlay: dict | None) -> dict[str, dict]:
    """`overlay` merged over `base` per phase and per key; an overlay value None removes the key (e.g. the UI's
    "provider default" sweep thinking over an env value). Empty phases are dropped."""
    out = {phase: dict(values) for phase, values in (base or {}).items() if isinstance(values, dict)}
    for phase, values in (overlay or {}).items():
        merged = out.setdefault(phase, {})
        for key, value in (values or {}).items():
            if value is None or value == "":
                merged.pop(key, None)
            else:
                merged[key] = value
    return {phase: values for phase, values in out.items() if values}


def pinned_values(profile: str) -> tuple[dict, dict]:
    """(AgentConfig values, phase settings) a NAMED profile imposes: the code default of every ENV_OVERRIDE_VARS
    AgentConfig setting, then the profile's own values; its document_sweep phase values (thinking, max_attempts) cover
    both phase variables of ENV_OVERRIDE_VARS. ({}, {}) for Custom / unknown."""
    if profile not in NAMED_PROFILES:
        return {}, {}
    values = {target: code_default(target) for _, target, _ in ENV_OVERRIDE_VARS if not isinstance(target, tuple)}
    values.update(NAMED_PROFILES[profile])
    return values, {phase: dict(keys) for phase, keys in _SWEEP_PHASE.items()}


def build_agent_config(env: Callable[[str], str | None] = os.environ.get, overrides: dict | None = None,
                       profile: str = CUSTOM):
    """The AgentConfig of one run: env defaults, then the Advanced-settings `overrides` (its `phase_settings` merged
    over the env phase settings), then a named profile's pinned values (which ignore env for those settings)."""
    from .agent import agent_config_from_env
    from .phase_settings import phase_settings_from_env

    values = dict(overrides or {})
    phases = merge_phase_settings(phase_settings_from_env(env), values.pop("phase_settings", None))
    pinned, pinned_phases = pinned_values(profile)
    values.update(pinned)
    phases = merge_phase_settings(phases, pinned_phases)
    return agent_config_from_env(env, **values, phase_settings=phases, run_profile=profile or "")


def profile_label(profile: str | None) -> str:
    return PROFILE_LABELS.get(profile or "", profile or "—")

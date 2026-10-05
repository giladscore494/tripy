"""Run profiles: the experiment configuration of ONE run, chosen in the UI per run instead of by editing env.

Environment variables stay the source of DEFAULTS (agent_config_from_env). A run profile or a UI choice wins over env
for that run:

    Production (default)        contract, card off, site map on,  grounded candidates on,  recovery reacquire
    Benchmark: Baseline         legacy,   card off, site map off, grounded candidates off, recovery cluster
    Benchmark: Treatment        contract, card off, site map on,  grounded candidates on,  recovery reacquire
    Benchmark: Treatment + card contract, card on,  site map on,  grounded candidates on,  recovery reacquire
    Custom                      the Advanced settings as edited (their defaults come from env)

Baseline is the pre-redesign engine (no grounded candidates, the cluster recovery agent). The importer SITE MAP IS PART
OF THE TREATMENT: it only acts in contract acquisition and reacquire recovery, neither of which Baseline runs, so
Baseline has it off and diagnostics.run_configuration() records `site_map_used` (the switch AND a stage that uses it).

Single runs of the A/B arms (Baseline, Treatment, Treatment + card) started OUTSIDE a series run with cross-run
research memory and negative-route blocking OFF (isolate_single_run), so an arm never inherits verified facts or
"unproductive route" verdicts of earlier runs. A run of an A/B series is unchanged: it already gets a private cache and
research memory per planned run (src/jobs/manager.py). Production keeps memory on. run_configuration() records both
flags.

Every named profile also runs: sweep mode adjudication, sweep 12 fields / 16 candidates, deterministic final assembly,
reasoning effort research high / document sweep low / recovery low / finalizer low, no thinking object, one HTTP
attempt for sweep and recovery requests (PHASE_DEFAULTS in src/phase_settings.py).

A NAMED profile also pins every experiment-relevant setting listed in ENV_OVERRIDE_VARS to its code default, so a
stale env value (e.g. DOCUMENT_SWEEP_MAX_FIELDS=30 or SWEEP_MODE=legacy left on a deployment) cannot leak into a
profiled run: among them the cluster recovery budgets (CLUSTER_*), the re-acquisition stage search cap
(REACQUIRE_STAGE_SEARCH_CAP, 8 billable searches per vehicle), the recovery turn cap
(FIELD_RECOVERY_MAX_TOTAL_STEPS) and the research / recovery HTTP attempts. A named profile ignores extra_body
(GLM_EXTRA_BODY) entirely. Everything else (models, timeouts, other phases' thinking / max_tokens ...) comes from the
Advanced settings / env as before. The research model stays outside profiles. UI phase settings are MERGED per phase
and per key over the env phase settings, never replacing them.
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
# (phase, key) settings every named profile pins to the code default (PHASE_DEFAULTS); a phase's thinking listed here
# is pinned to "inherit" (removed): the global thinking is pinned to "" (provider default, no thinking object)
_PINNED_PHASE_KEYS = (("research", "reasoning_effort"), ("document_sweep", "reasoning_effort"),
                      ("document_sweep", "max_attempts"), ("document_sweep", "thinking"),
                      ("research", "max_attempts"),
                      ("recovery", "reasoning_effort"), ("recovery", "max_attempts"),
                      ("finalizer", "reasoning_effort"))
_YIELD = {"site_map": True, "grounded_candidates": True, "recovery_mode": "reacquire"}       # PR #31
NAMED_PROFILES: dict[str, dict] = {
    PRODUCTION: {"acquisition_mode": "contract", "acquisition_document_card": False, **_SWEEP, **_YIELD},
    # the site map is part of the Treatment (Baseline's legacy acquisition and cluster recovery never use it)
    BASELINE: {"acquisition_mode": "legacy", "acquisition_document_card": False, **_SWEEP,
               "site_map": False, "grounded_candidates": False, "recovery_mode": "cluster"},
    TREATMENT: {"acquisition_mode": "contract", "acquisition_document_card": False, **_SWEEP, **_YIELD},
    TREATMENT_CARD: {"acquisition_mode": "contract", "acquisition_document_card": True, **_SWEEP, **_YIELD},
}

# PR #46 (S3): each named profile's default search backend / fallback. A run's own choice in the run settings wins (they
# are not pinned); without one a named profile uses these, never env. Production stays glm until the operator picks the
# bake-off's winner in the run settings.
PROFILE_SEARCH: dict[str, dict] = {p: {"search_backend": "glm", "search_fallback_backend": "none"}
                                   for p in (PRODUCTION, BASELINE, TREATMENT, TREATMENT_CARD)}


def profile_search(profile: str | None) -> dict:
    """The named profile's search defaults ({} for Custom / unknown: the env defaults apply)."""
    return dict(PROFILE_SEARCH.get(profile or "", {}))


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
    ("SWEEP_MODE", "sweep_mode", "str"),
    ("FINAL_ASSEMBLY", "final_assembly", "str"),
    ("GLM_REASONING_EFFORT", "reasoning_effort", "str"),
    ("GLM_RESEARCH_REASONING_EFFORT", ("phase", "research", "reasoning_effort"), "str"),
    ("GLM_DOCUMENT_SWEEP_REASONING_EFFORT", ("phase", "document_sweep", "reasoning_effort"), "str"),
    ("GLM_RECOVERY_REASONING_EFFORT", ("phase", "recovery", "reasoning_effort"), "str"),
    ("GLM_FINALIZER_REASONING_EFFORT", ("phase", "finalizer", "reasoning_effort"), "str"),
    ("GLM_THINKING", "thinking", "str"),
    ("GLM_DOCUMENT_SWEEP_THINKING", ("phase", "document_sweep", "thinking"), "str"),
    # shown whenever it is a non-empty object (a named profile ignores extra_body entirely)
    ("GLM_EXTRA_BODY", "extra_body", "extra_body"),
    ("GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS", ("phase", "document_sweep", "max_attempts"), "int"),
    ("GLM_RESEARCH_MAX_ATTEMPTS", ("phase", "research", "max_attempts"), "int"),
    ("GLM_RECOVERY_MAX_ATTEMPTS", ("phase", "recovery", "max_attempts"), "int"),
    ("ADJUDICATION_MAX_U_ITEMS", "adjudication_max_u_items", "int"),
    ("ADJUDICATION_MAX_A_FIELDS", "adjudication_max_a_fields", "int"),
    ("ADJUDICATION_MAX_A_CANDIDATES", "adjudication_max_a_candidates", "int"),
    ("ADJUDICATION_MAX_M_FIELDS", "adjudication_max_m_fields", "int"),
    ("ADJUDICATION_MAX_M_SNIPPETS", "adjudication_max_m_snippets", "int"),
    ("ADJUDICATION_U_MAX_TOKENS", "adjudication_u_max_tokens", "int"),
    ("ADJUDICATION_A_MAX_TOKENS", "adjudication_a_max_tokens", "int"),
    ("ADJUDICATION_M_MAX_TOKENS", "adjudication_m_max_tokens", "int"),
    ("PRIMARY_RESEARCH_MAX_TURNS", "max_steps", "int"),
    ("PRIMARY_RESEARCH_HARD_MAX_TURNS", "primary_research_hard_max_turns", "int"),
    ("PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS", "primary_research_min_base_documents", "int"),
    ("PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE", "primary_research_min_base_scoped_coverage", "float"),
    ("SITE_MAP", "site_map", "bool"),
    ("GROUNDED_CANDIDATES", "grounded_candidates", "bool"),
    ("RECOVERY_MODE", "recovery_mode", "str"),
    ("CLUSTER_MAX_ATTEMPTS", "cluster_max_attempts", "int"),
    ("CLUSTER_BASE_TURNS", "cluster_base_turns", "int"),
    ("CLUSTER_MAX_TURNS", "cluster_max_turns", "int"),
    ("CLUSTER_SEARCH_BUDGET", "cluster_search_budget", "int"),
    ("REACQUIRE_STAGE_SEARCH_CAP", "reacquire_stage_search_cap", "int"),
    ("FIELD_RECOVERY_MAX_TOTAL_STEPS", "field_recovery_max_total_steps", "int"),
)


def _agent_defaults() -> dict:
    from .agent import AgentConfig

    return {f.name: f.default for f in dataclasses.fields(AgentConfig) if f.default is not dataclasses.MISSING}


def code_default(target: Any) -> Any:
    """The code default of an AgentConfig field or of a phase setting ("" = provider default / inherit)."""
    if isinstance(target, tuple):
        from .phase_settings import PHASE_DEFAULTS

        return PHASE_DEFAULTS.get(target[1], {}).get(target[2], "")
    if target == "extra_body":
        return {}
    return _agent_defaults()[target]


def _parse(raw: str, kind: str) -> Any:
    from .agent import _env_bool

    raw = raw.strip()
    if kind == "extra_body":     # any key of it may change model calls
        import json

        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        return parsed if isinstance(parsed, dict) else raw
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
    if isinstance(default, dict):        # GLM_EXTRA_BODY: differs whenever it is a non-empty object
        return value == {} or value == default
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(default, (int, float)):
        return float(value) == float(default)
    return value == default


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, dict):
        return "no extra request fields"
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
        shown = "per-phase defaults (research high, others low)" if target == "reasoning_effort" else _shown(default)
        out.append({"var": var, "value": raw, "code_default": shown, "text": f"{var} = {raw} (code default {shown})"})
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
    AgentConfig setting (extra_body is handled by build_agent_config: it is dropped entirely), then the
    profile's own values; the phase settings of _PINNED_PHASE_KEYS at their code default (PHASE_DEFAULTS; None =
    removed, i.e. inherit the pinned global). They cover every phase variable of ENV_OVERRIDE_VARS. ({}, {}) for
    Custom / unknown."""
    from .phase_settings import PHASE_DEFAULTS

    if profile not in NAMED_PROFILES:
        return {}, {}
    values = {target: code_default(target) for _, target, kind in ENV_OVERRIDE_VARS
              if not isinstance(target, tuple) and kind != "extra_body"}
    values.update(NAMED_PROFILES[profile])
    phases: dict[str, dict] = {}
    for phase, key in _PINNED_PHASE_KEYS:
        phases.setdefault(phase, {})[key] = PHASE_DEFAULTS.get(phase, {}).get(key)
    return values, phases


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
    config = agent_config_from_env(env, **values, phase_settings=phases, run_profile=profile or "")
    if pinned:        # a named profile ignores extra_body (GLM_EXTRA_BODY / the UI's extra JSON) entirely
        config.extra_body = {}
    return config


def isolate_single_run(config, in_series: bool):
    """The AgentConfig a run actually uses: an A/B arm (Baseline, Treatment, Treatment + card) started OUTSIDE a
    series runs with research memory and negative-route blocking off (no fact reuse, no route verdicts of earlier
    runs); a series run (its own private cache and memory per planned run), Production, Custom and the CLI are
    unchanged."""
    if in_series or getattr(config, "run_profile", "") not in ARMS:
        return config
    return dataclasses.replace(config, research_memory_enabled=False, negative_route_blocking=False)


def profile_label(profile: str | None) -> str:
    return PROFILE_LABELS.get(profile or "", profile or "—")

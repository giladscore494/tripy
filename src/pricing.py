"""Cost reporting defaults. Observational only: cost never gates a run.

Defaults are the official Z.ai prices as provided on 2026-10-01. Each value can
be overridden with an environment variable or in the UI, and the prices actually
used are stored with every run so its cost stays reproducible.
"""

from __future__ import annotations

import os

PRICING_SOURCE = "Z.ai official pricing (as provided 2026-10-01)"

MODEL_PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "glm-5.3": {"input": 1.40, "output": 4.40},
    "glm-5.3-flash": {"input": 0.15, "output": 0.50},
    "glm-5.3-flashx": {"input": 0.37, "output": 1.25},
}
WEB_SEARCH_USD_PER_CALL = 0.01


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def default_pricing(model: str) -> dict:
    """Prices for a model id: table default, then env overrides. Unknown model → token prices None."""
    table = MODEL_PRICES_USD_PER_MTOK.get((model or "").strip().lower())
    pricing = {
        "input_per_mtok": table["input"] if table else None,
        "output_per_mtok": table["output"] if table else None,
        "web_search_per_call": WEB_SEARCH_USD_PER_CALL,
        "source": PRICING_SOURCE if table else "no default for this model id",
    }
    overrides = {
        "input_per_mtok": _env_float("GLM_PRICE_INPUT_PER_MTOK"),
        "output_per_mtok": _env_float("GLM_PRICE_OUTPUT_PER_MTOK"),
        "web_search_per_call": _env_float("GLM_PRICE_WEB_SEARCH_PER_CALL"),
    }
    for key, value in overrides.items():
        if value is not None:
            pricing[key] = value
            pricing["source"] = "environment override"
    return pricing


def compute_cost(usage: dict, search_api_calls: int, pricing: dict | None) -> dict:
    """Cost breakdown in USD. Token cost is None when no token prices are known.

    Cached prompt tokens are billed at the full input price here, because no
    cached-input price was supplied.
    """
    pricing = pricing or {}
    p_in, p_out = pricing.get("input_per_mtok"), pricing.get("output_per_mtok")
    tokens = None
    if p_in is not None and p_out is not None:
        tokens = round((usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out) / 1e6, 6)
    per_call = pricing.get("web_search_per_call")
    search = round(search_api_calls * per_call, 6) if per_call is not None else None
    parts = [x for x in (tokens, search) if x is not None]
    return {"tokens_usd": tokens, "web_search_usd": search, "total_usd": round(sum(parts), 6) if parts else None}


UNKNOWN_USAGE_NOTE = ("Recorded API cost from returned usage; provider billing may be higher for timed-out "
                      "requests.")


def run_cost(usage_research: dict | None, usage_finalizer: dict | None, search_api_calls: int,
             pricing: dict | None, pricing_finalizer: dict | None = None,
             unknown_usage_attempts: int = 0) -> tuple[dict, dict]:
    """(cost, details). `cost` keeps the {tokens_usd, web_search_usd, total_usd} shape.

    Only usage returned by successful responses is priced. Attempts whose outcome
    at the provider is unknown (timeouts, dropped connections) cannot be priced, so
    the figure is then a lower bound, flagged in `details`.
    """
    research = compute_cost(usage_research or {}, search_api_calls, pricing)
    final_usage = usage_finalizer or {}
    finalizer = compute_cost(final_usage, 0, pricing_finalizer or pricing)
    has_final = bool(final_usage.get("prompt_tokens") or final_usage.get("completion_tokens"))
    if research["tokens_usd"] is None or (has_final and finalizer["tokens_usd"] is None):
        tokens = None
    else:
        tokens = round(research["tokens_usd"] + (finalizer["tokens_usd"] or 0.0), 6)
    search = research["web_search_usd"]
    parts = [x for x in (tokens, search) if x is not None]
    cost = {"tokens_usd": tokens, "web_search_usd": search, "total_usd": round(sum(parts), 6) if parts else None}
    details = {
        "basis": "returned_usage_only",
        "research_tokens_usd": research["tokens_usd"],
        "finalizer_tokens_usd": finalizer["tokens_usd"] if has_final else 0.0,
        "web_search_usd": search,
        "unknown_usage_attempts": unknown_usage_attempts,
        "complete": unknown_usage_attempts == 0,
        "note": UNKNOWN_USAGE_NOTE if unknown_usage_attempts else "Recorded API cost from returned usage.",
    }
    return cost, details


def phase_run_cost(*, usage_research: dict | None, usage_sweep: dict | None, usage_recovery: dict | None,
                   usage_finalizer: dict | None, search_api_calls: int, pricing: dict | None,
                   pricing_finalizer: dict | None = None, unknown_usage_attempts: int = 0,
                   phase_models: dict[str, str] | None = None) -> tuple[dict, dict]:
    """run_cost with per-phase models (src/phase_settings.py): a document_sweep / field_recovery phase that ran on its
    own model (`phase_models` = {group: model}) is priced with that model's prices; every other phase as before. A
    phase that used no tokens is never priced separately (an unpriced model it never called cannot null the cost)."""
    own = {}
    for group, usage in (("document_sweep", usage_sweep), ("field_recovery", usage_recovery)):
        model = (phase_models or {}).get(group)
        if model and ((usage or {}).get("prompt_tokens") or (usage or {}).get("completion_tokens")):
            own[group] = (usage or {}, model)
    shared = [u for g, u in (("document_sweep", usage_sweep), ("field_recovery", usage_recovery)) if g not in own]
    total = {}
    for usage in [usage_research, *shared]:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
    cost, details = run_cost(total, usage_finalizer, search_api_calls, pricing, pricing_finalizer,
                             unknown_usage_attempts)
    for group, (usage, model) in own.items():
        part = compute_cost(usage, 0, default_pricing(model))["tokens_usd"]
        details[f"{group}_tokens_usd"] = part
        details[f"{group}_model"] = model
        cost["tokens_usd"] = None if part is None or cost["tokens_usd"] is None \
            else round(cost["tokens_usd"] + part, 6)
        parts = [x for x in (cost["tokens_usd"], cost["web_search_usd"]) if x is not None]
        cost["total_usd"] = round(sum(parts), 6) if parts else None
    return cost, details


def phase_models_of(cost_details: dict | None) -> dict[str, str]:
    """The per-phase models a priced run recorded in its cost_details."""
    return {g: m for g in ("document_sweep", "field_recovery")
            if (m := (cost_details or {}).get(f"{g}_model"))}

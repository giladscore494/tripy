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

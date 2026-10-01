"""Benchmark v1: fixed sample, selection, batch execution and observational metrics.

Metrics count what happened (fields filled, sources, tool calls, cache reuse,
time, tokens, cost). None of them is a truth score.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .agent import AgentConfig, effective_glm_config, run_vehicle
from .db import build_level15_payload
from .pricing import compute_cost
from .schemas import LEVEL3_TOPICS, has_value, iter_fields, target_field_names
from .storage.run_log import RunLog, utc_now, write_batch
from .tools import ToolConfig

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IDS_PATH = DATA_DIR / "benchmark_v1_ids.json"
# Vehicle #44 (XPeng G6 2026 MAX, NSGHA): the first real-API handshake vehicle.
HANDSHAKE_RECORD_ID = "101122"


def load_benchmark(path: Path = IDS_PATH) -> dict:
    return json.loads(Path(path).read_text("utf-8"))


def benchmark_vehicles(path: Path = IDS_PATH) -> list[dict]:
    return load_benchmark(path)["vehicles"]


def manufacturers(vehicles: list[dict]) -> list[str]:
    seen: list[str] = []
    for v in vehicles:
        if v["manufacturer"] not in seen:
            seen.append(v["manufacturer"])
    return seen


def select_vehicles(vehicles: list[dict], mode: str, value: str | None = None) -> list[dict]:
    """mode: 'one' (value=upstream_record_id), 'manufacturer' (value=name) or 'all'. Order is preserved."""
    if mode == "all":
        return list(vehicles)
    if mode == "manufacturer":
        return [v for v in vehicles if v["manufacturer"] == value]
    if mode == "one":
        return [v for v in vehicles if v["upstream_record_id"] == str(value)]
    raise ValueError(f"Unknown selection mode {mode!r}")


def vehicle_label(v: dict) -> str:
    return f"#{v['ordinal']} {v['manufacturer']} {v['model']} {v['year']} {v['trim']} ({v['upstream_record_id']})"


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def compute_metrics(result: dict, vehicle: dict | None = None, cache=None, pricing: dict | None = None) -> dict:
    """Observational metrics. Cost uses the pricing stored with the run unless `pricing` is given."""
    output = result.get("output")
    is_electrified = (vehicle or {}).get("propulsion", "") != "conventional"
    targets = target_field_names(include_electric=is_electrified)
    fields = iter_fields(output)
    filled_names = {name for name, entry in fields if has_value(entry)}
    target_filled = [name for name in targets if name in filled_names]
    extra = sorted(filled_names - set(target_field_names(include_electric=True)))

    urls = set()
    for item in result.get("evidence", []):
        if item.get("source_url"):
            urls.add(item["source_url"])
    for doc_id in result.get("documents", []):
        meta = cache.get(doc_id) if cache is not None else None
        if meta:
            urls.add(meta.get("final_url") or meta.get("url"))
    by_tool = Counter(call["name"] for call in result.get("tool_calls", []))
    counters = result.get("counters", {})
    usage = result.get("usage", {})
    conflicts = output.get("conflicts") if isinstance(output, dict) else None
    findings = output.get("additional_findings") if isinstance(output, dict) else None
    level3 = output.get("level3") if isinstance(output, dict) else None
    level3_topics = [k for k in (level3 or {}) if k in LEVEL3_TOPICS] if isinstance(level3, dict) else []
    search_api_calls = counters.get("search_api_calls", 0)
    cost = compute_cost(usage, search_api_calls, pricing if pricing is not None else result.get("pricing"))
    return {
        "record_id": result.get("record_id"),
        "status": result.get("status"),
        "target_fields": len(targets),
        "target_filled": len(target_filled),
        "coverage_pct": round(100 * len(target_filled) / len(targets), 1) if targets else 0.0,
        "fields_returned": len(fields),
        "fields_with_value": len(filled_names),
        "extra_fields": len(extra),
        "extra_field_names": extra,
        "evidence_items": len(result.get("evidence", [])),
        "unique_sources": len(urls),
        "unique_domains": len({_domain(u) for u in urls if u}),
        "documents_opened": len(result.get("documents", [])),
        "tool_calls": sum(by_tool.values()),
        "tool_calls_by_name": dict(by_tool),
        "tool_errors": sum(1 for call in result.get("tool_calls", []) if call.get("error")),
        "document_cache_hits": counters.get("cache_hits", 0),
        "document_cache_misses": counters.get("cache_misses", 0),
        "search_cache_hits": counters.get("search_cache_hits", 0),
        "search_api_calls": search_api_calls,
        "api_errors": counters.get("api_errors", 0),
        "conflicts_reported": len(conflicts) if isinstance(conflicts, list) else 0,
        "additional_findings": len(findings) if isinstance(findings, list) else 0,
        "level3_topics": len(level3_topics),
        "duration_s": result.get("duration_s", 0),
        "model_calls": usage.get("model_calls", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "model_latency_s": round(usage.get("model_latency_ms", 0) / 1000, 2),
        "cost_tokens_usd": cost["tokens_usd"],
        "cost_search_usd": cost["web_search_usd"],
        "cost_usd": cost["total_usd"],
    }


SUM_KEYS = ("target_filled", "fields_with_value", "extra_fields", "evidence_items", "documents_opened", "tool_calls",
            "tool_errors", "document_cache_hits", "document_cache_misses", "search_cache_hits",
            "search_api_calls", "api_errors", "conflicts_reported", "additional_findings", "duration_s",
            "model_latency_s", "model_calls", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")


def aggregate(metrics: list[dict]) -> dict:
    if not metrics:
        return {"vehicles": 0}
    n = len(metrics)
    out: dict = {"vehicles": n, "statuses": dict(Counter(m["status"] for m in metrics))}
    for key in SUM_KEYS:
        total = sum(m.get(key) or 0 for m in metrics)
        out[f"{key}_total"] = round(total, 2)
        out[f"{key}_mean"] = round(total / n, 2)
    out["coverage_pct_mean"] = round(sum(m["coverage_pct"] for m in metrics) / n, 1)
    hits, misses = out["document_cache_hits_total"], out["document_cache_misses_total"]
    out["document_cache_hit_rate_pct"] = round(100 * hits / (hits + misses), 1) if hits + misses else 0.0
    costs = [m["cost_usd"] for m in metrics if m.get("cost_usd") is not None]
    out["cost_usd_total"] = round(sum(costs), 4) if costs else None
    return out


def run_batch(vehicles: list[dict], rows_by_id: dict[str, dict], run_one: Callable[[dict, dict], dict],
              on_start: Callable[[dict], None] | None = None,
              on_done: Callable[[dict, dict], None] | None = None) -> list[dict]:
    """Run vehicles sequentially (one shared cache, so reuse is measured honestly)."""
    results = []
    for vehicle in vehicles:
        row = rows_by_id.get(vehicle["upstream_record_id"])
        if row is None:
            continue
        if on_start:
            on_start(vehicle)
        result = run_one(vehicle, row)
        results.append(result)
        if on_done:
            on_done(vehicle, result)
    return results


def start_batch(runs_dir: Path | str, batch_id: str, *, client, agent_cfg: AgentConfig, tool_cfg: ToolConfig,
                pricing: dict, vehicles: list[dict], level15_source: str, level15_note: str, selection: str,
                prompt_version: str) -> dict:
    """Write batch.json with the complete effective configuration (no secrets)."""
    info = {
        "batch_id": batch_id,
        "created_at": utc_now(),
        "model": client.model,
        "glm_config": effective_glm_config(client, agent_cfg, tool_cfg),
        "prompt_version": prompt_version,
        "search_backend": tool_cfg.search_backend,
        "pricing": pricing,
        "level15_source": level15_source,
        "level15_note": level15_note,
        "selection": selection,
        "record_ids": [v["upstream_record_id"] for v in vehicles],
        "agent_config": {k: v for k, v in agent_cfg.__dict__.items()},
        "tool_config": dict(tool_cfg.__dict__),
    }
    write_batch(runs_dir, batch_id, info)
    return info


def research_one(vehicle: dict, row: dict, *, client, cache, runs_dir: Path | str, batch_id: str,
                 agent_cfg: AgentConfig, tool_cfg: ToolConfig, pricing: dict, level15_source: str,
                 listener: Callable[[str, dict], None] | None = None, session=None) -> dict:
    """Research exactly one vehicle, write result.json, and return the result. Never moves on by itself."""
    log = RunLog(runs_dir, batch_id, vehicle["upstream_record_id"], listener=listener)
    result = run_vehicle(row, build_level15_payload(row), client=client, cache=cache, run_log=log,
                         config=agent_cfg, tool_config=tool_cfg, vehicle_meta=vehicle, batch_id=batch_id,
                         ordinal=vehicle.get("ordinal"), session=session, pricing=pricing)
    result["level15_source"] = level15_source
    result["metrics"] = compute_metrics(result, vehicle, cache)
    log.write_result(result)
    return result

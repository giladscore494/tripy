"""Pure functions over a run's events.jsonl.

Live runs and runs reconstructed after a crash, interrupt or finalization failure
derive the same numbers from the same events, so a run that never wrote
result.json can still be displayed and measured. Works with event logs written
before these helpers existed (older api_error events carry no `request_kind`,
`timeout` or `usage_unknown` keys; those are inferred from the endpoint and the
error text).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

FETCH_TOOLS = ("fetch_url", "fetch_pdf", "render_page")
SEARCH_TOOLS = ("search_web", "search_official_domains")
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "model_calls",
              "model_latency_ms")
RESEARCH_PHASE = "research"
RECOVERY_PHASE = "field_recovery"
PHASE_GROUPS = (RESEARCH_PHASE, RECOVERY_PHASE, "finalization")


def empty_usage() -> dict:
    return {key: 0 for key in USAGE_KEYS}


def add_usage(acc: dict, usage: dict | None, latency_ms: int | None = None) -> dict:
    usage = usage or {}
    acc["model_calls"] += 1
    acc["model_latency_ms"] += int(latency_ms or 0)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        acc[key] += int(usage.get(key) or 0)
    details = usage.get("prompt_tokens_details") or {}
    acc["cached_tokens"] += int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return acc


def sum_usage(*usages: dict | None) -> dict:
    out = empty_usage()
    for usage in usages:
        for key in USAGE_KEYS:
            out[key] += int((usage or {}).get(key) or 0)
    return out


def phase_group(phase: str | None) -> str:
    """'research', 'field_recovery' or 'finalization' (finalization, its repair turn, recovery finalization)."""
    if not phase or phase == RESEARCH_PHASE:
        return RESEARCH_PHASE
    return RECOVERY_PHASE if phase == RECOVERY_PHASE else "finalization"


def usage_by_phase(events: Iterable[dict]) -> dict[str, dict]:
    """Token usage confirmed by successful model responses, split research / field_recovery / finalization."""
    out = {group: empty_usage() for group in PHASE_GROUPS}
    for event in events:
        if event.get("kind") == "model_response":
            add_usage(out[phase_group(event.get("phase"))], event.get("usage"), event.get("latency_ms"))
    return out


# --- API attempts --------------------------------------------------------------------

def request_kind(event: dict, chat_path: str = "chat/completions") -> str:
    kind = event.get("request_kind")
    if kind in ("chat", "search"):
        return kind
    endpoint = str(event.get("endpoint") or "")
    return "chat" if endpoint == chat_path or "chat" in endpoint else "search"


def is_timeout(event: dict) -> bool:
    if "timeout" in event:
        return bool(event["timeout"])
    text = f"{event.get('error_type') or ''} {event.get('error') or ''}".lower()
    return "timeout" in text or "timed out" in text


def usage_unknown(event: dict) -> bool:
    """True when the provider may have processed (and billed) a request whose response we never got."""
    if "usage_unknown" in event:
        return bool(event["usage_unknown"])
    if event.get("error") == "invalid_json":
        return True
    if event.get("status") is not None:
        return False
    return "connecttimeout" not in str(event.get("error") or "").lower()


def api_stats(events: Iterable[dict], chat_path: str = "chat/completions") -> dict:
    """Every HTTP attempt to GLM, counted separately. Nothing here is billing truth."""
    stats = {"api_attempts": 0, "chat_attempts": 0, "search_attempts": 0, "api_successes": 0,
             "api_errors": 0, "chat_errors": 0, "search_errors": 0, "timeout_count": 0,
             "chat_timeouts": 0, "search_timeouts": 0, "unknown_usage_attempts": 0,
             "unknown_usage_chat_attempts": 0, "unknown_usage_search_attempts": 0}
    for event in events:
        kind = event.get("kind")
        if kind not in ("api_call", "api_error"):
            continue
        rk = request_kind(event, chat_path)
        stats["api_attempts"] += 1
        stats[f"{rk}_attempts"] += 1
        if kind == "api_call":
            stats["api_successes"] += 1
            continue
        stats["api_errors"] += 1
        stats[f"{rk}_errors"] += 1
        if is_timeout(event):
            stats["timeout_count"] += 1
            stats[f"{rk}_timeouts"] += 1
        if usage_unknown(event):
            stats["unknown_usage_attempts"] += 1
            stats[f"unknown_usage_{rk}_attempts"] += 1
    return stats


def billable_search_calls(events: Iterable[dict], chat_path: str = "chat/completions") -> int:
    return sum(1 for e in events if e.get("kind") == "api_call" and request_kind(e, chat_path) == "search")


# --- Research artifacts -----------------------------------------------------------------

def parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def tool_pairs(events: Iterable[dict]) -> list[dict]:
    """tool_call events joined with their tool_result (by call id, else by order)."""
    pairs: list[dict] = []
    open_by_id: dict[str, dict] = {}
    pending: list[dict] = []
    for event in events:
        kind = event.get("kind")
        if kind == "tool_call":
            pair = {"step": event.get("step"), "call_id": event.get("call_id"), "name": event.get("name"),
                    "arguments": event.get("arguments"), "result": None, "call_ts": event.get("ts")}
            pairs.append(pair)
            if pair["call_id"]:
                open_by_id[pair["call_id"]] = pair
            pending.append(pair)
        elif kind == "tool_result":
            pair = open_by_id.pop(event.get("call_id"), None) if event.get("call_id") else None
            if pair is None:
                pair = next((p for p in pending if p["result"] is None and p["name"] == event.get("name")), None)
            if pair is None:
                continue
            pair["result"] = event.get("result")
            pair["result_ts"] = event.get("ts")
            if pair in pending:
                pending.remove(pair)
    return pairs


def tool_call_rows(events: Iterable[dict]) -> list[dict]:
    """The same row shape the agent stores in result['tool_calls']."""
    rows = []
    for pair in tool_pairs(events):
        result = pair["result"] if isinstance(pair["result"], dict) else {}
        rows.append({
            "step": pair["step"], "name": pair["name"], "arguments": pair["arguments"],
            "duration_ms": result.get("_elapsed_ms"),
            "cache_hit": result.get("cache_hit"),
            "error": result.get("error") if pair["result"] is not None else "no_result_logged",
            "document_id": result.get("document_id"),
        })
    return rows


def evidence_items(events: Iterable[dict]) -> list[dict]:
    return [e["evidence"] for e in events if e.get("kind") == "evidence" and isinstance(e.get("evidence"), dict)]


def document_ids(events: Iterable[dict]) -> list[str]:
    """Every document a run touched, in first-touch order (fetches, cache reads and extraction tools)."""
    seen: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.startswith("d_") and value not in seen:
            seen.append(value)

    for event in events:
        kind = event.get("kind")
        if kind == "tool_result" and isinstance(event.get("result"), dict):
            result = event["result"]
            if not result.get("error") or result.get("document_id"):
                add(result.get("document_id"))
        elif kind == "document" and isinstance(event.get("document"), dict):
            add(event["document"].get("document_id"))
        elif kind == "evidence" and isinstance(event.get("evidence"), dict):
            add(event["evidence"].get("document_id"))
    return seen


def document_event_meta(events: Iterable[dict]) -> dict[str, dict]:
    """Last-known document metadata from `document` events (fallback when the cache is gone)."""
    out: dict[str, dict] = {}
    for event in events:
        if event.get("kind") == "document" and isinstance(event.get("document"), dict):
            doc = event["document"]
            if doc.get("document_id"):
                out[doc["document_id"]] = {**out.get(doc["document_id"], {}), **doc}
    return out


def tool_counters(events: Iterable[dict], chat_path: str = "chat/completions") -> Counter:
    """Counters equivalent to ToolContext.counters, recovered from events."""
    events = list(events)
    counters: Counter = Counter()
    for pair in tool_pairs(events):
        counters[f"tool:{pair['name']}"] += 1
        result = pair["result"] if isinstance(pair["result"], dict) else {}
        if pair["name"] in FETCH_TOOLS and "cache_hit" in result:
            counters["cache_hits" if result["cache_hit"] else "cache_misses"] += 1
        elif pair["name"] == "get_cached_document" and result.get("found"):
            counters["cache_hits"] += 1
        elif pair["name"] == "search_web" and "cache_hit" in result:
            counters["search_cache_hits" if result["cache_hit"] else "search_cache_misses"] += 1
        elif pair["name"] == "search_official_domains":
            for info in (result.get("per_domain") or {}).values():
                if isinstance(info, dict) and "cache_hit" in info:
                    counters["search_cache_hits" if info["cache_hit"] else "search_cache_misses"] += 1
    counters["search_api_calls"] = billable_search_calls(events, chat_path)
    counters["api_errors"] = sum(1 for e in events if e.get("kind") == "api_error")
    return counters


def model_responses(events: Iterable[dict]) -> list[dict]:
    out = []
    for event in events:
        if event.get("kind") != "model_response":
            continue
        out.append({"seq": event.get("seq"), "ts": event.get("ts"), "phase": event.get("phase") or RESEARCH_PHASE,
                    "model": event.get("model"), "finish_reason": event.get("finish_reason"),
                    "usage": event.get("usage"), "latency_ms": event.get("latency_ms"),
                    "content": event.get("content"), "reasoning_content": event.get("reasoning_content"),
                    "tool_calls": len(event.get("tool_calls") or [])})
    return out


def last_model_content(events: Iterable[dict]) -> str | None:
    last = None
    for event in events:
        if event.get("kind") == "model_response" and (event.get("content") or "").strip():
            last = event["content"]
    return last


def research_steps(events: Iterable[dict]) -> int:
    """Research model turns that completed (a response was received)."""
    return sum(1 for e in events if e.get("kind") == "model_response" and phase_group(e.get("phase")) == RESEARCH_PHASE)


def last_completed_tool_step(events: Iterable[dict]) -> int | None:
    steps = [p["step"] for p in tool_pairs(events) if p["result"] is not None and isinstance(p["step"], int)]
    return max(steps) if steps else None


def field_recovery_summary(events: list[dict]) -> dict | None:
    """Field detection / retry outcome recovered from events (None when the stage never ran)."""
    evaluations = [e for e in events if e.get("kind") == "field_evaluation"]
    attempts = [{k: v for k, v in e.items() if k not in ("seq", "ts", "kind")}
                for e in events if e.get("kind") == "field_recovery_finished"]
    if not evaluations and not attempts:
        return None
    primary = next((e for e in evaluations if e.get("stage") == "primary"), None)
    final = next((e for e in reversed(evaluations) if e.get("stage") == "after_recovery"), None)
    queue = last_event(events, "field_retry_queue") or {}
    states = {f["field"]: f for f in (final or primary or {}).get("fields") or []}
    for attempt in attempts:  # a run cut off mid-retry has no after_recovery evaluation yet
        if attempt.get("field") in states and not final:
            states[attempt["field"]] = {**states[attempt["field"]], "state": attempt.get("state_after"),
                                        "retry_eligible": attempt.get("state_after") in (
                                            "unresolved", "missing", "conflicting", "foreign_market_only",
                                            "variant_not_exact", "weak_provenance")}
    retried = sorted({a.get("field") for a in attempts if a.get("field")})
    return {
        "enabled": queue.get("enabled"),
        "requested": len((primary or {}).get("fields") or []),
        "primary_states": (primary or {}).get("summary"),
        "final_states": (final or {}).get("summary"),
        "queue": queue.get("fields") or [],
        "fields_retried": retried,
        "fields_recovered": [f for f in retried if not (states.get(f) or {}).get("retry_eligible")],
        "fields_still_failed": [f for f in retried if (states.get(f) or {}).get("retry_eligible")],
        "attempts": attempts,
        "attempt_count": len(attempts),
        "turns": sum(int(a.get("turns") or 0) for a in attempts),
        "evaluation_primary": (primary or {}).get("fields"),
        "evaluation_final": list(states.values()) if states else None,
        "reconstructed": True,
    }


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def first_event(events: Iterable[dict], kind: str) -> dict | None:
    return next((e for e in events if e.get("kind") == kind), None)


def last_event(events: list[dict], kind: str) -> dict | None:
    return next((e for e in reversed(events) if e.get("kind") == kind), None)


def chat_failures_after_research(events: list[dict], chat_path: str = "chat/completions") -> list[dict]:
    """Chat attempts that failed after the last tool result with no model response after them.

    In the GLM-5.3 baseline this is the finalization call that timed out repeatedly.
    """
    last_tool = max((i for i, e in enumerate(events) if e.get("kind") == "tool_result"), default=-1)
    tail = events[last_tool + 1:]
    if any(e.get("kind") == "model_response" for e in tail):
        return []
    return [e for e in tail if e.get("kind") == "api_error" and request_kind(e, chat_path) == "chat"]

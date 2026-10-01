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
                    "arguments": event.get("arguments"), "result": None, "call_ts": event.get("ts"),
                    "phase": event.get("phase"), "field": event.get("field"), "attempt": event.get("attempt")}
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
    """Canonical evidence items. Supplementary quote/note values carried by `evidence_reused` events are
    folded back into their original item (exactly as the live EvidenceStore does); no item is added."""
    items: list[dict] = []
    by_id: dict[str, dict] = {}
    for event in events:
        if event.get("kind") == "evidence" and isinstance(event.get("evidence"), dict):
            item = dict(event["evidence"])
            items.append(item)
            by_id.setdefault(str(item.get("evidence_id")), item)
        elif event.get("kind") == "evidence_reused" and event.get("supplementary"):
            item = by_id.get(str(event.get("evidence_id")))
            if item is not None:
                item["supplementary"] = list(item.get("supplementary") or []) + [dict(event["supplementary"])]
    return items


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
    for event in events:  # exact repeats answered before dispatch: never executions, never billed
        if event.get("kind") == "tool_reused":
            counters["duplicate_calls_suppressed"] += 1
            counters[f"reused:{event.get('name')}"] += 1
        elif event.get("kind") == "evidence_reused":
            counters["duplicate_evidence_suppressed"] += 1
    return counters


def reuse_counts(events: Iterable[dict]) -> dict:
    """Suppressed exact repeats by kind, recovered from `tool_reused` events."""
    out = {"duplicate_calls_suppressed": 0, "duplicate_searches_suppressed": 0,
           "duplicate_fetches_suppressed": 0, "duplicate_inspections_suppressed": 0}
    for event in events:
        if event.get("kind") != "tool_reused":
            continue
        name = event.get("name")
        out["duplicate_calls_suppressed"] += 1
        kind = "searches" if name in SEARCH_TOOLS else "fetches" if name in FETCH_TOOLS else "inspections"
        out[f"duplicate_{kind}_suppressed"] += 1
    return out


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


def apply_current_states(summary: dict | None, field_states: dict | None) -> dict | None:
    """Attach the CURRENT state of every field (recomputed from all events) to a recovery summary.

    Attempt history (state_before/state_after, replies) is left as it was; `current_states` is the
    authoritative view. For summaries rebuilt from events (interrupted runs), the recovered /
    still-failed lists and final counts are derived from the current states, so evidence stored
    mid-attempt or as a by-product is never reverted to a stale snapshot.
    """
    if not summary or not field_states:
        return summary
    current = {name: (state or {}).get("state") for name, state in field_states.items()}
    summary["current_states"] = current
    if summary.get("reconstructed"):
        done = ("ok", "not_applicable")
        retried = summary.get("fields_retried") or []
        summary["fields_recovered"] = [f for f in retried if current.get(f) in done]
        summary["fields_still_failed"] = [f for f in retried if current.get(f) not in done]
        summary["fields_resolved_after_queue"] = [f for f in summary.get("queue") or [] if current.get(f) in done]
        counts: dict[str, int] = {}
        for state in current.values():
            counts[state] = counts.get(state, 0) + 1
        summary["final_states"] = counts
    return summary


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
    budget = last_event(events, "field_recovery_budget_exhausted")
    # HISTORY only. Current states are never patched from attempt snapshots here: callers attach them
    # with apply_current_states(), computed by the shared evaluator over ALL events.
    states = {f["field"]: f for f in (final or {}).get("fields") or []}
    retried = sorted({a.get("field") for a in attempts if a.get("field")})
    early = [e for e in events if e.get("kind") == "field_recovery_early_resolved"]
    return {
        "enabled": queue.get("enabled"),
        "requested": len((primary or {}).get("fields") or []),
        "primary_states": (primary or {}).get("summary"),
        "final_states": (final or {}).get("summary"),
        "queue": queue.get("fields") or [],
        "fields_retried": retried,
        "fields_recovered": [f for f in retried if states and not (states.get(f) or {}).get("retry_eligible")],
        "fields_still_failed": [f for f in retried if states and (states.get(f) or {}).get("retry_eligible")],
        "fields_resolved_indirectly": {e.get("field"): {"resolved_during_field": e.get("resolved_during_field"),
                                                        "attempt": e.get("attempt"), "state": e.get("state")}
                                       for e in events if e.get("kind") == "field_recovery_queue_resolved_indirectly"},
        "attempts": attempts,
        "attempt_count": len(attempts),
        "prior_excerpt_items": sum(int(a.get("prior_excerpt_items") or 0) for a in attempts),
        "prior_excerpt_chars": sum(int(a.get("prior_excerpt_chars") or 0) for a in attempts),
        "attempts_with_prior_excerpts": sum(1 for a in attempts if a.get("prior_excerpt_items")),
        "turns": sum(int(a.get("turns") or 0) for a in attempts),
        "field_recovery_turns_used": sum(int(a.get("turns") or 0) for a in attempts),
        "field_recovery_turn_budget": (budget or {}).get("turn_budget"),
        "fields_not_attempted_due_to_budget": (budget or {}).get("fields_not_attempted") or [],
        "field_cut_short_by_budget": (budget or {}).get("field_cut_short"),
        "stopped": "max_total_steps" if budget else None,
        "evaluation_primary": (primary or {}).get("fields"),
        "evaluation_final": list(states.values()) if states else None,
        "early_resolution_count": len(early),
        "turn_budget_skipped_by_early_resolution": sum(int(e.get("turn_budget_skipped") or 0) for e in early),
        "turns_saved_by_early_resolution": len(early),
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

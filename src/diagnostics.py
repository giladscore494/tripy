"""Diagnostic telemetry for PRIMARY RESEARCH (source acquisition) and the DOCUMENT SWEEP. Observational only.

Built purely from a vehicle run's durable events.jsonl (plus its result.json when present); it makes no model call,
search or fetch, and nothing in the research engine reads it. It exists to tell, after a benchmark, whether a
limitation comes from search / source acquisition, source selection, deterministic extraction, document-sweep
reasoning, latency / timeouts or orchestration.

    acquisition turn   one record per primary-research model turn: models (configured vs provider-reported),
                       latency, tokens, attempts / timeouts; searches (queries, results, domains, source class,
                       official, target market, repeats); fetches (attempted, succeeded, failures by category);
                       acquisition state before / after the turn and the deltas; the acquisition artifacts and the
                       stop bookkeeping exactly as the existing policy recorded them (artifacts, no-artifact streak,
                       minimum-base gate, deferred stops, extension beyond the normal budget, stop reason).
    end to end         recovery (model calls, billable searches, fetches, attempts, fields resolved), the final field
                       states (current_evaluation over the final events), totals per phase (model calls, tokens),
                       cost (the run's own result.json cost when present), wall time, the run status and whether it
                       was interrupted (interrupted / incomplete runs are excluded from end-to-end means), and the
                       run's configuration (acquisition mode, models, sweep settings) for `by_config` grouping.
    sweep call         one record per document-sweep packet (a chunk; it may use up to the configured number of model
                       turns): models, latency, tokens, timeouts, retries; the packet (fields, documents, candidates,
                       size); every field's state before / after, evidence admitted / rejected (with reasons),
                       malformed tool output, and the deterministic-harvest classification below.

DETERMINISTIC_HARVEST_MISS (per sweep evidence item that resolved its field) is assigned only when ALL hold:
    * the field was retry-eligible before the call and is settled after it (resolved by this sweep call);
    * the evidence was admitted (an `evidence` event; rejected requests never qualify) and is not bound to another
      variant / unbound;
    * its document was already in the run (a `document` / fetch result event) before the sweep started — the sweep
      cannot fetch, so the fact came from an existing cached document;
    * the deterministic harvest produced NO candidate for that field from that document.
  If the harvest produced a candidate for that field from that document with another value, the item is
  DETERMINISTIC_VALUE_MISMATCH (a parser precision / normalization question, not a recall miss); with the same value
  it is PROMOTED_CANDIDATE. Anything that cannot be established (unknown document, document first seen during the
  sweep) is UNCLASSIFIED. The existing `deterministic_misses_found` counter (src/document_sweep.py) counts the first
  two together; both numbers are reported.

Files (inside the vehicle's run folder, i.e. under TRIPY_DATA_DIR/runs/<run_id>/<record_id>/):
    diagnostics.json   schema, identity, acquisition {turns, summary}, document_sweep {calls, summary}, summary_text
    diagnostics.jsonl  one line per acquisition turn and per sweep call (stream-friendly for aggregation)
A benchmark aggregate over many vehicles: `python -m src.diagnostics --runs-dir <runs> [run_id ...]`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .document_binding import is_trim_gap

SCHEMA = "tripy-diagnostics/1"
DIAGNOSTICS_FILE = "diagnostics.json"
DIAGNOSTICS_STREAM = "diagnostics.jsonl"
PARSER_GAPS_FILE = "parser_gaps.jsonl"

DETERMINISTIC_HARVEST_MISS = "DETERMINISTIC_HARVEST_MISS"
DETERMINISTIC_VALUE_MISMATCH = "DETERMINISTIC_VALUE_MISMATCH"
PROMOTED_CANDIDATE = "PROMOTED_CANDIDATE"
UNCLASSIFIED = "UNCLASSIFIED"

SEARCH_TOOLS = ("search_web", "search_official_domains")
FETCH_TOOLS = ("fetch_url", "fetch_pdf", "render_page")
EVIDENCE_TOOLS = ("store_evidence", "report_field_status")


# --- small helpers ------------------------------------------------------------------------------------------------

def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _domain(url: str | None) -> str:
    host = urlparse(str(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _norm_url(url: Any) -> str:
    return str(url or "").split("#")[0].rstrip("/")


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _seconds(a: str | None, b: str | None) -> float | None:
    start, end = _ts(a), _ts(b)
    return round((end - start).total_seconds(), 2) if start and end else None


def _norm_field(name: Any) -> str:
    from .fields import normalize_field_name
    return normalize_field_name(name)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _fetch_failure_category(result: Any) -> str | None:
    """None for a successful fetch with content; otherwise a coarse failure category."""
    if not isinstance(result, dict):
        return "invalid_result"
    if result.get("error"):
        error = str(result["error"]).lower()
        if "timeout" in error:
            return "timeout"
        if "connection" in error or "ssl" in error:
            return "connection"
        if "invalid" in error or "url" in error:
            return "invalid_request"
        return f"error:{result['error']}"
    status = result.get("status")
    if isinstance(status, int):
        if status in (401, 403):
            return "http_forbidden"
        if status == 404:
            return "http_not_found"
        if status == 429:
            return "http_rate_limited"
        if status >= 500:
            return "http_server_error"
        if status >= 400:
            return f"http_{status}"
    if not (result.get("text_chars") or 0):
        return "empty_content"
    return None


def _source_profile(url: str, manufacturer: str | None, target_market: str | None) -> dict:
    """Server-side source classification of a URL (the same classifiers the engine uses); never an admission."""
    from .field_recovery import is_target_market
    from .source_authority import OFFICIAL_CLASSES, classify_source, url_market

    try:
        authority = classify_source(url, manufacturer).get("source_authority") or "unknown"
        market, _ = url_market(url)
    except Exception:  # noqa: BLE001 - classification problems never break diagnostics
        authority, market = "unknown", None
    return {"source_class": authority, "official": authority in OFFICIAL_CLASSES, "market": market,
            "target_market": bool(market) and bool(target_market) and is_target_market(market, target_market)}


def _model_calls(events: list[dict], *, start: int, end: int, phase_prefix: str) -> list[dict]:
    """Model calls (one per model_response) of a phase inside a seq window, with attempts / timeouts / retries."""
    calls, attempts, errors = [], [], []
    for e in events:
        seq = e.get("seq") or 0
        if seq <= start or seq > end or not str(e.get("phase") or "").startswith(phase_prefix):
            continue
        kind = e.get("kind")
        if kind == "model_request_started":
            attempts.append(e)
        elif kind == "api_error" and e.get("request_kind") == "chat":
            errors.append(e)
        elif kind == "model_response":
            usage = e.get("usage") or {}
            meta = e.get("response_meta") or {}
            calls.append({"configured_model": e.get("model"),
                          # provider-reported model id of the response (null when the provider did not return one)
                          "resolved_model": meta.get("model"),
                          "latency_ms": e.get("latency_ms"),
                          "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                          "finish_reason": e.get("finish_reason"),
                          "tool_calls": len(e.get("tool_calls") or []),
                          "attempts": len(attempts) or 1, "retries": max(0, (len(attempts) or 1) - 1),
                          "timeouts": sum(1 for x in errors if x.get("timeout")),
                          "api_errors": [{"status": x.get("status"), "timeout": bool(x.get("timeout")),
                                          "attempt": x.get("attempt")} for x in errors]})
            attempts, errors = [], []
    if attempts or errors:      # requests that never produced a response (the call failed)
        calls.append({"configured_model": (attempts or errors)[-1].get("model"), "resolved_model": None,
                      "latency_ms": None, "input_tokens": None, "output_tokens": None, "finish_reason": None,
                      "tool_calls": 0, "attempts": len(attempts), "retries": max(0, len(attempts) - 1),
                      "timeouts": sum(1 for x in errors if x.get("timeout")), "failed": True,
                      "api_errors": [{"status": x.get("status"), "timeout": bool(x.get("timeout")),
                                      "attempt": x.get("attempt")} for x in errors]})
    return calls


# --- source acquisition -----------------------------------------------------------------------------------------------

def acquisition_turns(events: list[dict]) -> list[dict]:
    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    agent = started.get("agent_config") or {}
    max_turns = int(agent.get("max_steps") or started.get("max_steps") or 0) or None
    hard_max = agent.get("primary_research_hard_max_turns")
    target = started.get("target_market") or agent.get("target_market")
    manufacturer = (started.get("vehicle_label") or {}).get("manufacturer")
    stopped = next((e for e in events if e.get("kind") == "research_stopped"), None)
    end_seq = (stopped or {}).get("seq") or max([e.get("seq") or 0 for e in events] or [0])
    research = [e for e in events if (e.get("seq") or 0) <= end_seq]
    responses = [e for e in research if e.get("kind") == "model_response" and (e.get("phase") or "research") == "research"]
    turn_rows = {e.get("turn"): e for e in research if e.get("kind") == "primary_research_turn"}
    deferred = {e.get("turn"): e for e in research if e.get("kind") == "primary_research_stop_deferred"}
    unmeasured = {e.get("turn"): e for e in research if e.get("kind") == "primary_research_turn_unmeasured"}
    seen_queries: set[str] = set()
    seen_urls: set[str] = set()
    fetched_urls_by_turn: dict[int, set[str]] = {}
    for e in research:
        if e.get("kind") == "tool_result" and e.get("name") in FETCH_TOOLS and (e.get("phase") or "research") == "research":
            result = e.get("result") or {}
            for url in (result.get("url"), result.get("final_url")) if isinstance(result, dict) else ():
                if url:
                    fetched_urls_by_turn.setdefault(int(e.get("step") or 0), set()).add(_norm_url(url))
    all_fetched = set().union(*fetched_urls_by_turn.values()) if fetched_urls_by_turn else set()
    records = []
    previous_after: dict | None = None
    prev_resp_seq = max([e.get("seq") or 0 for e in research if e.get("kind") == "run_started"] or [0])
    for index, response in enumerate(responses, start=1):
        turn = index
        resp_seq = response.get("seq") or 0
        next_resp = responses[index].get("seq") if index < len(responses) else end_seq
        # the model call of turn N: (previous response, this response]; its tool calls / results / search requests
        # and the acquisition bookkeeping that follows: (this response, next response]
        window = [e for e in research if resp_seq < (e.get("seq") or 0) <= (next_resp or end_seq)]
        model = _model_calls(events, start=prev_resp_seq, end=resp_seq, phase_prefix="research")
        calls = [e for e in window if e.get("kind") == "tool_call" and e.get("step") == turn]
        results = {e.get("call_id"): e for e in window if e.get("kind") == "tool_result" and e.get("step") == turn}
        reused = [e for e in window if e.get("kind") == "tool_reused" and e.get("step") == turn]
        blocked = [e for e in window if e.get("kind") == "tool_blocked" and e.get("step") == turn]
        searches, fetches = [], []
        for call in calls:
            name, args = call.get("name"), _args(call.get("arguments"))
            result = (results.get(call.get("call_id")) or {}).get("result")
            if name in SEARCH_TOOLS:
                query = str(args.get("query") or "").strip()
                items = result.get("results") if isinstance(result, dict) else None
                items = items if isinstance(items, list) else []
                found = []
                for item in items:
                    if not isinstance(item, dict) or not item.get("url"):
                        continue
                    url = _norm_url(item["url"])
                    profile = _source_profile(url, manufacturer, target)
                    found.append({"url": url, "domain": _domain(url), **profile, "repeated": url in seen_urls,
                                  "selected": url in all_fetched})
                searches.append({"tool": name, "query": query, "domains": args.get("domains") or args.get("domain"),
                                 "results_returned": len(items), "results": found,
                                 "cache_hit": bool(isinstance(result, dict) and result.get("cache_hit")),
                                 "repeated_query": query.lower() in seen_queries,
                                 "error": result.get("error") if isinstance(result, dict) else None})
                seen_queries.add(query.lower())
                seen_urls.update(f["url"] for f in found)
            elif name in FETCH_TOOLS:
                url = _norm_url(args.get("url"))
                category = _fetch_failure_category(result)
                fetches.append({"tool": name, "url": url, "domain": _domain(url),
                                **_source_profile(url, manufacturer, target),
                                "status": result.get("status") if isinstance(result, dict) else None,
                                "succeeded": category is None, "failure_category": category,
                                "cache_hit": bool(isinstance(result, dict) and result.get("cache_hit")),
                                "document_id": result.get("document_id") if isinstance(result, dict) else None})
        billable = sum(1 for e in window if e.get("kind") == "api_call" and e.get("request_kind") == "search")
        row = turn_rows.get(turn)
        before = (row or {}).get("state_before") or previous_after
        after = (row or {}).get("state_after")
        delta = None
        if before and after:
            def d(key):
                a, b = after.get(key), before.get(key)
                return round(a - b, 1) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
            delta = {"new_useful_documents": d("useful_documents"), "new_official_documents": d("official_documents"),
                     "new_official_urls_discovered": d("official_urls_discovered"),
                     "new_target_market_documents": d("target_market_documents"), "new_candidates": d("candidates"),
                     "new_candidate_fields": d("candidate_fields"), "scoped_coverage_gain": d("scoped_coverage_pct"),
                     "new_admitted_evidence": d("admitted_evidence")}
        wanted = deferred.get(turn)
        final_turn = stopped is not None and index == len(responses)
        record = {
            "type": "acquisition_turn", "turn_number": turn, "max_turns": max_turns, "hard_max_turns": hard_max,
            "extended_beyond_normal_budget": bool(max_turns and turn > max_turns),
            "model": model[-1] if model else None, "model_calls": model,
            "search": {"calls": len(searches), "billable_search_requests": billable,
                       "queries": [s["query"] for s in searches], "results_returned": sum(s["results_returned"]
                                                                                           for s in searches),
                       "repeated_queries": sum(1 for s in searches if s["repeated_query"]),
                       "cache_hits": sum(1 for s in searches if s["cache_hit"]), "details": searches},
            "fetch": {"attempted": len(fetches), "succeeded": sum(1 for f in fetches if f["succeeded"]),
                      "failed": sum(1 for f in fetches if not f["succeeded"]),
                      "failure_categories": dict(Counter(f["failure_category"] for f in fetches
                                                         if f["failure_category"])),
                      "rereads": sum(1 for f in fetches if f["cache_hit"]), "details": fetches},
            "tools_reused": len(reused), "tools_blocked": len(blocked),
            "state_before": before, "state_after": after, "delta": delta,
            "measured": row is not None and after is not None,
            "unmeasured_reason": (unmeasured.get(turn) or {}).get("error")
            or (("final model answer (no tool calls): the acquisition policy measures only turns with tool calls"
                 if not calls else "no primary_research_turn event") if row is None else None),
            "artifacts": (row or {}).get("artifacts"),
            "qualifying_artifact": bool((row or {}).get("artifacts")) if row is not None else None,
            "no_artifact_streak": (row or {}).get("no_artifact_streak"),
            "base_gate_met": (row or {}).get("minimum_acquisition_met"),
            "stop_wanted": (wanted or {}).get("wanted_stop") or ((stopped or {}).get("reason") if final_turn else None),
            "stop_deferred_by_base_gate": wanted is not None,
            "stopped_after_this_turn": final_turn,
            "stop_reason": (stopped or {}).get("reason") if final_turn else None,
        }
        records.append(record)
        if after:
            previous_after = after
        prev_resp_seq = resp_seq
    return records


def acquisition_summary(events: list[dict], turns: list[dict]) -> dict:
    summary = next((e for e in events if e.get("kind") == "primary_research_summary"), None) or {}
    harvest = next((e for e in events if e.get("kind") == "deterministic_harvest_summary"), None) or {}
    stopped = next((e for e in events if e.get("kind") == "research_stopped"), None) or {}
    last_after = next((t["state_after"] for t in reversed(turns) if t.get("state_after")), None) or {}
    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    applicable = harvest.get("applicable_fields") or len([s for s in started.get("requested_field_specs") or []
                                                          if s.get("applicable", True)]) or None
    latencies = [c.get("latency_ms") for t in turns for c in t.get("model_calls") or [] if c.get("latency_ms")]
    tokens_in = [c.get("input_tokens") for t in turns for c in t.get("model_calls") or [] if c.get("input_tokens")]
    tokens_out = [c.get("output_tokens") for t in turns for c in t.get("model_calls") or [] if c.get("output_tokens")]
    gains = [t["delta"]["scoped_coverage_gain"] for t in turns if t.get("delta") and
             t["delta"].get("scoped_coverage_gain") is not None]
    return {
        "turns": len(turns) if turns else summary.get("turns"),
        "max_turns": turns[0]["max_turns"] if turns else summary.get("max_turns"),
        "search_calls": sum(t["search"]["calls"] for t in turns),
        "billable_search_requests": sum(t["search"]["billable_search_requests"] for t in turns),
        "repeated_searches": sum(t["search"]["repeated_queries"] for t in turns),
        "fetches_attempted": sum(t["fetch"]["attempted"] for t in turns),
        "fetches_failed": sum(t["fetch"]["failed"] for t in turns),
        "fetch_failure_categories": dict(sum((Counter(t["fetch"]["failure_categories"]) for t in turns), Counter())),
        "useful_documents": summary.get("useful_documents", last_after.get("useful_documents")),
        "official_documents": summary.get("official_sources", last_after.get("official_documents")),
        # newer runs only: official_documents then counts FETCHED official documents, this the fetched + search-seen
        # union; older runs recorded the union as official_documents and have no value here (never relabelled)
        "official_urls_discovered": summary.get("official_urls_discovered", last_after.get("official_urls_discovered")),
        "target_market_documents": summary.get("target_market_documents", last_after.get("target_market_documents")),
        "candidates": last_after.get("candidates") if last_after.get("candidates") is not None
        else harvest.get("candidate_count_total"),
        "candidate_fields": summary.get("candidate_fields", last_after.get("candidate_fields")),
        "applicable_fields": applicable,
        "final_scoped_coverage_pct": summary.get("scoped_coverage_pct", last_after.get("scoped_coverage_pct")),
        "scoped_coverage_by_turn": [t["state_after"]["scoped_coverage_pct"] if t.get("state_after") else None
                                    for t in turns],
        "scoped_coverage_gain_by_turn": [t["delta"]["scoped_coverage_gain"] if t.get("delta") else None for t in turns],
        "coverage_gain_total": round(sum(gains), 1) if gains else None,
        "no_artifact_turns": summary.get("no_artifact_turns",
                                         sum(1 for t in turns if t.get("qualifying_artifact") is False)),
        "extended_turns": summary.get("extended_turns", sum(1 for t in turns if t["extended_beyond_normal_budget"])),
        "stop_deferred_count": summary.get("stop_deferred_count"),
        "done_deferred_count": summary.get("done_deferred_count"),
        "minimum_base_met": summary.get("minimum_acquisition_met"),
        "stop_reason": summary.get("stop_reason") or stopped.get("reason"),
        "run_stop_reason": stopped.get("reason"),
        "acquisition_latency_s": stopped.get("research_s") or summary.get("research_s"),
        "model_latency_ms": sum(latencies) if latencies else None,
        "input_tokens": sum(tokens_in) if tokens_in else None,
        "output_tokens": sum(tokens_out) if tokens_out else None,
        "model_timeouts": sum(c.get("timeouts", 0) for t in turns for c in t.get("model_calls") or []),
        "model_retries": sum(c.get("retries", 0) for t in turns for c in t.get("model_calls") or []),
        **_min_base_reached(events, turns),
        "tool_blocked": sum(1 for e in events if e.get("kind") == "tool_blocked"
                            and (e.get("phase") or "research") == "research"),
        "extension_exhausted": next((e for e in events if e.get("kind") == "primary_research_extension_exhausted"),
                                    None) is not None,
        # importer site map (SITE_MAP, contract research): URLs offered, fetched by research, useful documents
        **_site_map_usage(events, summary),
    }


def _site_map_usage(events: list[dict], summary: dict) -> dict:
    usage = summary.get("site_map") if isinstance(summary.get("site_map"), dict) else {}
    event = next((e for e in events if e.get("kind") == "site_map" and e.get("stage", "acquisition") == "acquisition"),
                 None)
    if not usage and event is None:
        return {"site_map_offered": None, "site_map_fetched": None, "site_map_useful": None}
    return {"site_map_offered": usage.get("offered", len((event or {}).get("offered_urls") or [])),
            "site_map_fetched": usage.get("fetched"), "site_map_useful": usage.get("useful"),
            "site_map_url_count": (event or {}).get("url_count"),
            "site_map_duration_ms": (event or {}).get("duration_ms")}


def _min_base_reached(events: list[dict], turns: list[dict]) -> dict:
    """The first research turn whose minimum acquisition base was met (null when never), and the research tokens
    (input + output) spent up to and including it."""
    rows = sorted((e for e in events if e.get("kind") == "primary_research_turn"), key=lambda e: e.get("turn") or 0)
    turn = next((e.get("turn") for e in rows if e.get("minimum_acquisition_met")), None)
    tokens = None
    if turn is not None:
        used = [(c.get("input_tokens") or 0) + (c.get("output_tokens") or 0) for t in turns
                if t["turn_number"] <= turn for c in t.get("model_calls") or []]
        tokens = sum(used) if used else None
    return {"turn_reached_min_base": turn, "tokens_until_min_base": tokens}


# --- document sweep -----------------------------------------------------------------------------------------------------

def _evaluation_at(events: list[dict], seq: int, specs: list[dict], market: str | None) -> dict[str, dict]:
    from .field_recovery import current_evaluation
    prefix = [e for e in events if (e.get("seq") or 0) <= seq]
    return {e["field"]: e for e in current_evaluation(prefix, specs, market)} if specs else {}


def _documents_before(events: list[dict], seq: int) -> set[str]:
    docs: set[str] = set()
    for e in events:
        if (e.get("seq") or 0) >= seq:
            break
        if e.get("kind") == "document" and isinstance(e.get("document"), dict):
            if e["document"].get("document_id"):
                docs.add(str(e["document"]["document_id"]))
        elif e.get("kind") == "tool_result" and isinstance(e.get("result"), dict) and e["result"].get("document_id"):
            docs.add(str(e["result"]["document_id"]))
    return docs


def classify_sweep_evidence(item: dict, *, events: list[dict], sweep_start_seq: int, resolved: bool) -> dict:
    """The deterministic-harvest classification of one sweep evidence item (see the module docstring)."""
    from .candidate_harvest import deterministic_candidates_from_events as candidates_from_events
    from .field_recovery import material_key

    field, doc = _norm_field(item.get("field")), item.get("document_id")
    prior = [e for e in events if (e.get("seq") or 0) < sweep_start_seq]
    url_to_doc = {c.get("source_url"): c.get("document_id") for c in candidates_from_events(prior)
                  if c.get("source_url") and c.get("document_id")}
    doc = doc or url_to_doc.get(item.get("source_url"))
    base = {"evidence_id": item.get("evidence_id"), "field": field, "value": item.get("value"),
            "document_id": doc, "resolved_field": resolved}
    if not resolved:
        return {**base, "classification": UNCLASSIFIED, "reason": "field not resolved by this sweep call"}
    if str(item.get("variant_match") or "").lower() in ("different", "unbound"):
        return {**base, "classification": UNCLASSIFIED, "reason": f"variant_match={item.get('variant_match')}"}
    if not doc:
        return {**base, "classification": UNCLASSIFIED, "reason": "evidence has no document id"}
    if str(doc) not in _documents_before(events, sweep_start_seq):
        return {**base, "classification": UNCLASSIFIED, "reason": "document not in the run before the sweep"}
    values = {material_key(c.get("value")) for c in candidates_from_events(prior)
              if _norm_field(c.get("field")) == field and str(c.get("document_id")) == str(doc)}
    if not values:
        return {**base, "classification": DETERMINISTIC_HARVEST_MISS,
                "reason": "cached before the sweep; the deterministic harvest produced no candidate for this field "
                          "from this document"}
    if material_key(item.get("value")) in values:
        return {**base, "classification": PROMOTED_CANDIDATE, "reason": "matches a deterministic candidate"}
    return {**base, "classification": DETERMINISTIC_VALUE_MISMATCH,
            "reason": "the harvest produced a candidate for this field from this document with a different value"}


def sweep_calls(events: list[dict]) -> list[dict]:
    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    specs = [s for s in started.get("requested_field_specs") or []]
    market = started.get("target_market") or (started.get("agent_config") or {}).get("target_market")
    sweep_starts = [e for e in events if e.get("kind") == "document_sweep_started"]
    if not sweep_starts:
        return []
    finished = next((e for e in events if e.get("kind") == "document_sweep_finished"), None)
    end_all = (finished or {}).get("seq") or max(e.get("seq") or 0 for e in events)
    first_seq = sweep_starts[0].get("seq") or 0
    inspection = next((e for e in events if e.get("kind") == "document_inspection" and e.get("stage") == "pre_sweep"),
                      None) or {}
    located = set((inspection.get("locations") or {}).keys())
    records = []
    for index, start in enumerate(sweep_starts, start=1):
        s_seq = start.get("seq") or 0
        e_seq = (sweep_starts[index].get("seq") - 1) if index < len(sweep_starts) else end_all
        window = [e for e in events if s_seq < (e.get("seq") or 0) <= e_seq]
        fields = [_norm_field(f) for f in start.get("fields_to_review") or []]
        before = _evaluation_at(events, s_seq, specs, market)
        after = _evaluation_at(events, e_seq, specs, market)
        model = _model_calls(events, start=s_seq, end=e_seq, phase_prefix="document_sweep")
        failed = next((e for e in window if e.get("kind") == "document_sweep_failed"), None)
        evidence = [e["evidence"] for e in window if e.get("kind") == "evidence" and isinstance(e.get("evidence"), dict)]
        rejected = [e for e in window if e.get("kind") == "evidence_rejected"]
        tool_errors = [e for e in window if e.get("kind") == "tool_result" and e.get("name") in EVIDENCE_TOOLS
                       and isinstance(e.get("result"), dict) and e["result"].get("error")]
        calls_by_id = {e.get("call_id"): e for e in window if e.get("kind") == "tool_call"}
        prior_candidates = {}
        try:
            from .candidate_harvest import candidates_from_events
            for c in candidates_from_events([e for e in events if (e.get("seq") or 0) < s_seq]):
                prior_candidates[_norm_field(c.get("field"))] = prior_candidates.get(_norm_field(c.get("field")), 0) + 1
        except Exception:  # noqa: BLE001
            pass
        presented = start.get("candidates_per_field") or {}
        per_field = []
        classifications = []
        for name in fields:
            b, a = before.get(name) or {}, after.get(name) or {}
            resolved = bool(b.get("retry_eligible")) and not a.get("retry_eligible", True)
            mine = [x for x in evidence if _norm_field(x.get("field")) == name]
            rej = [x for x in rejected if _norm_field(x.get("field")) == name]
            malformed = [x for x in tool_errors
                         if _norm_field(_args((calls_by_id.get(x.get("call_id")) or {}).get("arguments")).get("field"))
                         == name]
            classes = [classify_sweep_evidence(x, events=events, sweep_start_seq=first_seq, resolved=resolved)
                       for x in mine]
            classifications += classes
            per_field.append({
                "field_id": name, "status_before": b.get("state"), "status_after": a.get("state"),
                "resolved_by_this_call": resolved,
                "candidate_count": prior_candidates.get(name, 0),
                "candidates_presented": presented.get(name, 0) if presented else None,
                "admitted_evidence_before": len(b.get("evidence_ids") or []),
                "located_by_local_inspection": name in located,
                "deterministic_signal": ("candidates" if prior_candidates.get(name) else
                                         "location_only" if name in located else "none"),
                "evidence_admitted": [{"evidence_id": x.get("evidence_id"), "value": x.get("value"),
                                       "document_id": x.get("document_id")} for x in mine],
                "evidence_rejected": [{"value": x.get("value"), "reasons": x.get("reasons")} for x in rej],
                "malformed_tool_outputs": len(malformed),
                "classifications": [c["classification"] for c in classes],
            })
        timeouts = sum(c.get("timeouts", 0) for c in model)
        latency = sum(c.get("latency_ms") or 0 for c in model)
        tokens_in = [c["input_tokens"] for c in model if c.get("input_tokens") is not None]
        tokens_out = [c["output_tokens"] for c in model if c.get("output_tokens") is not None]
        replies = [e for e in window if e.get("kind") == "model_response" and not e.get("tool_calls")]
        malformed_reply = False
        if replies:
            from .agent import parse_model_output
            text = replies[-1].get("content") or ""
            malformed_reply = bool(text.strip()) and parse_model_output(text)[0] is None
        resolved_count = sum(1 for f in per_field if f["resolved_by_this_call"])
        records.append({
            "type": "sweep_call", "sweep_call_number": index, "chunk": start.get("chunk"),
            # adjudication packets are no-tool calls with a JSON reply; legacy chunks are tool loops
            "sweep_mode": start.get("sweep_mode") or "legacy", "packet_class": start.get("packet_class"),
            "model": model[-1] if model else None, "model_calls": model, "model_call_count": len(model),
            "latency_ms": latency or None, "wall_latency_s": _seconds(start.get("ts"),
                                                                       (window[-1] if window else start).get("ts")),
            "timeout": timeouts > 0, "timeouts": timeouts, "retries": sum(c.get("retries", 0) for c in model),
            "input_tokens": sum(tokens_in) if tokens_in else None,
            "output_tokens": sum(tokens_out) if tokens_out else None,
            "input": {"fields_entering": len(fields), "field_ids": fields,
                      "cached_documents_available": start.get("documents"),
                      "packet_document_ids": start.get("packet_document_ids"),
                      "packet_chars": start.get("packet_chars"),
                      "estimated_input_tokens": start.get("estimated_input_tokens"),
                      "candidates_supplied": start.get("candidates_presented"),
                      "candidates_per_field": presented or None},
            "fields": per_field,
            "fields_entering": len(fields), "fields_resolved": resolved_count,
            "fields_remaining": len(fields) - resolved_count,
            "evidence_admitted": len(evidence), "evidence_rejected": len(rejected),
            "rejection_reasons": dict(Counter(r for x in rejected for r in (x.get("reasons") or []))),
            "malformed_field_outputs": len(tool_errors), "malformed_final_reply": malformed_reply,
            "deterministic_classification": dict(Counter(c["classification"] for c in classifications)),
            "classification_details": classifications,
            "call_success": failed is None and bool(model) and not any(c.get("failed") for c in model),
            "error": (failed or {}).get("error"),
        })
    return records


def _grounded_summary(events: list[dict]) -> dict:
    done = [e for e in events if e.get("kind") == "grounded_candidates_finished" and e.get("stage") != "reacquire"]
    if not done:
        return {"grounded_calls": None, "grounded_items": None, "grounded_admissible": None}
    return {"grounded_calls": sum(int(e.get("model_calls") or 0) for e in done),
            "grounded_items": sum(int(e.get("items") or 0) for e in done),
            "grounded_admissible": sum(int(e.get("admissible") or 0) for e in done),
            "grounded_not_admissible": sum(int(e.get("not_admissible") or 0) for e in done),
            # why grounded items did not become admissible: admission reasons + offset_invalid / quote_not_in_document /
            # unknown_field (grounded-v1 events before this count carried no reasons)
            "grounded_not_admissible_by_reason": dict(sum((Counter(e.get("not_admissible_by_reason") or {})
                                                           for e in done), Counter())),
            "grounded_fields_with_admissible": sorted({f for e in done for f in e.get("fields_with_admissible") or []})}


def sweep_summary(events: list[dict], calls: list[dict]) -> dict:
    finished = next((e for e in events if e.get("kind") in ("document_sweep_finished", "document_sweep_skipped")),
                    None) or {}
    classes = sum((Counter(c["deterministic_classification"]) for c in calls), Counter())
    model_calls = sum(c["model_call_count"] for c in calls)
    entering = len({f for c in calls for f in c["input"]["field_ids"]})
    resolved = len({f["field_id"] for c in calls for f in c["fields"] if f["resolved_by_this_call"]})
    evidence_in, evidence_out = sum(c["evidence_admitted"] for c in calls), sum(c["evidence_rejected"] for c in calls)
    tok_in = [c["input_tokens"] for c in calls if c["input_tokens"] is not None]
    tok_out = [c["output_tokens"] for c in calls if c["output_tokens"] is not None]
    return {
        "skipped": finished.get("reason") if finished.get("kind") == "document_sweep_skipped" else finished.get("skipped"),
        "calls": len(calls), "model_calls": model_calls,
        "fields_entered": entering, "fields_resolved": resolved, "fields_remaining": entering - resolved,
        "resolution_rate": round(resolved / entering, 3) if entering else None,
        "resolved_per_model_call": round(resolved / model_calls, 2) if model_calls else None,
        "deterministic_harvest_misses_recovered": classes.get(DETERMINISTIC_HARVEST_MISS, 0),
        "deterministic_value_mismatches": classes.get(DETERMINISTIC_VALUE_MISMATCH, 0),
        "promoted_candidates": classes.get(PROMOTED_CANDIDATE, 0),
        "unclassified_evidence": classes.get(UNCLASSIFIED, 0),
        # the pre-existing counter (src/document_sweep.py: misses + value mismatches together), for comparison
        "document_sweep_deterministic_misses_found": finished.get("deterministic_misses_found"),
        "evidence_accepted": evidence_in, "evidence_rejected": evidence_out,
        "evidence_rejection_rate": round(evidence_out / (evidence_in + evidence_out), 3)
        if evidence_in + evidence_out else None,
        "malformed_field_outputs": sum(c["malformed_field_outputs"] for c in calls),
        "input_tokens": sum(tok_in) if tok_in else None, "output_tokens": sum(tok_out) if tok_out else None,
        "latency_ms": sum(c["latency_ms"] or 0 for c in calls) or None,
        "stage_latency_ms": finished.get("document_sweep_latency_ms"),
        # grounded candidates (GROUNDED_CANDIDATES, sweep stage): reported apart from the deterministic harvest
        **_grounded_summary(events),
        "timeouts": sum(c["timeouts"] for c in calls),
        "failed_calls": sum(1 for c in calls if not c["call_success"]),
        # candidates offered only in failed chunks (never seen by a model; still fresh for recovery's local pass)
        "failed_chunk_candidates_kept_fresh": finished.get("failed_chunk_candidates_kept_fresh"),
    }


# --- end to end -------------------------------------------------------------------------------------------------------

FINAL_STATE_KEYS = ("ok", "conflicting", "unresolved_or_missing", "foreign_market_only", "variant_not_exact",
                    "not_applicable", "other")


def _evaluation_event(events: list[dict], stage: str) -> dict | None:
    return next((e for e in reversed(events) if e.get("kind") == "field_evaluation" and e.get("stage") == stage), None)


def _searches_by_reason(episodes: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in episodes:
        reason = str(e.get("budget_reason") or "other")
        out[reason] = out.get(reason, 0) + int(e.get("billable_searches", e.get("searches")) or 0)
    return out


def _reacquire_summary(window: list[dict]) -> dict:
    episodes = [e for e in window if e.get("kind") == "field_recovery_finished" and e.get("mode") == "reacquire"]
    if not episodes:
        return {}
    total = lambda key: sum(int(e.get(key) or 0) for e in episodes)   # noqa: E731
    return {"mode": "reacquire", "reacquire_episodes": len(episodes),
            "reacquire_new_useful_documents": total("new_useful_documents"),
            "reacquire_new_candidates": total("new_candidates"), "reacquire_grounded_items": total("grounded_items"),
            "reacquire_adjudication_accepted": total("adjudication_accepted"), "reacquire_admitted": total("admitted"),
            "reacquire_fields_resolved": sum(len(e.get("fields_resolved") or []) for e in episodes),
            "reacquire_site_map_urls": total("site_map_urls"),
            # billable searches per budget reason (technical_gap / trim_gap / other; PR #35 recovery spend caps)
            "reacquire_searches_by_reason": _searches_by_reason(episodes),
            "reacquire_clusters": [{k: e.get(k) for k in ("cluster", "searches", "fetches", "new_useful_documents",
                                                          "new_candidates", "grounded_items", "adjudication_accepted",
                                                          "admitted", "fields_resolved", "model_calls", "tokens",
                                                          "stop", "error")} for e in episodes]}


def recovery_summary(events: list[dict]) -> dict:
    """Tail recovery from events: model calls, billable searches, fetches, attempts, and fields resolved between the
    primary evaluation and the after-recovery evaluation."""
    primary, final = _evaluation_event(events, "primary"), _evaluation_event(events, "after_recovery")
    start = (primary or {}).get("seq") or 0
    end = (final or {}).get("seq") or max([e.get("seq") or 0 for e in events] or [0])
    window = [e for e in events if start < (e.get("seq") or 0) <= end]
    calls = [e for e in window if e.get("kind") == "model_response"
             and str(e.get("phase") or "").startswith("field_recovery")]
    before = {f.get("field") for f in (primary or {}).get("fields") or [] if f.get("retry_eligible")}
    after = {f.get("field") for f in (final or {}).get("fields") or [] if f.get("retry_eligible")}
    return {
        "ran": primary is not None,
        "attempts": sum(1 for e in window if e.get("kind") in ("field_recovery_started", "cluster_recovery_started",
                                                                "reacquire_started")),
        # RECOVERY_MODE=reacquire: per-cluster episodes (targeted acquire -> harvest -> grounded -> adjudicate)
        **_reacquire_summary(window),
        # fields no web episode was scheduled for: variant_not_exact with admitted official target-market evidence
        "reacquire_skipped_fields": sorted({f for e in window if e.get("kind") == "reacquire_skipped_binding_gap"
                                            for f in e.get("fields") or []}),
        "model_calls": len(calls),
        "input_tokens": sum((e.get("usage") or {}).get("prompt_tokens") or 0 for e in calls) or None,
        "output_tokens": sum((e.get("usage") or {}).get("completion_tokens") or 0 for e in calls) or None,
        "billable_searches": sum(1 for e in window
                                 if e.get("kind") == "api_call" and e.get("request_kind") == "search"),
        "fetches": sum(1 for e in window if e.get("kind") == "tool_call" and e.get("name") in FETCH_TOOLS
                       and str(e.get("phase") or "").startswith("field_recovery")),
        "fields_open_before": len(before) if primary is not None else None,
        "fields_open_after": len(after) if final is not None else None,
        "fields_resolved": len(before - after) if primary is not None and final is not None else None,
        # cluster attempts that ended on an API error (each ends only its own attempt), and whether consecutive
        # failures stopped all recovery (provider-outage guard)
        "failed_attempts": sum(1 for e in window if e.get("kind") == "field_recovery_failed"
                               and e.get("attempt") is not None),
        "api_failure_stop": any(e.get("kind") == "field_recovery_api_failure_stop" for e in window),
    }


def final_field_states(events: list[dict]) -> dict:
    """The final state of every requested field: current_evaluation() over the run's final events (the one
    field-state authority), counted by state, plus the list of ok fields."""
    from .field_recovery import current_evaluation

    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    specs = list(started.get("requested_field_specs") or [])
    if not specs:
        return {"counts": None, "ok_fields": None, "fields": 0, "binding_gaps": {}, "binding_gap_counts": {}}
    evaluation = current_evaluation(events, specs, started.get("target_market"))
    counts = {k: 0 for k in FINAL_STATE_KEYS}
    for e in evaluation:
        state = e["state"]
        key = "unresolved_or_missing" if state in ("unresolved", "missing") else state if state in counts else "other"
        counts[key] += 1
    # variant_not_exact fields: the binding dimensions that stopped them (field_recovery info `binding_gap:*`)
    gaps = {e["field"]: [str(i).split(":", 1)[1] for i in e.get("info") or [] if str(i).startswith("binding_gap:")]
            for e in evaluation if e["state"] == "variant_not_exact"}
    return {"counts": counts, "ok_fields": sorted(e["field"] for e in evaluation if e["state"] == "ok"),
            "fields": len(evaluation), "binding_gaps": gaps,
            "binding_gap_counts": dict(Counter(g for items in gaps.values() for g in items))}


YEAR_STATUSES = ("match", "adjacent", "mixed", "mismatch", "absent")


def binding_year_summary(events: list[dict]) -> dict:
    """Year-dimension telemetry over the run's admitted evidence (binding-v3): how many items' effective year status
    was match / adjacent / mixed / mismatch / absent (binding_dimensions.year; absent when not recorded), and how many
    years the model-year rules ignored, by kind (year_context.ignored: copyright, publication_date, date, url, prose,
    ...). Observational: `adjacent` and ignored years never feed binding_gap."""
    from .storage import trace

    status = {k: 0 for k in YEAR_STATUSES}
    ignored: Counter = Counter()
    items = [e for e in trace.evidence_items(events) if e.get("admission_status", "accepted") == "accepted"]
    for item in items:
        value = str(((item.get("binding_dimensions") or {}).get("year") or {}).get("status") or "absent")
        status[value if value in status else "absent"] += 1
        ignored.update(str(r.get("kind")) for r in (item.get("year_context") or {}).get("ignored") or [])
    return {"evidence": len(items), "binding_year_status_counts": status, "ignored_year_contexts": dict(ignored)}


def run_totals(events: list[dict], result: dict | None = None) -> dict:
    """Model calls and tokens per phase and in total (provider usage of successful responses), cost (the run's own
    result.json cost when present) and wall time."""
    from .storage import trace

    by_phase = trace.usage_by_phase(events)
    reasoning = {group: None for group in by_phase}
    for e in events:         # provider-reported reasoning tokens (usage.completion_tokens_details.reasoning_tokens)
        if e.get("kind") == "model_response":
            tokens = e.get("reasoning_tokens")
            if tokens is None:
                tokens = trace.reasoning_tokens(e.get("usage"))
            if tokens is not None:
                group = trace.phase_group(e.get("phase"))
                reasoning[group] = (reasoning.get(group) or 0) + int(tokens)
    # responses cut at max_tokens (finish_reason "length") and the one-time retries with a doubled max_tokens
    truncated = {group: {"truncated_calls": 0, "truncation_retries": 0} for group in by_phase}
    for e in events:
        key = {"model_output_truncated": "truncated_calls", "truncation_retry": "truncation_retries"}.get(e.get("kind"))
        if key:
            truncated.setdefault(trace.phase_group(e.get("phase")), {"truncated_calls": 0, "truncation_retries": 0})[
                key] += 1
    phases = {group: {"model_calls": u.get("model_calls", 0), "input_tokens": u.get("prompt_tokens", 0),
                      "output_tokens": u.get("completion_tokens", 0), "reasoning_tokens": reasoning.get(group),
                      **truncated.get(group, {"truncated_calls": 0, "truncation_retries": 0})}
              for group, u in by_phase.items()}
    finished = next((e for e in reversed(events) if e.get("kind") == "run_finished"), None) or {}
    started = next((e for e in events if e.get("kind") == "run_started"), None) or {}
    wall = (result or {}).get("duration_s") or finished.get("duration_s") \
        or _seconds(started.get("ts"), (events[-1] if events else {}).get("ts"))
    cost = (result or {}).get("cost") or finished.get("cost") or {}
    return {"model_calls": sum(p["model_calls"] for p in phases.values()),
            "input_tokens": sum(p["input_tokens"] for p in phases.values()),
            "output_tokens": sum(p["output_tokens"] for p in phases.values()),
            # null when no response reported reasoning tokens
            "reasoning_tokens": sum(p["reasoning_tokens"] for p in phases.values() if p["reasoning_tokens"] is not None)
            if any(p["reasoning_tokens"] is not None for p in phases.values()) else None,
            "by_phase": phases, "cost_usd": cost.get("total_usd") if isinstance(cost, dict) else None,
            "cost_source": "result.json" if (result or {}).get("cost") else "run_finished" if finished.get("cost")
            else None, "wall_time_s": wall}


def parser_gap_summary(events: list[dict]) -> dict:
    """The run's parser gaps (src/parser_gaps.py: a field label in a document, no harvested value) joined with what the
    document sweep found: per field {label_hits_no_value, recovered_by_sweep}, where recovered_by_sweep counts
    `candidate_missed_by_deterministic_harvest` events for that field on one of that field's gap documents."""
    event = next((e for e in reversed(events) if e.get("kind") == "parser_gaps"), None)
    if event is None:
        return {"recorded": False, "failed": any(e.get("kind") == "parser_gaps_failed" for e in events)}
    rows = [r for r in event.get("rows") or [] if isinstance(r, dict)]
    fields: dict[str, dict] = {}
    gap_docs: dict[str, set] = {}
    for row in rows:
        name = _norm_field(row.get("field"))
        fields.setdefault(name, {"label_hits_no_value": 0, "recovered_by_sweep": 0})["label_hits_no_value"] += 1
        gap_docs.setdefault(name, set()).add(str(row.get("document_id")))
    for missed in events:
        if missed.get("kind") != "candidate_missed_by_deterministic_harvest":
            continue
        name = _norm_field(missed.get("field"))
        if str(missed.get("document_id")) in gap_docs.get(name, ()):
            fields[name]["recovered_by_sweep"] += 1
    return {"recorded": True, "gaps_total": event.get("gaps_total", len(rows)), "truncated": event.get("truncated", 0),
            "fields_with_gaps": event.get("fields_with_gaps", len(fields)),
            "documents_scanned": event.get("documents_scanned"), "duration_ms": event.get("duration_ms"),
            "recovered_by_sweep": sum(f["recovered_by_sweep"] for f in fields.values()),
            "fields": fields, "rows": rows}


def run_configuration(events: list[dict]) -> dict:
    """The configuration a run used, as recorded at run start (runs before ACQUISITION_MODE are legacy)."""
    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    agent = started.get("agent_config") or {}
    glm = started.get("glm_config") or {}
    phases = glm.get("phase_settings") or {}
    sweep = phases.get("document_sweep") or {}
    acquisition_mode = started.get("acquisition_mode") or agent.get("acquisition_mode") or "legacy"
    recovery_mode = started.get("recovery_mode") or agent.get("recovery_mode") or "cluster"
    site_map = bool(started.get("site_map", agent.get("site_map", False)))
    return {"acquisition_mode": acquisition_mode,
            # runs logged before SWEEP_MODE existed used the tool-loop sweep
            "sweep_mode": started.get("sweep_mode") or agent.get("sweep_mode") or "legacy",
            "research_model": started.get("research_model") or started.get("model"),
            "sweep_model": sweep.get("model") or started.get("research_model") or started.get("model"),
            "sweep_thinking": sweep.get("thinking") or (glm.get("thinking") if isinstance(glm.get("thinking"), str)
                                                        else None),
            "sweep_max_attempts": sweep.get("max_attempts") or glm.get("chat_max_attempts"),
            # the effective reasoning effort per phase (null for runs logged before reasoning_effort existed)
            **{f"{label}_reasoning_effort": (phases.get(phase) or {}).get("reasoning_effort")
               for label, phase in REASONING_PHASES},
            # runs before FINAL_ASSEMBLY used the finalizer model ("llm")
            "final_assembly": started.get("final_assembly") or agent.get("final_assembly") or "llm",
            "sweep_max_fields": agent.get("document_sweep_max_fields"),
            "sweep_max_candidates": agent.get("document_sweep_max_candidates"),
            "sweep_max_packet_chars": agent.get("document_sweep_packet_max_chars"),
            # runs before the document card / run profiles had neither: card off, no profile
            "document_card": bool(started.get("acquisition_document_card", agent.get("acquisition_document_card"))),
            "run_profile": started.get("run_profile") or agent.get("run_profile") or None,
            # PR #31 switches; runs logged before them had no site map, no grounded candidates and cluster recovery
            "site_map": site_map,
            # the site map only acts in contract acquisition and reacquire recovery (it is part of the Treatment)
            "site_map_used": site_map and (acquisition_mode == "contract" or recovery_mode == "reacquire"),
            "grounded_candidates": bool(started.get("grounded_candidates", agent.get("grounded_candidates", False))),
            "recovery_mode": recovery_mode,
            # cross-run research memory (fact reuse, recovery yield) and negative-route blocking (off for single runs
            # of the A/B arms outside a series, src/run_profiles.isolate_single_run)
            "research_memory": bool(agent.get("research_memory_enabled", True)),
            "negative_route_blocking": bool(agent.get("negative_route_blocking", True)),
            # informational (env values differing from the code defaults; a named profile ignores them): not part of
            # config_key, whose other entries already hold the effective values
            "env_overrides": [o.get("text") for o in started.get("env_overrides") or [] if isinstance(o, dict)]}


REASONING_PHASES = (("research", "research"), ("sweep", "document_sweep"), ("recovery", "recovery"),
                    ("finalizer", "finalizer"))


def config_key(config: dict) -> str:
    return " | ".join(f"{k}={config.get(k)}" for k in ("run_profile", "acquisition_mode", "sweep_mode",
                                                         "document_card", "research_model", "sweep_model",
                                                         "sweep_thinking", "sweep_max_attempts", "sweep_max_fields",
                                                         "sweep_max_candidates", "research_reasoning_effort",
                                                         "sweep_reasoning_effort", "recovery_reasoning_effort",
                                                         "finalizer_reasoning_effort", "final_assembly",
                                                         "site_map", "site_map_used", "grounded_candidates",
                                                         "recovery_mode", "research_memory",
                                                         "negative_route_blocking"))


# --- run level ----------------------------------------------------------------------------------------------------------

def _fmt(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def summary_text(acq: dict, sweep: dict) -> str:
    fields = (f"{acq.get('candidate_fields')} / {acq.get('applicable_fields')}"
              if acq.get("candidate_fields") is not None and acq.get("applicable_fields") else _fmt(acq.get("candidate_fields")))
    lines = ["SOURCE ACQUISITION", "",
             f"Turns: {_fmt(acq.get('turns'))}", f"Search calls: {_fmt(acq.get('search_calls'))}",
             f"Useful documents: {_fmt(acq.get('useful_documents'))}",
             *([f"Official docs (fetched): {_fmt(acq.get('official_documents'))}",
                f"Official URLs (discovered): {_fmt(acq.get('official_urls_discovered'))}"]
               if acq.get("official_urls_discovered") is not None
               else [f"Official documents: {_fmt(acq.get('official_documents'))}"]),
             f"Target-market documents: {_fmt(acq.get('target_market_documents'))}",
             f"Candidates: {_fmt(acq.get('candidates'))}", f"Candidate fields: {fields}",
             f"Final scoped coverage: {_fmt(acq.get('final_scoped_coverage_pct'), '%')}",
             f"No-artifact turns: {_fmt(acq.get('no_artifact_turns'))}",
             f"Extended research turns: {_fmt(acq.get('extended_turns'))}",
             f"Stop reason: {_fmt(acq.get('stop_reason'))}", "", "DOCUMENT SWEEP", ""]
    if sweep.get("skipped"):
        lines.append(f"Skipped: {sweep['skipped']}")
    else:
        latency = sweep.get("latency_ms")
        lines += [f"Calls: {_fmt(sweep.get('calls'))} ({_fmt(sweep.get('model_calls'))} model calls)",
                  f"Fields entered: {_fmt(sweep.get('fields_entered'))}",
                  f"Fields resolved: {_fmt(sweep.get('fields_resolved'))}",
                  f"Fields remaining: {_fmt(sweep.get('fields_remaining'))}",
                  f"Deterministic harvest misses recovered: {_fmt(sweep.get('deterministic_harvest_misses_recovered'))}",
                  f"Evidence accepted: {_fmt(sweep.get('evidence_accepted'))}",
                  f"Evidence rejected: {_fmt(sweep.get('evidence_rejected'))}",
                  f"Input tokens: {_fmt(sweep.get('input_tokens'))}", f"Output tokens: {_fmt(sweep.get('output_tokens'))}",
                  f"Latency: {_fmt(round(latency / 1000, 1) if latency else None, ' s')}",
                  f"Timeouts: {_fmt(sweep.get('timeouts'))}"]
    return "\n".join(lines)


def vehicle_diagnostics(events: list[dict], *, run_id: str | None = None, record_id: str | None = None,
                        result: dict | None = None) -> dict:
    """`result`: the run's result.json when present (its cost / duration win over event-derived values)."""
    started = next((e for e in events if e.get("kind") == "run_started"), {}) or {}
    finished = next((e for e in reversed(events) if e.get("kind") == "run_finished"), None) or {}
    turns = acquisition_turns(events)
    calls = sweep_calls(events)
    acq, sweep = acquisition_summary(events, turns), sweep_summary(events, calls)
    status = finished.get("status") or (result or {}).get("status")
    interrupted = (not finished or status in ("interrupted", "incomplete")
                   or bool((result or {}).get("interrupted")))
    return {
        "schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id, "record_id": record_id or started.get("record_id"),
        "vehicle": started.get("vehicle_label"), "target_market": started.get("target_market"),
        # what the run was configured with (src/phase_settings.py describe()); per-call models are in each record
        "configured_models": {phase: (((started.get("glm_config") or {}).get("phase_settings") or {}).get(phase) or {})
                              .get("model") for phase in ("research", "document_sweep", "recovery", "finalizer")}
        | {"research_fallback": started.get("research_model") or started.get("model")},
        "run_status": status, "complete": bool(finished), "interrupted": interrupted,
        "configuration": run_configuration(events),
        "acquisition": {"summary": acq, "turns": turns},
        "document_sweep": {"summary": sweep, "calls": calls},
        "recovery": recovery_summary(events),
        "final_fields": final_field_states(events),
        "binding_year": binding_year_summary(events),
        "parser_gaps": parser_gap_summary(events),
        "totals": run_totals(events, result),
        "summary_text": summary_text(acq, sweep),
        "note": "observational telemetry; nothing here changes research behaviour",
    }


def write_vehicle_diagnostics(run_dir: Path | str, *, run_id: str | None = None) -> dict | None:
    """Build and atomically write diagnostics.json / .jsonl into the vehicle run folder. Never raises."""
    from .app_config import redact_obj
    from .storage.atomic import atomic_write_json, atomic_write_text
    from .storage.run_log import read_events

    run_dir = Path(run_dir)
    try:
        events = read_events(run_dir / "events.jsonl")
        if not events:
            return None
        result = None
        if (run_dir / "result.json").is_file():
            try:
                result = json.loads((run_dir / "result.json").read_text("utf-8"))
            except ValueError:
                result = None
        diag = redact_obj(vehicle_diagnostics(events, run_id=run_id or run_dir.parent.name, record_id=run_dir.name,
                                              result=result if isinstance(result, dict) else None))
        atomic_write_json(run_dir / DIAGNOSTICS_FILE, diag, durable=True)
        lines = [json.dumps({"run_id": diag["run_id"], "record_id": diag["record_id"], **row}, ensure_ascii=False,
                            default=str) for row in diag["acquisition"]["turns"] + diag["document_sweep"]["calls"]]
        atomic_write_text(run_dir / DIAGNOSTICS_STREAM, "\n".join(lines) + ("\n" if lines else ""), durable=True)
        return diag
    except Exception as exc:  # noqa: BLE001 - diagnostics must never cost a run
        try:
            from .server_logging import get_logger
            get_logger("diagnostics").warning("diagnostics for %s failed: %s: %s", run_dir, type(exc).__name__, exc)
        except Exception:  # noqa: BLE001
            pass
        return None


def load_vehicle_diagnostics(run_dir: Path | str, *, rebuild_if_missing: bool = True) -> dict | None:
    path = Path(run_dir) / DIAGNOSTICS_FILE
    diag = None
    if path.is_file():
        try:
            diag = json.loads(path.read_text("utf-8"))
        except ValueError:
            diag = None
    if diag is None and rebuild_if_missing:
        diag = write_vehicle_diagnostics(run_dir)
    return with_binding_replay(diag, run_dir)


def with_binding_replay(diag: dict | None, run_dir: Path | str) -> dict | None:
    """The diagnostics with this run's CURRENT Binding Replay summary, when one exists.

    Binding Replay summaries are cached by a content hash of the binding code/data. Never surface a stale summary in
    benchmark diagnostics after the binding code changes: the UI / CLI can regenerate it on demand."""
    if diag is None:
        return None
    try:
        from .binding_replay import load_replay
        replay = load_replay(run_dir)
    except Exception:  # noqa: BLE001 - replay metadata must never break diagnostics
        replay = None
    summary = (replay or {}).get("summary")
    vehicle = (summary or {}).get("vehicle") if isinstance(summary, dict) else None
    if not isinstance(vehicle, dict):
        return diag
    return {**diag, "binding_replay": {"code_version": summary.get("code_version"), **vehicle}}


# --- benchmark aggregation -----------------------------------------------------------------------------------------------

def vehicle_row(diag: dict) -> dict:
    a, s = diag["acquisition"]["summary"], diag["document_sweep"]["summary"]
    turns = diag["acquisition"]["turns"]
    searches = a.get("search_calls") or 0
    # end-to-end sections are absent from diagnostics.json files written before they existed
    rec, final, totals = diag.get("recovery") or {}, diag.get("final_fields") or {}, diag.get("totals") or {}
    config = diag.get("configuration") or {}
    gaps = diag.get("parser_gaps") or {}
    gaps = gaps if gaps.get("recorded") else {}
    counts = final.get("counts") or {}
    year, replay = diag.get("binding_year") or {}, diag.get("binding_replay") or {}
    phases = totals.get("by_phase") or {}
    research = phases.get("research") or {}
    return {"run_id": diag.get("run_id"), "record_id": diag.get("record_id"),
            "vehicle": " ".join(str((diag.get("vehicle") or {}).get(k) or "") for k in ("manufacturer", "model",
                                                                                         "year", "trim")).strip(),
            "run_status": diag.get("run_status"), "interrupted": bool(diag.get("interrupted")),
            "config_key": config_key(config) if config else None,
            **{f"cfg_{k}": v for k, v in config.items()},
            "acq_turns": a.get("turns"), "acq_search_calls": searches, "acq_repeated_searches": a.get("repeated_searches"),
            "acq_useful_documents": a.get("useful_documents"), "acq_official_documents": a.get("official_documents"),
            "acq_target_market_documents": a.get("target_market_documents"), "acq_candidates": a.get("candidates"),
            "acq_candidate_fields": a.get("candidate_fields"), "acq_applicable_fields": a.get("applicable_fields"),
            "acq_final_scoped_coverage_pct": a.get("final_scoped_coverage_pct"),
            "acq_coverage_gain_total": a.get("coverage_gain_total"),
            "acq_coverage_gain_per_turn": round(a["coverage_gain_total"] / len(turns), 2)
            if a.get("coverage_gain_total") is not None and turns else None,
            "acq_coverage_gain_per_search": round(a["coverage_gain_total"] / searches, 2)
            if a.get("coverage_gain_total") is not None and searches else None,
            "acq_no_artifact_turns": a.get("no_artifact_turns"), "acq_extended_turns": a.get("extended_turns"),
            "acq_stop_reason": a.get("stop_reason"),
            "acq_hard_max": a.get("stop_reason") == "hard_max_turns_under_acquired",
            "acq_latency_s": a.get("acquisition_latency_s"), "acq_input_tokens": a.get("input_tokens"),
            "acq_output_tokens": a.get("output_tokens"), "acq_fetch_failures": a.get("fetches_failed"),
            "sweep_skipped": s.get("skipped"), "sweep_calls": s.get("calls"), "sweep_model_calls": s.get("model_calls"),
            "sweep_fields_entered": s.get("fields_entered"), "sweep_fields_resolved": s.get("fields_resolved"),
            "sweep_resolution_rate": s.get("resolution_rate"),
            "sweep_deterministic_misses_recovered": s.get("deterministic_harvest_misses_recovered"),
            "sweep_value_mismatches": s.get("deterministic_value_mismatches"),
            "sweep_evidence_accepted": s.get("evidence_accepted"), "sweep_evidence_rejected": s.get("evidence_rejected"),
            "sweep_rejection_rate": s.get("evidence_rejection_rate"), "sweep_input_tokens": s.get("input_tokens"),
            "sweep_output_tokens": s.get("output_tokens"), "sweep_latency_ms": s.get("latency_ms"),
            "sweep_timeouts": s.get("timeouts"), "sweep_resolved_per_model_call": s.get("resolved_per_model_call"),
            "sweep_failed_chunk_candidates_kept_fresh": s.get("failed_chunk_candidates_kept_fresh"),
            "sweep_grounded_calls": s.get("grounded_calls"), "sweep_grounded_admissible": s.get("grounded_admissible"),
            "sweep_grounded_not_admissible_by_reason": json.dumps(s["grounded_not_admissible_by_reason"],
                                                                  sort_keys=True, ensure_ascii=False)
            if s.get("grounded_not_admissible_by_reason") is not None else None,
            "acq_official_urls_discovered": a.get("official_urls_discovered"),
            "acq_tool_blocked": a.get("tool_blocked"), "acq_turn_reached_min_base": a.get("turn_reached_min_base"),
            "acq_tokens_until_min_base": a.get("tokens_until_min_base"),
            "acq_tokens": (a.get("input_tokens") or 0) + (a.get("output_tokens") or 0)
            if a.get("input_tokens") is not None or a.get("output_tokens") is not None else None,
            "acq_extension_exhausted": a.get("extension_exhausted"),
            "acq_site_map_offered": a.get("site_map_offered"), "acq_site_map_fetched": a.get("site_map_fetched"),
            "acq_site_map_useful": a.get("site_map_useful"),
            "acq_done_deferred": a.get("done_deferred_count"),
            "parser_gap_rows": gaps.get("gaps_total"), "parser_gap_fields": gaps.get("fields_with_gaps"),
            "parser_gap_recovered_by_sweep": gaps.get("recovered_by_sweep"),
            "rec_attempts": rec.get("attempts"), "rec_model_calls": rec.get("model_calls"),
            "rec_billable_searches": rec.get("billable_searches"), "rec_fetches": rec.get("fetches"),
            "rec_fields_open_before": rec.get("fields_open_before"),
            "rec_fields_open_after": rec.get("fields_open_after"),
            "rec_fields_resolved": rec.get("fields_resolved"),
            **{f"final_{k}": counts.get(k) for k in FINAL_STATE_KEYS},
            "final_ok_fields": ",".join(final.get("ok_fields") or []) if final.get("ok_fields") is not None else None,
            # variant_not_exact fields stopped by the trim / by a technical dimension (a field may count in both)
            "binding_gap_trim": sum(1 for g in (final.get("binding_gaps") or {}).values() if any(map(is_trim_gap, g)))
            if final.get("binding_gaps") is not None else None,
            "binding_gap_technical": sum(1 for g in (final.get("binding_gaps") or {}).values()
                                         if any(not is_trim_gap(x) and x != "market" for x in g))
            if final.get("binding_gaps") is not None else None,
            "rec_reacquire_skipped_fields": len(rec["reacquire_skipped_fields"])
            if rec.get("reacquire_skipped_fields") is not None else None,
            # re-acquisition billable searches by budget reason (technical_gap / trim_gap / other)
            "rec_reacquire_searches_by_reason": json.dumps(rec["reacquire_searches_by_reason"], sort_keys=True)
            if rec.get("reacquire_searches_by_reason") is not None else None,
            # binding-v3 year telemetry over admitted evidence
            "binding_year_status_counts": json.dumps(year["binding_year_status_counts"], sort_keys=True)
            if year.get("binding_year_status_counts") is not None else None,
            "ignored_year_contexts": json.dumps(year["ignored_year_contexts"], sort_keys=True, ensure_ascii=False)
            if year.get("ignored_year_contexts") is not None else None,
            # Binding Replay (when a binding_replay_summary.json exists for the run): fields ok with today's binding
            "replay_fields_ok_now": replay.get("fields_ok_now"),
            "replay_fields_ok_recorded": replay.get("fields_ok_recorded"),
            "replay_gap_counts": json.dumps(replay["gap_counts"], sort_keys=True, ensure_ascii=False)
            if replay.get("gap_counts") is not None else None,
            "total_model_calls": totals.get("model_calls"), "total_input_tokens": totals.get("input_tokens"),
            "total_output_tokens": totals.get("output_tokens"),
            "total_reasoning_tokens": totals.get("reasoning_tokens"),
            **{f"{p}_{k}": (phases.get(g) or {}).get(k) for p, g in (("research", "research"),
                                                                     ("sweep", "document_sweep"),
                                                                     ("recovery", "field_recovery"),
                                                                     ("finalizer", "finalization"))
               for k in ("model_calls", "input_tokens", "output_tokens", "reasoning_tokens", "truncated_calls",
                         "truncation_retries")},
            "rec_failed_attempts": rec.get("failed_attempts"), "rec_api_failure_stop": rec.get("api_failure_stop"),
            "rec_mode": rec.get("mode"), "rec_reacquire_new_useful_documents": rec.get("reacquire_new_useful_documents"),
            "rec_reacquire_admitted": rec.get("reacquire_admitted"),
            "research_tokens": (research.get("input_tokens") or 0) + (research.get("output_tokens") or 0)
            if research else None,
            "cost_usd": totals.get("cost_usd"), "wall_time_s": totals.get("wall_time_s")}


E2E_KEYS = ("final_ok", "final_conflicting", "final_unresolved_or_missing", "final_foreign_market_only",
            "final_variant_not_exact", "final_not_applicable", "rec_attempts", "rec_model_calls",
            "rec_billable_searches", "rec_fetches", "rec_fields_resolved", "rec_failed_attempts", "total_model_calls",
            "total_input_tokens", "total_output_tokens", "total_reasoning_tokens", "research_tokens", "acq_tokens",
            "acq_tokens_until_min_base", "acq_turn_reached_min_base", "acq_tool_blocked",
            "acq_official_urls_discovered", "cost_usd", "wall_time_s")


def aggregate(diagnostics: list[dict]) -> dict:
    """Benchmark statistics over every vehicle, the same statistics per configuration (`by_config`, e.g. legacy vs
    contract in one file) and the per-vehicle rows."""
    out = _aggregate_stats(diagnostics)
    groups: dict[str, list[dict]] = {}
    for d in diagnostics:
        groups.setdefault(config_key(d.get("configuration") or {}) if d.get("configuration") else "unknown",
                          []).append(d)
    out["by_config"] = {key: {"configuration": (items[0].get("configuration") or {}),
                              **{k: v for k, v in _aggregate_stats(items).items()
                                 if k not in ("schema", "generated_at")}}
                        for key, items in sorted(groups.items())}
    out["per_vehicle"] = [vehicle_row(d) for d in diagnostics]
    out["parser_gaps_by_field"] = parser_gaps_by_field(diagnostics)
    return out


def parser_gap_rows(diagnostics: list[dict]) -> list[dict]:
    """Every parser-gap row of the given vehicle runs, with its run_id / record_id (parser_gaps.jsonl)."""
    return [{"run_id": d.get("run_id"), "record_id": d.get("record_id"), **row}
            for d in diagnostics for row in ((d.get("parser_gaps") or {}).get("rows") or [])]


def parser_gaps_by_field(diagnostics: list[dict]) -> list[dict]:
    """The catalog-wide parser backlog: per field, across runs, the gap rows, the runs with a gap, the rows the sweep
    recovered and the top-3 matched aliases; most widespread first (runs with a gap, then rows)."""
    out: dict[str, dict] = {}
    aliases: dict[str, Counter] = {}
    for d in diagnostics:
        gaps = d.get("parser_gaps") or {}
        if not gaps.get("recorded"):
            continue
        for name, item in (gaps.get("fields") or {}).items():
            entry = out.setdefault(name, {"field": name, "gap_rows": 0, "runs_with_gap": 0, "recovered_by_sweep": 0})
            entry["gap_rows"] += item.get("label_hits_no_value", 0)
            entry["runs_with_gap"] += 1
            entry["recovered_by_sweep"] += item.get("recovered_by_sweep", 0)
        for row in gaps.get("rows") or []:
            aliases.setdefault(_norm_field(row.get("field")), Counter())[str(row.get("matched_alias"))] += 1
    for name, entry in out.items():
        entry["top_matched_aliases"] = [{"alias": a, "rows": n} for a, n in aliases.get(name, Counter()).most_common(3)]
    return sorted(out.values(), key=lambda e: (-e["runs_with_gap"], -e["gap_rows"], e["field"]))


def _aggregate_stats(diagnostics: list[dict]) -> dict:
    rows = [vehicle_row(d) for d in diagnostics]

    def nums(key):
        return [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]

    def stat(key):
        values = nums(key)
        return {"mean": _mean(values), "median": _median(values), "total": round(sum(values), 2) if values else None,
                "n": len(values)}

    swept = [r for r in rows if r.get("sweep_calls")]
    complete = [r for r in rows if not r.get("interrupted")]
    by_turn: dict[int, list[float]] = {}
    gain_by_turn: dict[int, list[float]] = {}
    for d in diagnostics:
        for t in d["acquisition"]["turns"]:
            if t.get("state_after"):
                by_turn.setdefault(t["turn_number"], []).append(t["state_after"]["scoped_coverage_pct"])
            if t.get("delta") and t["delta"].get("scoped_coverage_gain") is not None:
                gain_by_turn.setdefault(t["turn_number"], []).append(t["delta"]["scoped_coverage_gain"])
    entered, resolved = sum(nums("sweep_fields_entered")), sum(nums("sweep_fields_resolved"))
    accepted, rejected = sum(nums("sweep_evidence_accepted")), sum(nums("sweep_evidence_rejected"))
    model_calls = sum(nums("sweep_model_calls"))
    return {
        "schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vehicles": len(rows),
        "acquisition": {
            "turns": stat("acq_turns"), "searches_per_vehicle": stat("acq_search_calls"),
            "repeated_searches": stat("acq_repeated_searches"), "useful_documents": stat("acq_useful_documents"),
            "official_documents": stat("acq_official_documents"),
            "target_market_documents": stat("acq_target_market_documents"),
            "final_scoped_coverage_pct": stat("acq_final_scoped_coverage_pct"),
            "scoped_coverage_after_turn": {str(k): {"mean": _mean(v), "median": _median(v), "n": len(v)}
                                           for k, v in sorted(by_turn.items())},
            "coverage_gain_by_turn": {str(k): {"mean": _mean(v), "n": len(v)} for k, v in sorted(gain_by_turn.items())},
            "coverage_gain_per_turn": stat("acq_coverage_gain_per_turn"),
            "coverage_gain_per_search": stat("acq_coverage_gain_per_search"),
            "no_artifact_turns": stat("acq_no_artifact_turns"), "extended_turns": stat("acq_extended_turns"),
            "hard_max_turn_rate": round(sum(1 for r in rows if r["acq_hard_max"]) / len(rows), 3) if rows else None,
            "stop_reasons": dict(Counter(str(r["acq_stop_reason"]) for r in rows)),
            "latency_s": stat("acq_latency_s"), "fetch_failures": stat("acq_fetch_failures"),
        },
        "document_sweep": {
            "vehicles_swept": len(swept), "skipped": dict(Counter(str(r["sweep_skipped"]) for r in rows
                                                                  if r.get("sweep_skipped"))),
            "calls_per_vehicle": stat("sweep_calls"), "model_calls_per_vehicle": stat("sweep_model_calls"),
            "fields_entering": stat("sweep_fields_entered"), "fields_resolved": stat("sweep_fields_resolved"),
            "resolution_rate": round(resolved / entered, 3) if entered else None,
            "deterministic_misses_recovered": stat("sweep_deterministic_misses_recovered"),
            "value_mismatches": stat("sweep_value_mismatches"),
            "evidence_rejection_rate": round(rejected / (accepted + rejected), 3) if accepted + rejected else None,
            "input_tokens": stat("sweep_input_tokens"), "output_tokens": stat("sweep_output_tokens"),
            "latency_ms": stat("sweep_latency_ms"),
            "timeout_rate": round(sum(1 for r in swept if r.get("sweep_timeouts")) / len(swept), 3) if swept else None,
            "resolved_fields_per_model_call": round(resolved / model_calls, 2) if model_calls else None,
        },
        # end to end: complete runs only; interrupted / incomplete runs are reported separately, never averaged
        "end_to_end": {**{k: _stat_of(complete, k) for k in E2E_KEYS},
                       "under_acquired_exhausted": sum(1 for r in complete if r.get("acq_extension_exhausted")),
                       "runs": len(complete)},
        "interrupted": {"runs": len(rows) - len(complete),
                        "record_ids": [r["record_id"] for r in rows if r.get("interrupted")]},
    }


def _stat_of(rows: list[dict], key: str) -> dict:
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
    return {"mean": _mean(values), "median": _median(values), "total": round(sum(values), 2) if values else None,
            "n": len(values)}


def vehicle_dirs(runs_dir: Path | str, run_ids: Iterable[str] | None = None) -> list[Path]:
    root = Path(runs_dir)
    batches = [root / r for r in run_ids] if run_ids else sorted(p for p in root.iterdir()
                                                                if p.is_dir() and not p.name.startswith(("_", ".")))
    return [child for batch in batches if batch.is_dir() for child in sorted(batch.iterdir())
            if child.is_dir() and (child / "events.jsonl").is_file()]


def per_vehicle_csv(rows: list[dict]) -> str:
    """Export every column across mixed diagnostic versions; absent values stay blank."""
    if not rows:
        return ""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_benchmark(runs_dir: Path | str, run_ids: Iterable[str] | None = None, out_dir: Path | str | None = None,
                    *, rebuild: bool = False) -> dict:
    """Aggregate every vehicle of the given runs (default: all) into benchmark.json + per_vehicle.csv / .jsonl and
    parser_gaps.jsonl (one parser-gap row per line, with run_id / record_id)."""
    from .app_config import redact_obj
    from .storage.atomic import atomic_write_json, atomic_write_text

    diags = []
    for run_dir in vehicle_dirs(runs_dir, run_ids):
        diag = with_binding_replay(write_vehicle_diagnostics(run_dir), run_dir) if rebuild \
            else load_vehicle_diagnostics(run_dir)
        if diag:
            diags.append(diag)
    result = redact_obj(aggregate(diags))
    if out_dir is not None:
        out = Path(out_dir)
        atomic_write_json(out / "benchmark.json", result, durable=True)
        atomic_write_text(out / PARSER_GAPS_FILE, "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                                         for r in redact_obj(parser_gap_rows(diags))), durable=True)
        atomic_write_text(out / "per_vehicle.jsonl", "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                                             for r in result["per_vehicle"]), durable=True)
        if result["per_vehicle"]:
            atomic_write_text(out / "per_vehicle.csv", per_vehicle_csv(result["per_vehicle"]), durable=True)
    return result


def main(argv: list[str] | None = None) -> int:
    from .storage.paths import resolve_paths

    parser = argparse.ArgumentParser(description="Aggregate acquisition / document-sweep diagnostics of benchmark runs.")
    parser.add_argument("run_ids", nargs="*", help="run (batch) ids; default: every run")
    parser.add_argument("--runs-dir", default=str(resolve_paths().runs_dir))
    parser.add_argument("--out", default="", help="output folder (default: TRIPY_DATA_DIR/benchmarks/<UTC stamp>)")
    parser.add_argument("--rebuild", action="store_true", help="rebuild every vehicle's diagnostics.json from events")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else resolve_paths().data_dir / "benchmarks" / datetime.now(
        timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = write_benchmark(args.runs_dir, args.run_ids or None, out, rebuild=args.rebuild)
    print(json.dumps({k: v for k, v in result.items() if k != "per_vehicle"}, ensure_ascii=False, indent=1,
                     default=str))
    print(f"written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

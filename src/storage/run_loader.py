"""Load every run of a batch for the UI, including runs that never wrote result.json.

For each record_id declared in batch.json (plus any run folder found on disk):

* result.json exists           -> load it as is;
* only input.json/events.jsonl -> synthesize an incomplete run view from the
  events (status, tool calls, evidence, documents, usage confirmed by model
  responses, API attempts/timeouts, model responses, partial research bundle).

A synthesized view never contains structured fields: `output` is always None.
Nothing on disk is modified.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import bundle as bundle_mod
from ..pricing import UNKNOWN_USAGE_NOTE, run_cost
from . import trace
from .run_log import load_batch, read_events

RESULT_FILE = "result.json"
INCOMPLETE_BANNER = ("This run did not produce a final result.json. The research trace below was recovered "
                     "from events.jsonl.")
RUN_ARTIFACTS = ("result.json", "input.json", "events.jsonl")


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def run_record_ids(runs_root: Path | str, batch_id: str) -> list[str]:
    """record_ids declared in batch.json first (in order), then any other run folder on disk."""
    folder = Path(runs_root) / batch_id
    info = load_batch(runs_root, batch_id) or {}
    ids = [str(r) for r in info.get("record_ids") or []]
    if folder.is_dir():
        for child in sorted(folder.iterdir()):
            if child.is_dir() and child.name not in ids and any((child / a).is_file() for a in RUN_ARTIFACTS):
                ids.append(child.name)
    return ids


def _derive_status(events: list[dict]) -> tuple[str, str | None]:
    """(status, error) for a run without result.json."""
    finished = trace.last_event(events, "run_finished")
    if finished:
        status = finished.get("status") or "incomplete"
        # An interruption is never reported as an error; otherwise note the missing result file.
        return status, None if status == "interrupted" else "run_finished was logged but result.json is missing"
    interrupted = trace.last_event(events, "interrupted")
    if interrupted:
        return "interrupted", None   # a script-control interruption is not an error
    failed = trace.last_event(events, "finalization_failed")
    if failed:
        return "finalization_failed", failed.get("error")
    error = trace.last_event(events, "error")
    if error:
        return "research_failed", error.get("message")
    return "incomplete", None


def _interruption(events: list[dict]) -> dict:
    from ..agent import interruption_message

    event = trace.last_event(events, "interrupted")
    if not event:
        return {"interrupted": False, "interrupted_phase": None, "interruption_type": None,
                "interruption_message": None}
    return {"interrupted": True, "interrupted_phase": event.get("phase"),
            "interruption_type": event.get("exception"), "interruption_message": interruption_message(event.get("phase"))}


def _stop_reason(events: list[dict], max_steps: int | None) -> str | None:
    stopped = trace.last_event(events, "research_stopped")
    if stopped:
        return stopped.get("reason")
    for event in events:
        if (event.get("kind") == "model_response" and trace.phase_group(event.get("phase")) == "research"
                and not event.get("tool_calls")):
            return "model_finished"
    last_step = max((e.get("step") or 0 for e in events if e.get("kind") == "tool_call"), default=0)
    if max_steps and last_step >= max_steps:
        return "max_steps"
    return None


def end_state_note(events: list[dict], chat_path: str = "chat/completions") -> str | None:
    """A plain description of where a run without result.json stopped."""
    if not events:
        return "No events were logged."
    failures = trace.chat_failures_after_research(events, chat_path)
    last_step = trace.last_completed_tool_step(events)
    if failures:
        timeouts = sum(1 for e in failures if trace.is_timeout(e))
        return (f"Research reached step {last_step}. After the last tool result, {len(failures)} chat attempt(s) "
                f"failed ({timeouts} timed out) and no model response followed, so no final answer was produced.")
    last = events[-1]
    if last.get("kind") in ("tool_call", "tool_result"):
        return f"The log ends during research step {last.get('step')} ({last.get('kind')} {last.get('name')})."
    return f"The log ends with a `{last.get('kind')}` event."


def reconstruct_run(runs_root: Path | str, batch_id: str, record_id: str, *, cache=None,
                    batch_info: dict | None = None, ordinal: int | None = None) -> dict:
    """A result-shaped view of a run that has no result.json. Never invents structured fields."""
    run_dir = Path(runs_root) / batch_id / str(record_id)
    info = batch_info if batch_info is not None else (load_batch(runs_root, batch_id) or {})
    events = read_events(run_dir / "events.jsonl")
    payload = _read_json(run_dir / "input.json")
    started = trace.first_event(events, "run_started") or {}
    glm_config = started.get("glm_config") or info.get("glm_config") or {}
    chat_path = glm_config.get("chat_path") or "chat/completions"
    agent_cfg = started.get("agent_config") or info.get("agent_config") or {}
    research_model = started.get("research_model") or started.get("model") or info.get("model")
    finalizer_model = (started.get("finalizer_model") or glm_config.get("finalizer_model") or research_model)
    usage = trace.usage_by_phase(events)
    counters = dict(trace.tool_counters(events, chat_path))
    stats = trace.api_stats(events, chat_path)
    status, error = _derive_status(events)
    stop_reason = _stop_reason(events, agent_cfg.get("max_steps") or started.get("max_steps"))
    pricing = started.get("pricing") or info.get("pricing")
    pricing_finalizer = started.get("pricing_finalizer") or pricing
    cost, cost_details = run_cost(trace.sum_usage(usage["research"], usage["field_recovery"]), usage["finalization"],
                                  counters.get("search_api_calls", 0),
                                  pricing, pricing_finalizer, stats["unknown_usage_attempts"])
    t_start = trace.parse_ts(started.get("ts") or (events[0].get("ts") if events else None))
    t_last = trace.parse_ts(events[-1].get("ts")) if events else None
    documents_dir = run_dir / "documents"
    research_bundle = bundle_mod.build_research_bundle(
        events, payload, cache=cache, documents_dir=documents_dir,
        include_level3=agent_cfg.get("include_level3", True),
        max_chars=agent_cfg.get("finalizer_bundle_max_chars") or 60000, stop_reason=stop_reason)
    api_errors = [{k: v for k, v in e.items() if k not in ("seq", "kind")} for e in events
                  if e.get("kind") == "api_error"]
    final_fail = trace.last_event(events, "finalization_failed")
    finalization = None
    pending = trace.chat_failures_after_research(events, chat_path)
    if pending:
        # e.g. the GLM-5.3 baseline: the finalization call timed out and nothing came back.
        finalization = {"status": "failed_no_response", "inferred_from_events": True, "attempts": len(pending),
                        "timeouts": sum(1 for e in pending if trace.is_timeout(e)),
                        "model": pending[-1].get("model") or research_model,
                        "error": pending[-1].get("error")}
    elif final_fail:
        finalization = {"status": "failed", "inferred_from_events": True, "error": final_fail.get("error"),
                        "model": final_fail.get("model")}
    return {
        "record_id": str(record_id),
        "ordinal": ordinal,
        "batch_id": batch_id,
        "model": research_model,
        "research_model": research_model,
        "finalizer_model": finalizer_model,
        "glm_config": glm_config,
        "effective_config": {"glm": glm_config, "agent": agent_cfg,
                             "tools": started.get("tool_config") or info.get("tool_config") or {},
                             "prompt_version": started.get("prompt_version") or info.get("prompt_version")},
        "prompt_version": started.get("prompt_version") or info.get("prompt_version"),
        "status": status,
        "stop_reason": stop_reason,
        "research_steps": trace.research_steps(events),
        "last_successful_step": trace.last_completed_tool_step(events),
        "error": error,
        "api_error": (final_fail or {}).get("api_error"),
        "api_errors": api_errors,
        "finalizer_error": (final_fail or {}).get("error"),
        "started_at": started.get("ts") or (events[0].get("ts") if events else None),
        "latest_event_at": events[-1].get("ts") if events else None,
        "finished_at": None,
        "duration_s": round((t_last - t_start).total_seconds(), 2) if t_start and t_last else None,
        "output": None,
        "parse_note": "no_result_json",
        "raw_final_text": None,
        "last_model_content": trace.last_model_content(events),
        "model_responses": trace.model_responses(events),
        "evidence": trace.evidence_items(events),
        "documents": trace.document_ids(events),
        "tool_calls": trace.tool_call_rows(events),
        "counters": counters,
        "usage": trace.sum_usage(usage["research"], usage["field_recovery"], usage["finalization"]),
        "usage_research": usage["research"],
        "usage_field_recovery": usage["field_recovery"],
        "usage_finalizer": usage["finalization"],
        "requested_fields": started.get("requested_fields"),
        "target_market": started.get("target_market"),
        "field_recovery": trace.apply_current_states(trace.field_recovery_summary(events),
                                                     research_bundle.get("field_states")),
        "research_tracking": trace.reuse_counts(events),
        "api_stats": stats,
        "finalization": finalization,
        "search_api_calls": counters.get("search_api_calls", 0),
        "pricing": pricing,
        "pricing_finalizer": pricing_finalizer,
        "cost": cost,
        "cost_details": cost_details,
        "cost_note": UNKNOWN_USAGE_NOTE if stats["unknown_usage_attempts"] else None,
        "research_bundle": research_bundle,
        "documents_dir": str(documents_dir) if documents_dir.is_dir() else None,
        "level15_input_present": payload is not None,
        "end_state": end_state_note(events, chat_path),
        "event_count": len(events),
        "result_source": "events.jsonl",
        "synthesized": True,
        "banner": INCOMPLETE_BANNER,
        "partial": True,
        **_interruption(events),
        "recovered": False,
    }


def load_run(runs_root: Path | str, batch_id: str, record_id: str, *, cache=None, batch_info: dict | None = None,
             ordinal: int | None = None) -> dict | None:
    run_dir = Path(runs_root) / batch_id / str(record_id)
    result = _read_json(run_dir / RESULT_FILE)
    if result is not None:
        result.setdefault("record_id", str(record_id))
        result.setdefault("batch_id", batch_id)
        result.setdefault("result_source", "result.json")
        return result
    if (run_dir / "input.json").is_file() or (run_dir / "events.jsonl").is_file():
        return reconstruct_run(runs_root, batch_id, record_id, cache=cache, batch_info=batch_info, ordinal=ordinal)
    return None


def load_runs(runs_root: Path | str, batch_id: str, *, cache=None, metrics_fn=None) -> list[dict]:
    """Every started run of a batch, in batch order. `metrics_fn(result)` fills missing `metrics`."""
    info = load_batch(runs_root, batch_id) or {}
    runs = []
    for index, record_id in enumerate(run_record_ids(runs_root, batch_id)):
        run = load_run(runs_root, batch_id, record_id, cache=cache, batch_info=info)
        if run is None:
            continue
        run["_order"] = index
        if metrics_fn is not None and (run.get("synthesized") or "metrics" not in run):
            run["metrics"] = metrics_fn(run)
        runs.append(run)
    runs.sort(key=lambda r: r["_order"])
    return runs


def run_document_metas(result: dict, runs_root: Path | str, cache=None) -> list[dict]:
    """Metadata for every document a run touched: shared cache, else the run's exported copy, else events."""
    run_dir = Path(runs_root) / str(result.get("batch_id", "")) / str(result.get("record_id", ""))
    event_meta = trace.document_event_meta(read_events(run_dir / "events.jsonl")) if result.get("synthesized") else {}
    return [bundle_mod.document_meta(doc_id, cache, run_dir / "documents", event_meta)
            for doc_id in result.get("documents") or []]


def document_text(document_id: str, result: dict | None, runs_root: Path | str, cache=None) -> str:
    text = cache.read_text(document_id) if cache is not None else ""
    if not text and result is not None:
        path = (Path(runs_root) / str(result.get("batch_id", "")) / str(result.get("record_id", ""))
                / "documents" / document_id / "text.txt")
        if path.is_file():
            text = path.read_text("utf-8", errors="replace")
    return text

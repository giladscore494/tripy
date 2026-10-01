"""Finalize an existing saved research run without repeating the research.

    python -m src.cli --finalize-existing --batch-id <batch> --record-id <id>

Reads the run's input.json and events.jsonl (and result.json if one exists),
builds the compact research bundle from the logged evidence, tool results and
cached documents, and makes ONLY the finalization call. No search, no fetch.

History is preserved: events are appended to events.jsonl (sequence numbers
continue), a pre-existing result.json is first copied to
result.pre-recovery-<stamp>.json, and the exact finalizer request is stored under
recovery/<stamp>/. The new result.json is marked `recovered: true`.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .agent import (AgentConfig, ModelCaller, effective_glm_config, finalizer_messages, finalizer_model_of,
                    run_finalization)
from .bundle import build_research_bundle
from .pricing import UNKNOWN_USAGE_NOTE, default_pricing, run_cost
from .storage import trace
from .storage.run_loader import reconstruct_run
from .storage.run_log import RunLog, load_batch, read_events, utc_now
from .tools import ToolConfig


class RecoveryError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _base_record(runs_dir: Path, batch_id: str, record_id: str, cache, batch_info: dict) -> tuple[dict, dict | None]:
    prior = _read_json(runs_dir / batch_id / record_id / "result.json")
    if prior is not None:
        return prior, prior
    return reconstruct_run(runs_dir, batch_id, record_id, cache=cache, batch_info=batch_info), None


def plan_recovery(runs_dir: Path | str, batch_id: str, record_id: str, *, cache=None,
                  config: AgentConfig | None = None) -> dict:
    """Dry run: what the recovery finalizer would receive. Writes nothing, calls nothing."""
    runs_dir = Path(runs_dir)
    run_dir = runs_dir / batch_id / str(record_id)
    if not (run_dir / "events.jsonl").is_file():
        raise RecoveryError(f"No events.jsonl in {run_dir}")
    info = load_batch(runs_dir, batch_id) or {}
    base, prior = _base_record(runs_dir, batch_id, str(record_id), cache, info)
    config = config or AgentConfig()
    events = read_events(run_dir / "events.jsonl")
    payload = _read_json(run_dir / "input.json") or {}
    bundle = build_research_bundle(events, payload, cache=cache, documents_dir=run_dir / "documents",
                                   include_level3=config.include_level3,
                                   max_chars=config.finalizer_bundle_max_chars, stop_reason=base.get("stop_reason"))
    messages = finalizer_messages(bundle)
    return {"run_dir": str(run_dir), "prior_status": base.get("status"), "prior_result_json": prior is not None,
            "prior_has_output": base.get("output") is not None, "stop_reason": base.get("stop_reason"),
            "events": len(events), "evidence_items": len(bundle["evidence"]),
            "documents": len(bundle["documents"]), "excerpts": len(bundle["document_excerpts"]),
            "bundle_chars": bundle["bundle_chars"],
            "finalizer_input_chars": sum(len(m["content"]) for m in messages),
            "research_events_sent_to_finalizer": 0}


def finalize_existing_run(runs_dir: Path | str, batch_id: str, record_id: str, *, client, cache,
                          config: AgentConfig, tool_config: ToolConfig | None = None,
                          pricing_finalizer: dict | None = None, force: bool = False,
                          listener: Callable[[str, dict], None] | None = None,
                          metrics_fn: Callable[[dict], dict] | None = None) -> dict:
    """Run only the compact finalization for a saved run and write a recovered result.json.

    Makes exactly one finalization call (plus at most one JSON-repair call). Never
    calls search or fetch tools. Raises RecoveryError for missing artifacts, or when
    the run already has structured output and `force` is False.
    """
    runs_dir = Path(runs_dir)
    record_id = str(record_id)
    run_dir = runs_dir / batch_id / record_id
    if not (run_dir / "events.jsonl").is_file():
        raise RecoveryError(f"No events.jsonl in {run_dir}")
    if not (run_dir / "input.json").is_file():
        raise RecoveryError(f"No input.json (Level 1.5 payload) in {run_dir}")
    info = load_batch(runs_dir, batch_id) or {}
    base, prior = _base_record(runs_dir, batch_id, record_id, cache, info)
    if base.get("output") is not None and not force:
        raise RecoveryError(f"{run_dir} already has structured output (status {base.get('status')}); "
                            "pass force=True / --force to finalize again")
    payload = _read_json(run_dir / "input.json") or {}
    tool_config = tool_config or ToolConfig()
    stamp = _stamp()
    recovery_dir = run_dir / "recovery" / stamp
    finalizer_model = finalizer_model_of(client)
    research_pricing = base.get("pricing") or info.get("pricing")
    if pricing_finalizer is None:
        same = finalizer_model == (base.get("research_model") or base.get("model"))
        pricing_finalizer = research_pricing if same and research_pricing else default_pricing(finalizer_model)

    log = RunLog(runs_dir, batch_id, record_id, listener=listener)  # appends; sequence numbers continue
    preserved = None
    if prior is not None:
        preserved = run_dir / f"result.pre-recovery-{stamp}.json"
        shutil.copy2(run_dir / "result.json", preserved)

    # Make the run self-contained: copy touched documents that were never exported (an interrupted run
    # never reached the export step). Existing files are left as they are.
    documents_dir = run_dir / "documents"
    for doc_id in base.get("documents") or []:
        if not (documents_dir / doc_id).exists():
            try:
                cache.export(doc_id, documents_dir)
            except OSError:
                pass

    phase = "recovery_finalization"
    glm_config = effective_glm_config(client, config, tool_config)
    log.event("recovery_started", finalizer_model=finalizer_model, prior_status=base.get("status"),
              prior_result_source=base.get("result_source"), prior_result_preserved_as=str(preserved) if preserved else None,
              stop_reason=base.get("stop_reason"), glm_config=glm_config, recovery_dir=str(recovery_dir),
              note="finalization only; no search or fetch is performed")
    api_errors: list[dict] = []

    def api_hook(kind: str, **data) -> None:
        if kind == "api_error":
            api_errors.append(dict(data))
        log.event(kind, phase=phase, **data)

    previous_hook = getattr(client, "hook", None)
    client.hook = api_hook
    caller = ModelCaller(client, log, config)
    try:
        fin = run_finalization(caller, run_log=log, payload=payload, config=config, cache=cache,
                               documents_dir=documents_dir, stop_reason=base.get("stop_reason"), phase=phase,
                               model=finalizer_model, request_path=recovery_dir / "finalizer_request.json")
    except BaseException as exc:  # Ctrl+C: log it; the prior state on disk is untouched
        log.event("interrupted", phase=phase, exception=type(exc).__name__)
        client.hook = previous_hook
        raise
    client.hook = previous_hook
    (recovery_dir / "research_bundle.json").write_text(json.dumps(fin["bundle"], ensure_ascii=False, indent=1),
                                                       "utf-8")

    events = read_events(run_dir / "events.jsonl")
    chat_path = glm_config.get("chat_path") or "chat/completions"
    stats = trace.api_stats(events, chat_path)
    usage_research = base.get("usage_research") or trace.usage_by_phase(
        [e for e in events if e.get("kind") == "model_response" and e.get("phase") != phase])["research"]
    usage_finalizer = trace.sum_usage(base.get("usage_finalizer"), caller.usage["finalization"])
    search_calls = base.get("search_api_calls") or 0
    cost, cost_details = run_cost(usage_research, usage_finalizer, search_calls, research_pricing,
                                  pricing_finalizer, stats["unknown_usage_attempts"])
    if fin["error"]:
        status = "finalization_failed"
    elif fin["output"] is None:
        status = "completed_unparsed"
    else:
        status = "recovered_finalized"
    recovery = {
        "recovered_at": utc_now(), "stamp": stamp, "finalizer_model": finalizer_model,
        "prior_status": base.get("status"), "prior_result_source": base.get("result_source"),
        "prior_result_preserved_as": str(preserved) if preserved else None,
        "recovery_dir": str(recovery_dir), "status": fin["info"]["status"], "error": fin["error"],
        "usage": caller.usage["finalization"], "finalization": fin["info"],
        "glm_config": glm_config, "searches_performed": 0, "fetches_performed": 0,
    }
    result = {k: v for k, v in base.items() if k not in ("metrics", "banner", "_order")}
    result.update({
        "status": status,
        "output": fin["output"],
        "parse_note": f"recovery:{fin['parse_note']}",
        "raw_final_text": fin["text"],
        "error": fin["error"],
        "api_error": fin["api_error"],
        "finalizer_error": fin["error"],
        "finalizer_model": finalizer_model,
        "finalization": fin["info"],
        "usage_research": usage_research,
        "usage_finalizer": usage_finalizer,
        "usage": trace.sum_usage(usage_research, usage_finalizer),
        "api_stats": stats,
        "api_errors": list(base.get("api_errors") or []) + api_errors,
        "pricing": research_pricing,
        "pricing_finalizer": pricing_finalizer,
        "cost": cost,
        "cost_details": cost_details,
        "cost_note": UNKNOWN_USAGE_NOTE if stats["unknown_usage_attempts"] else None,
        "research_bundle": fin["bundle"],
        "documents_dir": str(documents_dir) if documents_dir.is_dir() else base.get("documents_dir"),
        "recovered": True,
        "recovery": recovery,
        "result_source": "result.json",
        "synthesized": False,
        "finished_at": utc_now(),
    })
    if metrics_fn is not None:
        result["metrics"] = metrics_fn(result)
    log.write_result(result)
    log.event("recovery_finished", status=status, finalizer_model=finalizer_model, parse_note=result["parse_note"],
              usage=caller.usage["finalization"], cost=cost)
    return result

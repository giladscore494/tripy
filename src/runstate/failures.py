"""User-facing failure explanations: where a run stopped, why (in plain words), what was preserved and which
recovery is safe. Technical detail is returned separately (and redacted) for an expander; stack traces never
reach the normal UI (they go to the server log).

Recovery semantics (no correctness risk):

* "finalize": the engine's own `finalize_existing_run` (src/recovery.py): builds the compact bundle from the saved
  events and makes ONLY the finalizer call. No search, fetch, harvest, sweep or recovery is repeated; history is
  kept (events appended, the prior result.json preserved as result.pre-recovery-<stamp>.json).
* "restart": a brand-new run of the same target (new run_id). The old run stays untouched in history.

Resuming a run in the middle of Document Sweep or Tail Recovery is NOT offered: those phases keep working state
in memory (tool session, novelty tracking, retry queue, harvester) that the engine does not checkpoint, so a
mid-phase resume could not reproduce what an uninterrupted run would have done.
"""

from __future__ import annotations

import json

from ..app_config import redact
from .model import STAGE_LABELS

SUCCESS = ("completed", "max_steps_finalized", "no_new_research_finalized", "acquisition_sufficient_finalized",
           "under_acquired_finalized", "recovered_finalized")
FINALIZABLE = ("finalization_failed", "finalization_pending", "completed_unparsed", "interrupted", "incomplete",
               "research_failed")


def classify_reason(error: str | None, api_error: dict | None = None, engine_status: str | None = None) -> tuple[str, str]:
    """(code, plain-language reason). Never contains a secret (callers redact technical text separately)."""
    api_error = api_error or {}
    text = f"{error or ''} {api_error.get('message') or ''}".lower()
    status = api_error.get("status")
    if engine_status == "completed_unparsed" or "finalization_unparsed" in text:
        return "malformed_response", "The model returned a malformed final answer (not valid JSON)."
    if "checkpoint_write_failed" in text or "no space left" in text or "read-only file system" in text:
        return "storage", "The run could not write to storage (check the data volume)."
    if "level 1.5" in text or "level15" in text:
        return "level15", "The vehicle's Level 1.5 data could not be loaded."
    if api_error.get("timeout") or "timed out" in text or "timeout" in text:
        return "provider_timeout", "Provider request timed out."
    if status == 429 or " 429" in text or "rate limit" in text:
        return "rate_limited", "The provider rate-limited the request (HTTP 429)."
    if status in (401, 403) or " 401" in text or " 403" in text or "unauthorized" in text:
        return "auth", "The provider rejected the credentials (HTTP 401/403). Check GLM_API_KEY."
    if isinstance(status, int) and status >= 500:
        return "provider_error", f"The model provider returned a server error (HTTP {status})."
    if "web_search" in text or "search" in str(api_error.get("endpoint") or "").lower():
        return "search_failure", "The web search request failed."
    if "connectionerror" in text or "connection" in text or "name or service" in text:
        return "connection", "Could not connect to the model provider."
    if "json" in text and ("decode" in text or "parse" in text or "malformed" in text):
        return "malformed_response", "The provider returned a malformed response."
    if isinstance(status, int) and status >= 400:
        return "provider_error", f"The provider rejected the request (HTTP {status})."
    return "unexpected", "An unexpected error stopped the run."


def _stage_of(result: dict, pipeline_view: dict | None) -> str | None:
    engine = result.get("status")
    if result.get("interrupted_phase"):
        from .model import ENGINE_PHASE_TO_STAGE
        return ENGINE_PHASE_TO_STAGE.get(result["interrupted_phase"], "acquisition")
    if engine in ("finalization_failed", "finalization_pending", "completed_unparsed"):
        return "finalization"
    if engine == "research_failed":
        return "acquisition"
    if pipeline_view:
        failed = [s for s in pipeline_view.get("stages") or [] if s["state"] in ("failed", "interrupted", "running")]
        if failed:
            return failed[-1]["key"]
    return None


def explain(result: dict | None, pipeline_view: dict | None = None, *, run_status: str | None = None,
            run_error: dict | None = None, can_finalize: bool = True) -> dict | None:
    """None for a successful vehicle run; otherwise everything the failure card needs."""
    result = result or {}
    engine = result.get("status") or (pipeline_view or {}).get("engine_status")
    if engine in SUCCESS and result.get("output") is not None:
        return None
    if engine is None and run_error is None and run_status not in ("FAILED", "INTERRUPTED", "CANCELLED"):
        return None
    stage = _stage_of(result, pipeline_view) or (run_error or {}).get("stage")
    stage_label = STAGE_LABELS.get(stage or "", "the run")
    error = result.get("error") or (run_error or {}).get("message")
    api_error = result.get("api_error") or (run_error or {}).get("api_error")
    if engine == "interrupted" or run_status in ("INTERRUPTED", "CANCELLED") and engine in (None, "interrupted",
                                                                                           "finalization_pending",
                                                                                           "incomplete"):
        if run_status == "CANCELLED":
            code, reason = "cancelled", "The run was stopped by a user."
        else:
            code, reason = "server_stopped", ("The server stopped while the run was active (redeploy, restart or "
                                             "crash).")
        title = f"Research stopped during {stage_label}."
    elif engine == "finalization_pending":
        code, reason = "server_stopped", "The server stopped while the finalizer was running."
        title = "Research finished, but the final result was not built."
    else:
        code, reason = classify_reason(error, api_error, engine)
        title = f"Research stopped during {stage_label}." if stage else "Research did not complete."
    has_research = bool(result.get("evidence") or result.get("documents") or result.get("tool_calls")
                        or ((pipeline_view or {}).get("counters") or {}).get("sources"))
    preserved = None
    if has_research:
        preserved = ("Your acquired sources, harvested candidates and admitted evidence were preserved."
                     if stage in ("sweep", "recovery", "finalization", "harvest")
                     else "Everything acquired before the stop (sources, evidence, events) was preserved.")
    actions = []
    if can_finalize and engine in FINALIZABLE and result.get("output") is None and has_research:
        actions.append("finalize")
    actions.append("restart")
    technical = {k: v for k, v in {
        "engine_status": engine, "stop_reason": result.get("stop_reason"), "error": error,
        "api_error": {k: v for k, v in (api_error or {}).items() if k not in ("headers",)} or None,
        "interruption_type": result.get("interruption_type"), "run_error": run_error,
    }.items() if v}
    return {"code": code, "title": title, "stage": stage, "stage_label": stage_label, "reason": reason,
            "preserved": preserved, "actions": actions,
            "finalize_label": ("Retry finalization" if stage == "finalization"
                               else "Finalize from preserved research"),
            "finalize_note": ("Builds the final result from the saved research only: no new searches or fetches. "
                              + ("" if stage in ("finalization", None) else
                                 f"Phases after {stage_label} that did not run are not repeated or resumed.")),
            "technical": redact(json.dumps(technical, ensure_ascii=False, indent=1, default=str))}

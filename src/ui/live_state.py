"""In-memory state of a running batch, built ONLY from events the runs already emit.

    worker thread:  RunLog listener -> queue.put({"record_id", "kind", "event"})   (no Streamlit calls)
    main thread:    BatchLiveState.drain(queue) -> per-vehicle VehicleLive.apply(kind, event)
                    -> pure view models (Hebrew) -> src/ui/live_dashboard.py renders them

No function here makes a model call, a search or a fetch: progress is computed with the shared field
evaluator (current_evaluation) over the vehicle's own evidence / declaration events; "why" lines come
from orchestration metadata (phase, failure reason, tool); reasoning is shown only when the provider
returned reasoning_content in a model response. This module does not import Streamlit.
"""

from __future__ import annotations

import json
import queue as queue_mod
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

from ..field_recovery import current_evaluation
from ..fields import field_display_name, group_display_name
from ..pricing import compute_cost
from . import labels_he as he

EVAL_KINDS = ("run_started", "evidence", "field_status", "model_response")
OPEN_STATES = ("unresolved", "missing", "conflicting", "foreign_market_only", "variant_not_exact", "weak_provenance")
DONE_STATES = ("ok", "not_applicable")
TERMINAL_OPS = ("completed", "failed", "interrupted")
FAILED_RUN_STATUSES = ("research_failed", "finalization_failed", "error")


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _domain(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


# --- the detailed (raw) feed: kept as before, now secondary ------------------------------------------

def feed_line(kind: str, event: dict) -> str | None:
    """One line of the detailed activity log (debug view; canonical identifiers allowed)."""
    if kind == "tool_call":
        call = f"{event.get('name')}({_short(event.get('arguments'), 140)})"
        if event.get("field"):
            return f"🔧 {event['field']} · attempt {event.get('attempt')} · step {event.get('step')}\n   {call}"
        return f"🔧 step {event.get('step')} · {call}"
    if kind == "tool_reused":
        return (f"   ↺ reused result of step {event.get('original_step')} (not executed again): "
                f"{event.get('name')}({_short(event.get('arguments'), 100)})")
    if kind == "tool_blocked":
        return f"   ⛔ {event.get('name')} refused in {event.get('phase')} (not executed)"
    if kind == "field_recovery_started":
        return (f"🎯 field recovery · {event.get('field')} · attempt {event.get('attempt')}/"
                f"{event.get('max_attempts')} · reason {event.get('failure_reason')}")
    if kind == "field_recovery_finished":
        return f"   ↳ {event.get('field')} attempt {event.get('attempt')}: {event.get('state_before')} → {event.get('state_after')}"
    if kind == "cluster_recovery_started":
        return (f"🎯 cluster recovery · {event.get('cluster')} · attempt {event.get('attempt')}/"
                f"{event.get('max_attempts')} · {event.get('mode')} · {len(event.get('fields') or [])} open field(s) · "
                f"search budget {event.get('search_budget')}")
    if kind == "cluster_turn_novelty":
        return (f"   ↳ turn {event.get('turn')} novelty: {', '.join(event.get('novelty') or []) or 'none'}")
    if kind == "cluster_recovery_no_novelty_stop":
        return f"   ⏹ {event.get('cluster')} stopped: no novelty after turn {event.get('after_turn')}"
    if kind == "cluster_recovery_budget_extended":
        return f"   ➕ {event.get('cluster')}: turn {event.get('turn')} granted after real novelty"
    if kind == "cluster_recovery_skipped":
        return f"   ⏭ {event.get('cluster')} attempt skipped ({event.get('reason')})"
    if kind == "field_recovery_queue_resolved_indirectly":
        return (f"✅ {event.get('field')} resolved while recovering {event.get('resolved_during_field')}; "
                "its own retry is skipped")
    if kind == "tool_result":
        result = event.get("result") or {}
        if isinstance(result, dict) and result.get("error"):
            return f"   ↳ error: {result.get('error')} {_short(result.get('message', ''), 120)}"
        if isinstance(result, dict) and "results" in result:
            return f"   ↳ {len(result['results'])} results{' (cache)' if result.get('cache_hit') else ''}"
        if isinstance(result, dict) and result.get("document_id"):
            hit = " (cache)" if result.get("cache_hit") else ""
            return (f"   ↳ {result['document_id']} {result.get('status', '')} "
                    f"{_short(result.get('final_url') or result.get('url') or '', 90)}{hit}")
        return None
    if kind == "duplicate_work":
        return f"   ↺ repeated work: {_short(event.get('note'), 120)}"
    if kind == "evidence":
        ev = event.get("evidence", {})
        scope = " · ".join(str(ev[k]) for k in ("variant_match", "market", "source_authority") if ev.get(k))
        return f"📌 {ev.get('evidence_id')} {ev.get('field')} = {_short(ev.get('value'), 60)}" + (f" ({scope})" if scope
                                                                                                 else "")
    if kind == "evidence_rejected":
        return (f"⛔ evidence rejected: {event.get('field')} = {_short(event.get('value'), 50)} "
                f"({', '.join(event.get('reasons') or [])})")
    if kind == "evidence_reused":
        return f"↺ evidence reused: {event.get('evidence_id')} {event.get('field')} = {_short(event.get('value'), 60)}"
    if kind == "field_recovery_early_resolved":
        return (f"✅ {event.get('field')} resolved by stored evidence after turn {event.get('after_turn')}; "
                "attempt ended without another model call")
    if kind == "model_response":
        usage = event.get("usage") or {}
        calls = event.get("tool_calls") or []
        tokens = usage.get("total_tokens", "?")
        if event.get("field"):
            return (f"🧠 {event.get('phase', 'field_recovery')} · {event['field']} · attempt {event.get('attempt')}/"
                    f"{event.get('max_attempts')} · turn {event.get('turn')}/{event.get('turn_budget')} · "
                    f"{len(calls)} tool call(s) · tokens {tokens}")
        return f"🧠 {event.get('phase', 'research')} turn · {len(calls)} tool call(s) · tokens {tokens}"
    if kind == "research_stopped":
        return f"🛑 research stopped: {event.get('reason')} after {event.get('steps')} step(s)"
    if kind == "candidates_harvested":
        return f"🧲 harvested {event.get('candidate_count')} candidate(s) from {event.get('document_id')}"
    if kind == "document_sweep_started":
        return f"🔎 document sweep · {event.get('candidates_presented')} candidate(s) · {len(event.get('fields_to_review') or [])} field(s)"
    if kind == "document_sweep_finished":
        return f"   ↳ document sweep resolved {event.get('unique_fields_resolved')} field(s)"
    if kind == "candidate_missed_by_deterministic_harvest":
        return f"🧩 sweep found a fact the parser missed: {event.get('field')} = {_short(event.get('value'), 60)}"
    if kind == "finalization_started":
        return f"🧾 finalization with {event.get('model')} · bundle {event.get('finalizer_input_chars')} chars"
    if kind == "api_error":
        return (f"⚠️ API {event.get('request_kind') or event.get('endpoint')} attempt "
                f"{event.get('attempt')}/{event.get('max_attempts', '?')}: "
                f"{event.get('status') or event.get('error')} {_short(event.get('body') or '', 160)}")
    if kind in ("error", "finalization_failed"):
        return f"❌ {event.get('message') or event.get('error')}"
    if kind == "interrupted":
        return f"⏹️ interrupted during {event.get('phase')}"
    return None


# --- one vehicle -----------------------------------------------------------------------------------------

class VehicleLive:
    def __init__(self, record_id: str, title: str, *, pricing: dict | None = None,
                 pricing_finalizer: dict | None = None, log_lines: int = 200):
        self.record_id, self.title = record_id, title
        self.pricing, self.pricing_finalizer = pricing, pricing_finalizer
        self.op = "queued"
        self.phase: str | None = None
        self.model: str | None = None
        self.research_model: str | None = None
        self.finalizer_model: str | None = None
        self.field: str | None = None
        self.attempt: int | None = None
        self.max_attempts: int | None = None
        self.failure_reason: str | None = None
        self.action: tuple[str, str] | None = None      # (tool name, detail)
        self.reasoning: str | None = None
        self.reasoning_available = False
        self.reasoning_context: str | None = None
        self.specs: list[dict] = []
        self.target_market: str | None = None
        self.eval_events: list[dict] = []
        self._evaluation: list[dict] | None = None
        self.recovery_budget: int | None = None
        self.recovery_turns = 0
        self.recovery_attempts: dict[str, int] = {}
        self.cluster: str | None = None            # clustered tail recovery: the cluster being recovered
        self.cluster_fields: list[str] = []
        self.cluster_mode: str | None = None
        self.resolved_indirectly: dict[str, str] = {}
        self.sweep_resolved: set[str] = set()
        self.sweep_info: dict = {}
        self.harvest: dict = {"documents": 0, "fields": set()}
        self.documents: dict[str, str] = {}
        self.model_calls = 0
        self.searches = 0
        self.evidence_rejected = 0
        self.http_429 = 0
        self.timeouts = 0
        self.unknown_usage = 0
        self.usage_by_model: dict[str, dict] = {}
        self.search_api_calls = 0
        self.status: str | None = None
        self.started = None
        self.finished = None
        self.updated = time.monotonic()
        self.lines: deque[str] = deque(maxlen=log_lines)
        self.version = 0

    # -- event intake ----------------------------------------------------------------------------------

    def apply(self, kind: str, event: dict) -> None:
        self.version += 1
        self.updated = time.monotonic()
        line = feed_line(kind, event)
        if line:
            self.lines.append(line)
        if event.get("phase") and kind not in ("tool_result", "candidates_harvested"):
            self.phase = event["phase"] if event["phase"] in he.PHASE_LABELS_HE else self.phase
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(event)
        if kind in EVAL_KINDS and (kind != "model_response" or (event.get("phase") in (None, "research")
                                                               and not event.get("tool_calls"))):
            self.eval_events.append(event)   # only what the shared evaluator reads
            self._evaluation = None

    def _on_run_started(self, e: dict) -> None:
        self.started = self.started or time.monotonic()
        self.op, self.phase = "evaluating", "research"
        self.specs = list(e.get("requested_field_specs") or [{"name": n, "description": d, "applicable": True}
                                                             for n, d in (e.get("requested_fields") or {}).items()])
        self.target_market = e.get("target_market")
        self.research_model = e.get("research_model") or e.get("model")
        self.finalizer_model = e.get("finalizer_model") or self.research_model
        self.model = self.research_model
        agent = e.get("agent_config") or {}
        if agent.get("field_recovery_enabled", True):
            self.recovery_budget = agent.get("field_recovery_max_total_steps")
        if e.get("pricing") is not None:
            self.pricing = e["pricing"]
        if e.get("pricing_finalizer") is not None:
            self.pricing_finalizer = e["pricing_finalizer"]

    def _on_model_queue_wait_started(self, e: dict) -> None:
        self.op, self.model = "waiting_model", e.get("model") or self.model

    def _on_model_slot_acquired(self, e: dict) -> None:
        self.op = "model_inflight"

    def _on_model_request_started(self, e: dict) -> None:
        self.op, self.model = "model_inflight", e.get("model") or self.model
        self.reasoning, self.reasoning_available = None, False
        self.action = None       # the previous turn's tool action is over

    def _on_model_request_finished(self, e: dict) -> None:
        self.op = "evaluating"   # the response is back (or failed); the model is no longer thinking

    def _on_model_response(self, e: dict) -> None:
        self.model_calls += 1
        self.model = e.get("model") or self.model
        usage = self.usage_by_model.setdefault(self.model or "", {"prompt_tokens": 0, "completion_tokens": 0})
        for key in usage:
            usage[key] += int((e.get("usage") or {}).get(key) or 0)
        if e.get("phase") == "field_recovery":
            self.recovery_turns += 1
        reasoning = e.get("reasoning_content")
        self.reasoning_available = bool(isinstance(reasoning, str) and reasoning.strip())
        self.reasoning = reasoning if self.reasoning_available else None
        bits = [he.phase_label(e.get("phase"))]
        if e.get("cluster"):
            bits.append(f"קבוצה: {he.cluster_label(e['cluster'])}")
        elif e.get("field"):
            bits.append(field_display_name(e["field"], self._spec(e["field"])))
        if e.get("turn"):
            bits.append(f"תור {e['turn']}")
        self.reasoning_context = " · ".join(b for b in bits if b and b != "—")
        self.op = "evaluating"

    def _on_search_queue_wait_started(self, e: dict) -> None:
        self.op = "waiting_search"

    def _on_search_request_started(self, e: dict) -> None:
        self.op = "searching"

    def _on_api_call(self, e: dict) -> None:
        if e.get("request_kind") == "search":
            self.search_api_calls += 1

    def _on_api_error(self, e: dict) -> None:
        if e.get("status") == 429:
            self.http_429 += 1
        if e.get("timeout"):
            self.timeouts += 1
        if e.get("usage_unknown"):
            self.unknown_usage += 1

    def _on_tool_call(self, e: dict) -> None:
        name = e.get("name") or ""
        args = _args(e.get("arguments"))
        detail = args.get("query") or args.get("url") or ""
        doc = args.get("document_id") or args.get("key")
        if not detail and doc:
            detail = self.documents.get(doc, doc)
        if name in ("store_evidence", "report_field_status") and args.get("field"):
            label = field_display_name(args["field"], self._spec(args["field"]))
            if name == "store_evidence":
                detail = f"{label} = {_short(args.get('value'), 40)}"
            else:
                detail = f"{label} · {he.declaration_label(args.get('status'))}"
        self.action = (name, _short(detail, 140))
        self.op = he.TOOL_OP_STATE.get(name, "evaluating")
        if name in ("search_web", "search_official_domains"):
            self.searches += 1

    def _on_evidence_rejected(self, e: dict) -> None:
        self.evidence_rejected += 1

    def _on_tool_result(self, e: dict) -> None:
        result = e.get("result") or {}
        if isinstance(result, dict) and result.get("document_id"):
            self.documents[result["document_id"]] = result.get("final_url") or result.get("url") or result["document_id"]

    def _on_document(self, e: dict) -> None:
        doc = e.get("document") or {}
        if doc.get("document_id"):
            self.documents[doc["document_id"]] = doc.get("final_url") or doc.get("url") or doc["document_id"]

    def _on_candidates_harvested(self, e: dict) -> None:
        self.harvest["documents"] += 1
        self.harvest["fields"] |= set(e.get("fields") or [])

    def _on_deterministic_harvest_summary(self, e: dict) -> None:
        self.action = None
        self.phase, self.op, self.field = "deterministic_harvest", "extracting", None
        self.harvest["summary"] = e

    def _on_document_sweep_started(self, e: dict) -> None:
        self.action = None
        self.phase, self.field = "document_sweep", None
        self.sweep_info = {"fields_to_review": len(e.get("fields_to_review") or []),
                           "candidates": e.get("candidates_presented")}

    def _on_document_sweep_finished(self, e: dict) -> None:
        self.sweep_resolved = set(e.get("fields_resolved") or [])
        self.sweep_info["resolved"] = e.get("unique_fields_resolved")

    def _on_field_evaluation(self, e: dict) -> None:
        self.op = "evaluating"
        if e.get("stage") == "primary":
            self.phase = "field_detection"

    def _on_field_recovery_started(self, e: dict) -> None:
        self.action = None
        self.phase = "field_recovery"
        self.field, self.attempt, self.max_attempts = e.get("field"), e.get("attempt"), e.get("max_attempts")
        self.failure_reason = e.get("failure_reason")
        self.recovery_attempts[self.field] = self.recovery_attempts.get(self.field, 0) + 1

    def _on_cluster_recovery_started(self, e: dict) -> None:
        self.action = None
        self.phase = "field_recovery"
        self.field, self.failure_reason = None, None
        self.cluster, self.cluster_fields = e.get("cluster"), list(e.get("fields") or [])
        self.cluster_mode = e.get("mode")
        self.attempt, self.max_attempts = e.get("attempt"), e.get("max_attempts")
        for name in self.cluster_fields:
            self.recovery_attempts[name] = self.recovery_attempts.get(name, 0) + 1

    def _on_field_recovery_queue_resolved_indirectly(self, e: dict) -> None:
        self.resolved_indirectly[e.get("field")] = e.get("resolved_during_field")

    def _on_finalization_checkpoint_written(self, e: dict) -> None:
        self.action = None
        self.phase, self.op, self.field = "finalization", "finalizing", None

    def _on_finalization_started(self, e: dict) -> None:
        self.action = None
        self.phase, self.op, self.field = "finalization", "finalizing", None
        self.model = e.get("model") or self.finalizer_model

    def _on_interrupted(self, e: dict) -> None:
        self.op = "interrupted"

    def _on_run_finished(self, e: dict) -> None:
        self.status = e.get("status")
        self.finished = time.monotonic()
        self.field = None
        if self.status == "interrupted":
            self.op = "interrupted"
        elif self.status in FAILED_RUN_STATUSES:
            self.op = "failed"
        else:
            self.op = "completed"

    def mark_error(self, message: str) -> None:
        self.status, self.op = "error", "failed"
        self.lines.append(f"❌ {message}")
        self.version += 1

    # -- derived views ---------------------------------------------------------------------------------

    def _spec(self, name: str) -> dict | None:
        return next((s for s in self.specs if s.get("name") == name), None)

    def evaluation(self) -> list[dict]:
        """The shared evaluator over this vehicle's events (no second evaluator, no stale snapshot)."""
        if self._evaluation is None:
            self._evaluation = current_evaluation(self.eval_events, self.specs, self.target_market) if self.specs else []
        return self._evaluation

    def applicable(self) -> list[dict]:
        return [s for s in self.specs if s.get("applicable", True)]

    def progress(self) -> dict:
        """Counts over APPLICABLE requested fields (the denominator is not always the schema size).

        completed: current state ok, or not_applicable declared by the model for an applicable field.
        with_evidence / without_evidence: strict, from stored evidence records (as targets_without_stored_
        evidence); needs_followup: current state is anything else (it may well have evidence)."""
        names = [s["name"] for s in self.applicable()]
        ev = {e["field"]: e for e in self.evaluation()}
        state = {n: (ev.get(n) or {}).get("state") for n in names}
        has_ev = {n: bool((ev.get(n) or {}).get("evidence_ids")) for n in names}
        return {"total": len(names),
                "completed": sum(1 for n in names if state[n] in DONE_STATES),
                "with_evidence": sum(1 for n in names if has_ev[n]),
                "needs_followup": sum(1 for n in names if state[n] not in DONE_STATES),
                "without_evidence": sum(1 for n in names if not has_ev[n]),
                "conflicting": sum(1 for n in names if state[n] == "conflicting")}

    def origin(self, name: str, state: str | None) -> str:
        if state == "not_applicable":
            return he.RESOLUTION_ORIGIN_HE["not_applicable"]
        if state != "ok":
            if (name == self.field or (self.cluster and self.phase == "field_recovery"
                                       and name in self.cluster_fields)) and self.op not in TERMINAL_OPS:
                return he.RESOLUTION_ORIGIN_HE["in_progress"]
            return he.RESOLUTION_ORIGIN_HE["needs_decision" if state == "conflicting" else "open"]
        if name in self.resolved_indirectly:
            return he.RESOLUTION_ORIGIN_HE["recovery_indirect"]
        if self.recovery_attempts.get(name):
            return he.RESOLUTION_ORIGIN_HE["recovery_direct"]
        if name in self.sweep_resolved:
            return he.RESOLUTION_ORIGIN_HE["document_sweep"]
        return he.RESOLUTION_ORIGIN_HE["primary"]

    def field_rows(self) -> list[dict]:
        """The Hebrew field-progress table (user-facing: no canonical identifiers)."""
        ev = {e["field"]: e for e in self.evaluation()}
        evidence = [e.get("evidence") for e in self.eval_events if e.get("kind") == "evidence"]
        rows = []
        for spec in self.applicable():
            name = spec["name"]
            entry = ev.get(name) or {}
            state = entry.get("state")
            values = entry.get("values") or []
            last = next((item for item in reversed(evidence) if isinstance(item, dict) and item.get("field") == name), None)
            rows.append({
                "קבוצה": group_display_name(spec.get("group")),
                "שדה": field_display_name(name, spec),
                "מצב": he.state_label(state),
                "ערך / ערכים שנמצאו": " / ".join(_short(v, 30) for v in values[:4]) if values else "—",
                "מספר ראיות": len(entry.get("evidence_ids") or []),
                "שווקים": ", ".join("ישראל" if str(m).upper() in ("IL", "ISRAEL") else str(m)
                                     for m in entry.get("markets") or []) or "—",
                "מקור אחרון": _domain((last or {}).get("source_url")) or ((last or {}).get("document_id") or "—"),
                "סוג מקור": he.authority_label((last or {}).get("source_authority")),
                "התאמת גרסה": he.variant_match_label((last or {}).get("variant_match")),
                "ניסיון Recovery": self.recovery_attempts.get(name, 0),
                "אופן ההשלמה": self.origin(name, state),
            })
        return rows

    def why(self) -> str | None:
        if self.op in TERMINAL_OPS:
            return None
        if self.phase == "field_recovery" and self.cluster and not self.field:
            text = he.WHY_CLUSTER_HE.format(count=len(self.cluster_fields), cluster=he.cluster_label(self.cluster),
                                            mode=he.CLUSTER_MODE_HE.get(self.cluster_mode or "", self.cluster_mode))
            if self.attempt:
                text += f" (ניסיון {self.attempt}" + (f" מתוך {self.max_attempts}" if self.max_attempts else "") + ")"
            return text
        if self.phase == "field_recovery" and self.field:
            label = field_display_name(self.field, self._spec(self.field))
            template = he.WHY_BY_FAILURE_HE.get(self.failure_reason or "", he.WHY_BY_PHASE_HE["field_recovery"])
            text = template.format(field=label)
            if self.attempt:
                text += f" (ניסיון {self.attempt}" + (f" מתוך {self.max_attempts}" if self.max_attempts else "") + ")"
            return text
        return he.WHY_BY_PHASE_HE.get(self.phase or "")

    def cost_usd(self) -> float | None:
        """Known cost from returned usage only; None when a used model has no known prices."""
        total = 0.0
        for model, usage in self.usage_by_model.items():
            pricing = self.pricing_finalizer if (model == self.finalizer_model and model != self.research_model) \
                else self.pricing
            cost = compute_cost(usage, 0, pricing)["tokens_usd"]
            if cost is None and (usage["prompt_tokens"] or usage["completion_tokens"]):
                return None
            total += cost or 0.0
        search = compute_cost({}, self.search_api_calls, self.pricing)["web_search_usd"] or 0.0
        return round(total + search, 6)

    def card(self) -> dict:
        """Everything the vehicle card shows, in Hebrew."""
        p = self.progress()
        budget = self.recovery_budget
        if budget:
            recovery = {"text": f"{self.recovery_turns} / {budget}", "remaining": max(0, budget - self.recovery_turns)}
        else:
            recovery = {"text": he.UNLIMITED_RECOVERY_HE, "remaining": None}
        action = None
        if self.action and self.op not in TERMINAL_OPS:
            action = {"label": he.tool_action(self.action[0]), "detail": self.action[1]}
        reasoning = None
        if self.op == "model_inflight":
            reasoning = he.MODEL_THINKING_HE
        elif self.model_calls:
            reasoning = self.reasoning if self.reasoning_available else he.NO_REASONING_HE
        return {
            "title": self.title,
            "phase": he.phase_label(self.phase) if self.phase else he.op_label("queued"),
            "model": he.model_display_name(self.model) if self.model else "—",
            "op": he.op_label(self.op),
            "field": field_display_name(self.field, self._spec(self.field)) if self.field else None,
            "why_title": he.WHY_TITLE_HE,
            "why": self.why(),
            "action": action,
            "progress": p,
            "progress_text": f"הושלמו {p['completed']} מתוך {p['total']} שדות",
            "recovery": recovery,
            "harvest": {"documents": self.harvest["documents"], "fields_with_candidates":
                        len(self.harvest["fields"] & {s["name"] for s in self.applicable()})},
            "sweep": dict(self.sweep_info),
            "evidence_rejected": self.evidence_rejected,
            "reasoning_title": he.REASONING_TITLE_HE,
            "reasoning": reasoning,
            "reasoning_available": self.reasoning_available,
            "reasoning_context": self.reasoning_context,
            "status": he.run_status_label(self.status) if self.status else None,
        }


# --- the batch ---------------------------------------------------------------------------------------------

class BatchLiveState:
    """All vehicles of one running batch. Fed only from the queue on the main thread."""

    def __init__(self, vehicles: list[tuple[str, str]], *, pricing: dict | None = None,
                 pricing_finalizer: dict | None = None, controller=None):
        self.order = [rid for rid, _ in vehicles]
        self.vehicles = {rid: VehicleLive(rid, title, pricing=pricing, pricing_finalizer=pricing_finalizer)
                         for rid, title in vehicles}
        self.queue: queue_mod.Queue = queue_mod.Queue()
        self.controller = controller
        self.started = time.monotonic()

    def listener_for(self, record_id: str):
        """A RunLog listener for a worker: it only enqueues (never touches Streamlit)."""
        q = self.queue

        def listen(kind: str, event: dict) -> None:
            q.put({"record_id": record_id, "kind": kind, "event": event})

        return listen

    def drain(self, limit: int = 20000) -> set[str]:
        changed: set[str] = set()
        for _ in range(limit):
            try:
                item = self.queue.get_nowait()
            except queue_mod.Empty:
                break
            vehicle = self.vehicles.get(str(item.get("record_id")))
            if vehicle is None:
                continue
            vehicle.apply(item.get("kind") or "", item.get("event") or {})
            changed.add(vehicle.record_id)
        return changed

    def header(self) -> dict:
        vs = list(self.vehicles.values())
        totals = {"total": 0, "completed": 0, "with_evidence": 0, "without_evidence": 0, "conflicting": 0,
                  "needs_followup": 0}
        for v in vs:
            for key, value in v.progress().items():
                totals[key] = totals.get(key, 0) + value
        costs = [v.cost_usd() for v in vs]
        pools = []
        if self.controller is not None:
            snap = self.controller.snapshot()
            for model, info in sorted(snap["chat"].items()):
                pools.append({"name": he.model_display_name(model), "active": info["active"], "limit": info["limit"],
                              "waiting": info["waiting"], "provider_limit": info["provider_limit"]})
            s = snap["search"]
            pools.append({"name": "Search-Prime", "active": s["active"], "limit": s["limit"], "waiting": s["waiting"],
                          "provider_limit": s["provider_limit"]})
        return {
            "title": f"ריצת העשרה — {len(vs)} רכבים",
            "done": sum(1 for v in vs if v.op in TERMINAL_OPS),
            "total": len(vs),
            "active": sum(1 for v in vs if v.op not in TERMINAL_OPS + ("queued",)),
            "waiting_model": sum(1 for v in vs if v.op == "waiting_model"),
            "waiting_search": sum(1 for v in vs if v.op == "waiting_search"),
            "failed": sum(1 for v in vs if v.op == "failed"),
            "interrupted": sum(1 for v in vs if v.op == "interrupted"),
            "pools": pools,
            "elapsed_s": round(time.monotonic() - self.started, 1),
            "cost_usd": round(sum(c for c in costs if c is not None), 4),
            "cost_complete": all(c is not None for c in costs) and not any(v.unknown_usage for v in vs),
            "model_calls": sum(v.model_calls for v in vs),
            "searches": sum(v.search_api_calls for v in vs),
            "http_429": sum(v.http_429 for v in vs),
            "timeouts": sum(v.timeouts for v in vs),
            "fields": totals,
            "fields_text": f"{totals['completed']:,} / {totals['total']:,} שדות Level 2 הושלמו",
            "progress_caption": he.PROGRESS_CAPTION_HE,
        }


def candidate_table_rows(events: list[dict], specs: list[dict], vehicle: dict | None = None) -> list[dict]:
    """The Hebrew candidate-matrix table: what the parser found per field, how many sources, whether
    verified evidence exists, and the current state. Display only; candidates never change a state."""
    from ..candidate_harvest import candidate_matrix

    applicable = [s for s in specs if s.get("applicable", True)]
    matrix = candidate_matrix(events, applicable, vehicle)
    evaluation = {e["field"]: e for e in current_evaluation(events, applicable)} if applicable else {}
    rows = []
    for spec in applicable:
        name = spec["name"]
        cands = matrix["fields"].get(name) or []
        values: list[str] = []
        for cand in cands:
            text = f"{cand.get('value')}" + (f" {cand['unit']}" if cand.get("unit") and not isinstance(cand.get("value"), bool) else "")
            if isinstance(cand.get("value"), bool):
                text = "כן" if cand["value"] else "לא"
            if text not in values:
                values.append(text)
        entry = evaluation.get(name) or {}
        state = entry.get("state")
        sources = {c.get("document_id") or c.get("source_url") for c in cands}
        rows.append({
            "שדה": field_display_name(name, spec),
            "מועמדים שנמצאו": ", ".join(values[:5]) if values else "—",
            "מקורות": len(sources),
            "ראיה מאומתת": "כן" if entry.get("evidence_ids") else "לא",
            "מצב": "דורש חיפוש" if not cands and state not in DONE_STATES else he.state_label(state),
        })
    return rows

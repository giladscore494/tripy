"""The research pipeline of one vehicle, reconstructed from its durable events.jsonl.

Everything here is derived from events the engine already writes (no model call, no new telemetry):

    Source acquisition  (phase research)            run_started ... research_stopped
    Deterministic harvest                            ... deterministic_harvest_summary
    Document sweep      (local-first)                document_inspection / document_sweep_* events
    Tail recovery       (field detection + recovery) field_evaluation(primary) ... field_evaluation(after_recovery)
    Finalization        (checkpoint + finalizer)     finalization_checkpoint_written ... finalization_finished

Counters (sources, candidates, resolved fields, turns, searches, model calls) reuse `VehicleLive`, the same state
machine the live dashboard uses, fed with the same events, so the UI shows identical numbers whether it watched
the run live or reopened it after a browser refresh or a server restart.

`EventTail` reads events.jsonl incrementally (only new complete lines), so polling an active run is cheap.
No percentages are invented: stages report waiting / running / done / skipped / failed / interrupted.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path

from ..storage.trace import parse_ts, phase_group
from ..ui.live_state import VehicleLive
from .model import ENGINE_PHASE_TO_STAGE, STAGE_KEYS, STAGE_LABELS

WAITING, RUNNING, DONE, SKIPPED, FAILED, INTERRUPTED, NOT_RUN = (
    "waiting", "running", "done", "skipped", "failed", "interrupted", "not_run")
STAGE_STATE_LABELS = {WAITING: "Waiting", RUNNING: "Running", DONE: "Done", SKIPPED: "Skipped", FAILED: "Failed",
                      INTERRUPTED: "Interrupted", NOT_RUN: "Not run"}
STAGE_ICONS = {WAITING: "○", RUNNING: "●", DONE: "✓", SKIPPED: "–", FAILED: "✕", INTERRUPTED: "■", NOT_RUN: "○"}


class EventTail:
    """Incremental reader of an append-only events.jsonl (complete lines only; a torn last line waits)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.offset = 0
        self._buffer = b""
        self._lock = threading.Lock()

    def read_new(self) -> list[dict]:
        with self._lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                return []
            if size < self.offset:            # file replaced/truncated: start over
                self.offset, self._buffer = 0, b""
            if size == self.offset:
                return []
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
            self.offset = size
            data = self._buffer + chunk
            lines = data.split(b"\n")
            self._buffer = lines.pop()        # incomplete tail (no newline yet)
            out = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue                 # a line cut off by a hard kill is skipped, as read_events does
                if isinstance(value, dict):
                    out.append(value)
            return out


class VehiclePipeline:
    """Stage states, timings and counters of one vehicle, fed event by event (replay or live)."""

    def __init__(self, record_id: str, title: str = ""):
        self.record_id = record_id
        self.live = VehicleLive(record_id, title or record_id)
        self.stages: dict[str, str] = {k: WAITING for k in STAGE_KEYS}
        self.stage_started: dict[str, str] = {}
        self.stage_finished: dict[str, str] = {}
        self.stage_notes: dict[str, str] = {}
        self.started_at: str | None = None
        self.last_event_at: str | None = None
        self.finished_at: str | None = None
        self.engine_status: str | None = None
        self.stop_reason: str | None = None
        self.layered = True
        self.acquisition: dict = {}           # last primary_research_turn / primary_research_summary
        self.harvest: dict = {}
        self.sweep: dict = {}
        self.recovery: dict = {}
        self.model_calls_by_stage: Counter = Counter()
        self.evidence_admitted = 0
        self.evidence_rejected = 0
        self.errors: list[dict] = []
        self.retrying = False                 # a finalize-existing retry is in progress
        self.max_turns: int | None = None
        self._sweep_fields: set[str] = set()
        self.events_seen = 0

    # -- stage helpers ----------------------------------------------------------------------------------
    def _start(self, stage: str, ts: str | None) -> None:
        if self.stages[stage] in (WAITING, NOT_RUN, FAILED, INTERRUPTED) or stage == "finalization":
            if self.stages[stage] != RUNNING:
                self.stage_started[stage] = ts or self.stage_started.get(stage)
                self.stage_finished.pop(stage, None)
            self.stages[stage] = RUNNING
        # every earlier stage still marked waiting/running is over by now
        for earlier in STAGE_KEYS[:STAGE_KEYS.index(stage)]:
            if self.stages[earlier] == RUNNING:
                self._finish(earlier, DONE, ts)
            elif self.stages[earlier] == WAITING:
                self.stages[earlier] = SKIPPED

    def _finish(self, stage: str, state: str, ts: str | None) -> None:
        self.stages[stage] = state
        if ts:
            self.stage_finished[stage] = ts
            self.stage_started.setdefault(stage, ts)

    def current_stage(self) -> str | None:
        return next((k for k in STAGE_KEYS if self.stages[k] == RUNNING), None)

    @property
    def finished(self) -> bool:
        return self.engine_status is not None and not self.retrying

    # -- intake -----------------------------------------------------------------------------------------
    def apply(self, event: dict) -> None:
        kind = event.get("kind") or ""
        ts = event.get("ts")
        self.events_seen += 1
        self.last_event_at = ts or self.last_event_at
        self.live.apply(kind, event)
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(event, ts)
        if kind == "model_response":
            stage = ENGINE_PHASE_TO_STAGE.get(event.get("phase") or "research", "acquisition")
            self.model_calls_by_stage[stage] += 1

    def apply_all(self, events: list[dict]) -> "VehiclePipeline":
        for event in events:
            self.apply(event)
        return self

    def _on_run_started(self, e: dict, ts) -> None:
        self.started_at = self.started_at or ts
        self.layered = bool((e.get("agent_config") or {}).get("layered_harvest_enabled", True))
        self.max_turns = (e.get("agent_config") or {}).get("max_steps") or e.get("max_steps")
        self._start("acquisition", ts)

    def _on_primary_research_turn(self, e: dict, ts) -> None:
        self.acquisition.update({k: e.get(k) for k in ("turn", "useful_documents", "fields_with_candidates",
                                                       "candidate_field_coverage_pct", "scoped_coverage_pct")})
        after = e.get("state_after") or {}          # diagnostic state (newer runs only)
        if after:
            self.acquisition.update({"official_sources": after.get("official_documents"),
                                     "target_market_documents": after.get("target_market_documents"),
                                     "candidates_live": after.get("candidates")})

    def _on_primary_research_summary(self, e: dict, ts) -> None:
        self.acquisition.update({k: e.get(k) for k in (
            "turns", "useful_documents", "target_market_documents", "official_sources", "candidate_fields",
            "candidate_field_coverage_pct", "stop_reason", "minimum_acquisition_met", "scoped_coverage_pct")})

    def _on_error(self, e: dict, ts) -> None:
        stage = ENGINE_PHASE_TO_STAGE.get(e.get("phase") or "research", "acquisition")
        self.errors.append({"stage": stage, "message": e.get("message"), "api_error": e.get("api_error")})
        self._finish(stage, FAILED, ts)

    def _on_research_stopped(self, e: dict, ts) -> None:
        self.stop_reason = e.get("reason")
        if self.stages["acquisition"] != FAILED:
            self._finish("acquisition", DONE, ts)
        if self.stages["acquisition"] == FAILED or e.get("reason") in ("api_failure", "research_exception"):
            for stage in STAGE_KEYS[1:]:
                self.stages[stage] = NOT_RUN
        elif not self.layered:
            self.stages["harvest"] = self.stages["sweep"] = SKIPPED
            self.stage_notes["harvest"] = "disabled (LAYERED_HARVEST_ENABLED=false)"
        else:
            self._start("harvest", ts)

    def _on_deterministic_harvest_summary(self, e: dict, ts) -> None:
        self.harvest = {k: e.get(k) for k in ("documents_harvested", "candidate_count_total", "candidate_fields_total",
                                               "candidate_field_coverage_pct", "applicable_fields")}
        self._finish("harvest", DONE, ts)
        self._start("sweep", ts)

    def _on_document_inspection(self, e: dict, ts) -> None:
        if self.stages["sweep"] == WAITING:
            self._start("sweep", ts)

    def _on_document_sweep_started(self, e: dict, ts) -> None:
        if self.stages["sweep"] != RUNNING:
            self._start("sweep", ts)
        chunk = e.get("chunk") or {}
        self.sweep["chunk"] = f"{chunk.get('index')}/{chunk.get('of')}" if (chunk.get("of") or 1) > 1 else None
        self._sweep_fields.update(e.get("fields_to_review") or [])
        self.sweep["fields_entering"] = len(self._sweep_fields)

    def _on_document_sweep_finished(self, e: dict, ts) -> None:
        self.sweep["fields_resolved"] = (self.sweep.get("fields_resolved") or 0) + (e.get("unique_fields_resolved") or 0)
        self.sweep.update({k: e.get(k) for k in ("fields_unresolved_before", "document_sweep_calls",
                                                  "document_sweep_latency_ms", "deterministic_misses_found",
                                                  "document_sweep_timeouts")})
        self._finish("sweep", DONE, ts)

    def _on_document_sweep_skipped(self, e: dict, ts) -> None:
        self.stage_notes["sweep"] = f"skipped ({e.get('reason')})"
        self._finish("sweep", SKIPPED, ts)

    def _on_document_sweep_failed(self, e: dict, ts) -> None:
        self.stage_notes["sweep"] = "failed; the run continued with the harvested candidates"
        self.errors.append({"stage": "sweep", "message": e.get("error"), "non_fatal": True})
        self._finish("sweep", FAILED, ts)

    def _on_field_evaluation(self, e: dict, ts) -> None:
        if e.get("stage") == "primary":
            self._start("recovery", ts)
        elif e.get("stage") == "after_recovery":
            self._finish("recovery", DONE, ts)

    def _on_field_retry_queue(self, e: dict, ts) -> None:
        fields = e.get("fields") or []
        self.recovery["queued_fields"] = len(fields)
        if not fields:
            self.stage_notes["recovery"] = "nothing to recover"

    def _on_cluster_recovery_started(self, e: dict, ts) -> None:
        self.recovery["cluster"] = e.get("cluster")
        self.recovery["attempt"] = e.get("attempt")

    def _on_field_recovery_failed(self, e: dict, ts) -> None:
        if not e.get("field"):            # the whole recovery stage raised (the run still finalizes)
            self.stage_notes["recovery"] = "failed; the run continued to finalization"
            self.errors.append({"stage": "recovery", "message": e.get("error"), "non_fatal": True})
            self._finish("recovery", FAILED, ts)

    def _on_finalization_checkpoint_written(self, e: dict, ts) -> None:
        self._start("finalization", ts)
        self.stage_notes["finalization"] = "durable checkpoint written"

    def _on_finalization_started(self, e: dict, ts) -> None:
        self._start("finalization", ts)

    def _on_finalization_finished(self, e: dict, ts) -> None:
        self.stage_notes.pop("finalization", None)
        # "parsed" = structured output; "unparsed" = the reply was not valid JSON even after the repair turn
        self._finish("finalization", DONE if e.get("status") in (None, "parsed") else FAILED, ts)
        if e.get("status") == "unparsed":
            self.stage_notes["finalization"] = "the final answer could not be parsed as JSON"

    def _on_finalization_failed(self, e: dict, ts) -> None:
        self.stage_notes.pop("finalization", None)
        self.errors.append({"stage": "finalization", "message": e.get("error"), "api_error": e.get("api_error")})
        self._finish("finalization", FAILED, ts)

    def _on_finalization_not_started(self, e: dict, ts) -> None:
        self.errors.append({"stage": "finalization", "message": f"finalization not started: {e.get('reason')}"})
        self._finish("finalization", FAILED, ts)

    def _on_evidence(self, e: dict, ts) -> None:
        self.evidence_admitted += 1

    def _on_evidence_rejected(self, e: dict, ts) -> None:
        self.evidence_rejected += 1

    def _on_interrupted(self, e: dict, ts) -> None:
        stage = ENGINE_PHASE_TO_STAGE.get(e.get("phase") or "", None) or self.current_stage()
        if stage:
            self._finish(stage, INTERRUPTED, ts)
        for later in STAGE_KEYS:
            if self.stages[later] == WAITING:
                self.stages[later] = NOT_RUN
        self.retrying = False

    def _on_run_finished(self, e: dict, ts) -> None:
        self.engine_status = e.get("status")
        self.stop_reason = e.get("stop_reason") or self.stop_reason
        self.finished_at = ts
        current = self.current_stage()
        if current and self.engine_status not in ("interrupted",):
            self._finish(current, DONE if self.engine_status not in ("research_failed", "finalization_failed",
                                                                     "error") else FAILED, ts)
        for stage in STAGE_KEYS:
            if self.stages[stage] == WAITING:
                # model finished without needing a separate finalizer call: nothing was skipped by failure
                self.stages[stage] = SKIPPED if self.engine_status in (
                    "completed", "completed_unparsed") else NOT_RUN
                if stage == "finalization" and self.stages[stage] == SKIPPED:
                    self.stage_notes["finalization"] = "not needed: the research model returned the final answer"

    # finalize-existing (retry from the durable checkpoint) appends to the same events.jsonl
    def _on_recovery_started(self, e: dict, ts) -> None:
        self.retrying = True
        self.stage_notes["finalization"] = "retry from the durable checkpoint (no new research)"
        self.stages["finalization"] = RUNNING
        self.stage_started["finalization"] = ts
        self.stage_finished.pop("finalization", None)

    def _on_recovery_finished(self, e: dict, ts) -> None:
        self.retrying = False
        self.engine_status = e.get("status")
        self.finished_at = ts
        self._finish("finalization", DONE if e.get("status") == "recovered_finalized" else FAILED, ts)

    # -- derived ----------------------------------------------------------------------------------------
    def stage_duration_s(self, stage: str) -> float | None:
        start, end = parse_ts(self.stage_started.get(stage)), parse_ts(self.stage_finished.get(stage))
        if start is None:
            return None
        if end is None:
            if self.stages[stage] != RUNNING:
                return None
            end = parse_ts(self.last_event_at)
        return round(max(0.0, (end - start).total_seconds()), 1) if end else None

    def counters(self) -> dict:
        """The headline counters. None = not known yet (never a guessed number)."""
        progress = self.live.progress()
        applicable = progress["total"] or None
        candidate_fields = self.harvest.get("candidate_fields_total")
        if candidate_fields is None:
            candidate_fields = self.acquisition.get("candidate_fields", self.acquisition.get("fields_with_candidates"))
        return {
            "sources": len(self.live.documents),
            "useful_sources": self.acquisition.get("useful_documents"),
            "target_market_sources": self.acquisition.get("target_market_documents"),
            "official_sources": self.acquisition.get("official_sources"),
            "candidates": self.harvest.get("candidate_count_total"),
            "candidate_fields": candidate_fields,
            "applicable_fields": applicable,
            "resolved_fields": progress["completed"] if applicable else None,
            "fields_with_evidence": progress["with_evidence"] if applicable else None,
            "conflicting_fields": progress["conflicting"] if applicable else None,
            "research_turns": self.model_calls_by_stage.get("acquisition", 0),
            "searches": self.live.search_api_calls,
            "model_calls": self.live.model_calls,
            "model_calls_by_stage": dict(self.model_calls_by_stage),
            "evidence_admitted": self.evidence_admitted,
            "evidence_rejected": self.evidence_rejected,
            "timeouts": self.live.timeouts,
            "rate_limited": self.live.http_429,
        }

    def view(self) -> dict:
        return {
            "record_id": self.record_id,
            "title": self.live.title,
            "current_stage": self.current_stage(),
            "stages": [{"key": k, "label": STAGE_LABELS[k], "state": self.stages[k],
                        "icon": STAGE_ICONS[self.stages[k]], "state_label": STAGE_STATE_LABELS[self.stages[k]],
                        "duration_s": self.stage_duration_s(k), "note": self.stage_notes.get(k)}
                       for k in STAGE_KEYS],
            "counters": self.counters(),
            "model": self.live.model,
            "research_model": self.live.research_model,
            "finalizer_model": self.live.finalizer_model,
            "activity": _activity(self),
            "engine_status": self.engine_status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "finished_at": self.finished_at,
            "errors": self.errors,
            "events_seen": self.events_seen,
            "acquisition": {**self.acquisition, "max_turns": self.max_turns,
                            "research_turns": self.model_calls_by_stage.get("acquisition", 0),
                            "searches": self.live.search_api_calls},
            "sweep": dict(self.sweep),
        }


def _activity(p: VehiclePipeline) -> str | None:
    """A short English line about what is happening now (from orchestration metadata only)."""
    if p.finished or p.current_stage() is None:
        return None
    op = p.live.op
    words = {"waiting_model": "Waiting for a model slot", "model_inflight": "Model is working",
             "waiting_search": "Waiting for a search slot", "searching": "Searching the web",
             "fetching": "Reading a source", "extracting": "Extracting from a document",
             "storing_evidence": "Checking evidence", "evaluating": "Evaluating fields", "finalizing": "Finalizing"}
    text = words.get(op)
    action = p.live.action
    if action and action[1] and op in ("searching", "fetching", "extracting", "waiting_search"):
        text = f"{text or 'Working'}: {action[1]}"
    if p.current_stage() == "recovery" and p.live.cluster:
        text = (text + " · " if text else "") + f"cluster {p.live.cluster}"
    return text


def pipeline_from_events(events: list[dict], record_id: str = "", title: str = "") -> VehiclePipeline:
    return VehiclePipeline(record_id, title).apply_all(events)


class PipelineCache:
    """Process-wide incremental pipelines per events.jsonl (shared by every browser session; thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[EventTail, VehiclePipeline]] = {}

    def get(self, events_path: Path | str, record_id: str, title: str = "") -> VehiclePipeline:
        key = str(Path(events_path).resolve())
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = (EventTail(events_path), VehiclePipeline(record_id, title))
                self._items[key] = item
            tail, pipeline = item
            for event in tail.read_new():
                pipeline.apply(event)
            if title:
                pipeline.live.title = title
            return pipeline

    def forget(self, events_path: Path | str) -> None:
        with self._lock:
            self._items.pop(str(Path(events_path).resolve()), None)


def stage_group(phase: str | None) -> str:
    """Engine phase -> pipeline stage (unknown phases count as acquisition, like trace.phase_group)."""
    return ENGINE_PHASE_TO_STAGE.get(phase or "", ENGINE_PHASE_TO_STAGE.get(phase_group(phase), "acquisition"))

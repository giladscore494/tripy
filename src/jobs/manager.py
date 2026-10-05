"""Background research runs, decoupled from browser sessions and HTTP requests.

One `RunManager` per server process owns the research threads (the FastAPI application builds it once at startup,
src/api/deps.py). Starting a run returns immediately with a stable `run_id`; the run executes on a daemon thread that
no request or browser owns, so refreshes, closed tabs and polling clients do not affect it. Every client discovers runs
from the durable run repository (run_state.json) and replays the engine's own events.jsonl for progress.

The research itself is exactly the engine's `research_one` / `run_batch` / `finalize_existing_run` path the old
UI and the CLI use, with the same shared ConcurrencyController (provider limits unchanged). Ordinary runs share
the document cache; A/B benchmark runs each use their own durable cold cache so one arm cannot train the next.

Guards against duplicate execution:
* `start` runs under one process lock: an idempotency key (one per form submission) returns the run it already
  created; a run for the same vehicle(s) that is still active is never started twice; at most
  TRIPY_MAX_ACTIVE_RUNS runs execute at once (default 1).
* A run executes only on the thread `start` spawned; nothing re-executes a run on rerender.
* A finalization retry refuses a run that is still active.

Lifecycle and restarts: a run whose owning process is gone (redeploy, crash) is marked INTERRUPTED when the next
process starts (`reconcile`), using the engine's own interrupted / finalization_pending results on disk. On a
normal shutdown (SIGTERM → the ASGI lifespan's shutdown, else interpreter exit) active runs are asked to stop at their
next safe point so they persist an `interrupted` partial result first. This is the limit of a single-process design:
a research run cannot outlive the server process that executes it.
"""

from __future__ import annotations

import atexit
import os
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path
from typing import Any, Callable

from ..agent import AgentConfig
from ..app_config import max_active_runs, redact
from ..benchmark import batch_observability, compute_metrics, research_one, run_batch, start_batch, \
    vehicle_client_factory
from ..concurrency import ConcurrencyController
from ..db import load_level15
from ..recovery import RecoveryError, finalize_existing_run
from ..runstate.failures import explain
from ..runstate.model import (ACTIVE_STATUSES, CANCELLED, COMPLETED, FAILED, FINALIZING, INTERRUPTED, QUEUED,
                              STAGE_KEYS, STAGE_TO_STATUS, STARTING, RunRecord, aggregate_status,
                              status_for_engine_result)
from ..runstate.pipeline import pipeline_from_events
from ..runstate.report import vehicle_report
from ..runstate.repository import FileRunRepository, parse_ts, utc_now
from ..server_logging import get_logger
from ..storage.cache import DocumentCache
from ..storage.paths import DataPaths
from ..storage.run_log import load_batch, new_batch_id, read_events, update_batch
from ..tools import ToolConfig

log = get_logger("jobs")

HEARTBEAT_S = 5.0
DEFAULT_SHUTDOWN_GRACE_S = 20.0


class RunRejected(RuntimeError):
    """The request was refused before any run was created (configuration, limits, duplicates)."""

    def __init__(self, message: str, *, existing_run_id: str | None = None):
        super().__init__(message)
        self.existing_run_id = existing_run_id


@dataclass
class ResearchRequest:
    vehicles: list[dict]                 # benchmark vehicles (upstream_record_id, manufacturer, model, ...)
    label: str
    scope: str                           # "One vehicle" | "Manufacturer" | "All 50"
    settings: Any                        # GLMSettings (holds the API key in memory only; never persisted)
    agent_cfg: AgentConfig
    tool_cfg: ToolConfig
    pricing: dict
    prompt_version: str
    data_source: str = "auto"
    dsn: str = ""
    workers: int = 1
    chat_limits: dict = field(default_factory=dict)
    search_limit: int | None = None
    idempotency_key: str | None = None
    series: dict | None = None           # {series_id, index, repeat, arm} for a run of an A/B series


@dataclass
class SeriesRequest:
    """An A/B benchmark series: for each repeat, for each arm (a run profile, in order), one run over `vehicles`."""
    template: ResearchRequest            # vehicles, settings, tool config, pricing, ... (agent_cfg replaced per arm)
    arm_configs: dict                    # run profile -> the AgentConfig of its runs, in arm order
    repeats: int = 3
    label: str = ""
    idempotency_key: str | None = None


# A/B series lifecycle (runs/_series/<series_id>/series.json)
SERIES_DIR = "_series"
SERIES_RUNNING, SERIES_COMPLETED, SERIES_CANCELLED, SERIES_INTERRUPTED, SERIES_FAILED = (
    "RUNNING", "COMPLETED", "CANCELLED", "INTERRUPTED", "FAILED")
PLANNED, NOT_STARTED = "PLANNED", "NOT_STARTED"


def plan_series(arms: list[str], repeats: int) -> list[dict]:
    """The runs of a series in execution order: arms interleaved per repeat (B, T, B, T, ...), so provider-side drift
    over time hits every arm equally."""
    plan = []
    for repeat in range(1, max(1, int(repeats)) + 1):
        for arm in arms:
            plan.append({"index": len(plan), "repeat": repeat, "arm": arm, "run_id": None, "status": PLANNED})
    return plan


def series_progress(series: dict) -> dict:
    """Planned / done / running per arm and in total (for the UI)."""
    def count(items: list[dict]) -> dict:
        return {"planned": len(items),
                "done": sum(1 for i in items if i["status"] not in (PLANNED, "RUNNING") and i.get("run_id")),
                "running": sum(1 for i in items if i["status"] == "RUNNING"),
                "not_run": sum(1 for i in items if i["status"] in (CANCELLED, NOT_STARTED) and not i.get("run_id"))}
    plan = series.get("planned") or []
    return {"total": count(plan), "arms": {arm: count([i for i in plan if i["arm"] == arm])
                                           for arm in series.get("arms") or []}}


@dataclass
class StartResult:
    run_id: str
    created: bool
    message: str = ""
    warnings: list = field(default_factory=list)


_CONTROLLER: ConcurrencyController | None = None
_CONTROLLER_LOCK = threading.Lock()


def shared_controller(env: Callable[[str], str | None] = os.environ.get) -> ConcurrencyController:
    """ONE controller per process: Z.ai limits are per account, so every run shares the pools (as before)."""
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = ConcurrencyController.from_env(env)
        return _CONTROLLER


def _vehicle_title(v: dict) -> str:
    return " · ".join(str(x) for x in (f"{v.get('manufacturer', '')} {v.get('model', '')}".strip(), v.get("year"),
                                       v.get("trim")) if x) or str(v.get("upstream_record_id"))


def agent_config_from_saved(saved: dict | None) -> AgentConfig:
    """The exact AgentConfig a run used (batch.json `agent_config`); unknown keys are ignored."""
    names = {f.name for f in dc_fields(AgentConfig)}
    return AgentConfig(**{k: v for k, v in (saved or {}).items() if k in names})


def tool_config_from_saved(saved: dict | None) -> ToolConfig:
    names = {f.name for f in dc_fields(ToolConfig)}
    return ToolConfig(**{k: v for k, v in (saved or {}).items() if k in names})


class _StageTracker:
    """RunLog listener (worker threads): persists the run's current pipeline stage when it ADVANCES.

    Only stage transitions are written (a handful per vehicle), never every event."""

    KIND_STAGE = {"run_started": "acquisition", "deterministic_harvest_summary": "sweep",
                  "document_inspection": "sweep", "document_sweep_started": "sweep",
                  "finalization_checkpoint_written": "finalization", "finalization_started": "finalization"}
    PHASE_STAGE = {"research": "acquisition", "deterministic_harvest": "harvest", "document_sweep": "sweep",
                   "field_detection": "recovery", "field_recovery": "recovery", "finalization": "finalization",
                   "finalization_repair": "finalization"}

    def __init__(self, manager: "RunManager", run_id: str):
        self.manager, self.run_id = manager, run_id
        self.stages: dict[str, str] = {}
        self.lock = threading.Lock()

    def _stage_for(self, kind: str, event: dict) -> str | None:
        if kind == "research_stopped":
            return None if event.get("reason") in ("api_failure", "research_exception") else "harvest"
        if kind == "field_evaluation" and event.get("stage") == "primary":
            return "recovery"
        if kind in self.KIND_STAGE:
            return self.KIND_STAGE[kind]
        if kind in ("model_request_started", "model_response", "tool_call"):
            return self.PHASE_STAGE.get(event.get("phase") or "")
        return None

    def listener_for(self, record_id: str) -> Callable[[str, dict], None]:
        def listen(kind: str, event: dict) -> None:
            stage = self._stage_for(kind, event)
            if stage is None:
                return
            with self.lock:
                current = self.stages.get(record_id)
                if current is not None and STAGE_KEYS.index(stage) <= STAGE_KEYS.index(current):
                    return
                self.stages[record_id] = stage
                active = [s for s in self.stages.values()]
                run_stage = min(active, key=STAGE_KEYS.index)       # the batch's least advanced vehicle
            try:
                def mutate(r: RunRecord) -> None:
                    if r.status in ACTIVE_STATUSES and not r.cancel_requested:
                        r.stage = run_stage
                        r.status = STAGE_TO_STATUS[run_stage]
                    r.vehicles.setdefault(record_id, {})["stage"] = stage
                self.manager.repository.update(self.run_id, mutate, durable=False)
            except Exception:  # noqa: BLE001 - status bookkeeping must never break research
                log.warning("run %s: could not persist stage %s", self.run_id, stage, exc_info=True)

        return listen


class RunManager:
    def __init__(self, paths: DataPaths, *, cache: DocumentCache | None = None,
                 controller: ConcurrencyController | None = None, vehicle_label: Callable[[str], str] | None = None,
                 max_active: int | None = None, heartbeat_stale_s: float = 90.0,
                 research_fn: Callable[..., dict] = research_one, level15_loader: Callable[..., Any] = load_level15,
                 client_factory: Callable[..., Callable[[], Any]] = vehicle_client_factory,
                 finalize_fn: Callable[..., dict] = finalize_existing_run, register_atexit: bool = True,
                 shared_storage: bool = False):
        self.paths = paths.ensure()
        self.runs_dir = paths.runs_dir
        self.cache = cache or DocumentCache(paths.cache_dir)
        self.controller = controller or shared_controller()
        self.repository = FileRunRepository(self.runs_dir, vehicle_label=vehicle_label)
        self.max_active = max_active or max_active_runs()
        self.heartbeat_stale_s = heartbeat_stale_s
        # One process per data directory is the supported deployment (one Railway service, one replica: a Volume
        # attaches to one deployment at a time). Then an active run owned by any other process is an orphan.
        # shared_storage=True (several processes on one folder) trusts a fresh heartbeat of another process instead.
        self.shared_storage = shared_storage
        self.boot_id = uuid.uuid4().hex
        self.owner = {"boot_id": self.boot_id, "pid": os.getpid(), "host": socket.gethostname()}
        self._research_fn, self._level15_loader = research_fn, level15_loader
        self._client_factory, self._finalize_fn = client_factory, finalize_fn
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._idempotency: dict[str, str] = {}
        self._shutting_down = False
        self._reconciled_at = time.monotonic()
        self._series_threads: dict[str, threading.Thread] = {}
        self._series_cancel: dict[str, threading.Event] = {}
        self._series_lock = threading.RLock()
        self.series_poll_s = 1.0
        self.reconcile()
        self.reconcile_series()
        if register_atexit:
            atexit.register(self.shutdown)

    # -- discovery ----------------------------------------------------------------------------------------
    def is_executing(self, run_id: str) -> bool:
        thread = self._threads.get(run_id)
        return bool(thread and thread.is_alive())

    def owned_here(self, record: RunRecord) -> bool:
        return (record.owner or {}).get("boot_id") == self.boot_id

    def heartbeat_fresh(self, record: RunRecord) -> bool:
        beat = parse_ts(record.heartbeat_at or record.updated_at)
        return bool(beat and time.time() - beat.timestamp() < self.heartbeat_stale_s)

    def reconcile(self) -> list[str]:
        """Mark runs that claim to be active but have no executing owner as INTERRUPTED. Returns their ids.

        An active run owned by THIS process without a live thread, or owned by another process whose heartbeat is
        stale, is an orphan (e.g. the container was redeployed mid-run)."""
        orphans = []
        for record in self.repository.list_runs():
            if record.legacy or not record.active:
                continue
            if self.owned_here(record):
                with self._lock:          # start() registers and starts the thread under this lock: no race
                    if self.is_executing(record.run_id):
                        continue
            elif self.shared_storage and self.heartbeat_fresh(record) and record.status != QUEUED:
                continue          # another live process executes it (shared storage); leave it alone
            self._finish_orphan(record)
            orphans.append(record.run_id)
        if orphans:
            log.warning("marked %d orphaned run(s) as INTERRUPTED: %s", len(orphans), ", ".join(orphans))
        return orphans

    def maybe_reconcile(self, min_interval_s: float = 30.0) -> list[str]:
        """`reconcile`, at most once per `min_interval_s` (called on page renders)."""
        now = time.monotonic()
        with self._lock:
            if now - self._reconciled_at < min_interval_s:
                return []
            self._reconciled_at = now
        return self.reconcile()

    def _finish_orphan(self, record: RunRecord) -> None:
        def mutate(r: RunRecord) -> None:
            statuses = []
            for rid in r.record_ids:
                result = self._read_result(r.run_id, rid)
                engine = (result or {}).get("status")
                vstatus = status_for_engine_result(engine) if engine else INTERRUPTED
                if engine is None:
                    events = read_events(self.runs_dir / r.run_id / rid / "events.jsonl")
                    finished = next((e for e in reversed(events) if e.get("kind") in ("run_finished",
                                                                                     "recovery_finished")), None)
                    if finished:
                        engine = finished.get("status")
                        vstatus = status_for_engine_result(engine)
                r.vehicles.setdefault(rid, {}).update({"status": vstatus, "engine_status": engine or "incomplete"})
                statuses.append(vstatus)
            r.status = INTERRUPTED if any(s == INTERRUPTED for s in statuses) or not statuses \
                else aggregate_status(statuses)
            r.finished_at = r.finished_at or utc_now()
            r.error = r.error or {"stage": r.stage, "code": "server_stopped",
                                  "message": "The server stopped while this run was active."}
            r.notes = list(r.notes) + [f"reconciled after restart at {utc_now()} (previous owner "
                                       f"{(r.owner or {}).get('host')}:{(r.owner or {}).get('pid')})"]
            for job in r.jobs:
                if not job.get("finished_at"):
                    job.update({"finished_at": utc_now(), "status": INTERRUPTED})
        try:
            self.repository.update(record.run_id, mutate)
        except Exception:  # noqa: BLE001
            log.warning("could not reconcile run %s", record.run_id, exc_info=True)

    def list_runs(self, limit: int | None = None) -> list[RunRecord]:
        return self.repository.list_runs(limit)

    def get(self, run_id: str) -> RunRecord | None:
        return self.repository.get(run_id)

    def active_runs(self) -> list[RunRecord]:
        return [r for r in self.repository.list_runs() if r.active and not r.legacy
                and (self.is_executing(r.run_id) or self._foreign_live(r))]

    def _foreign_live(self, record: RunRecord) -> bool:
        return self.shared_storage and not self.owned_here(record) and self.heartbeat_fresh(record)

    # -- start --------------------------------------------------------------------------------------------
    def start(self, request: ResearchRequest) -> StartResult:
        """Create and launch a run. Idempotent per `idempotency_key`; never starts a duplicate of an active run."""
        from dataclasses import replace

        from ..run_profiles import isolate_single_run

        if not request.vehicles:
            raise RunRejected("Select at least one vehicle.")
        # a single A/B-arm run (outside a series) runs without cross-run research memory / negative routes
        request = replace(request, agent_cfg=isolate_single_run(request.agent_cfg, in_series=bool(request.series)))
        ids = [str(v["upstream_record_id"]) for v in request.vehicles]
        with self._lock:
            if self._shutting_down:
                raise RunRejected("The server is shutting down; try again in a moment.")
            key = request.idempotency_key
            if key and key in self._idempotency:
                return StartResult(self._idempotency[key], created=False, message="This request already started.")
            active = self.active_runs()
            for record in active:
                if set(record.record_ids) & set(ids):
                    raise RunRejected(f"A run for this target is already active ({record.run_id}).",
                                      existing_run_id=record.run_id)
            if len(active) >= self.max_active:
                raise RunRejected(f"{len(active)} research run(s) already active (TRIPY_MAX_ACTIVE_RUNS="
                                  f"{self.max_active}). Wait for it to finish or stop it first.",
                                  existing_run_id=active[0].run_id)
            make_client = self._client_factory(request.settings, self.controller, None)
            client = make_client()          # validates the settings (GLMError -> the UI shows it; nothing created)
            load = self._level15_loader(ids, source=request.data_source, dsn=request.dsn)
            rows_by_id = {str(r["upstream_record_id"]): r for r in load.rows}
            warnings = [f"Level 1.5 rows not found for: {', '.join(load.missing)}"] if load.missing else []
            selection = [v for v in request.vehicles if str(v["upstream_record_id"]) in rows_by_id]
            if not selection:
                raise RunRejected("No Level 1.5 data was found for the selected vehicle(s).")
            model_id = request.settings.model
            finalizer_id = (request.settings.finalizer_model or "").strip()
            for model, limit in request.chat_limits.items():
                self.controller.set_model_limit(model, limit)
            if request.search_limit:
                self.controller.set_search_limit(int(request.search_limit))
            workers = max(1, min(int(request.workers or 1), len(selection)))
            run_id = new_batch_id(f"{model_id}-{request.scope.split()[0].lower()}", self.runs_dir)
            start_batch(self.runs_dir, run_id, client=client, agent_cfg=request.agent_cfg, tool_cfg=request.tool_cfg,
                        pricing=request.pricing, vehicles=selection, level15_source=load.source,
                        level15_note=load.note, selection=request.scope, prompt_version=request.prompt_version,
                        concurrency={"batch_max_workers": workers,
                                     **self.controller.config([model_id, finalizer_id or model_id])})
            record = RunRecord(
                run_id=run_id, status=QUEUED,
                target={"label": request.label, "scope": request.scope, "record_ids": [str(v["upstream_record_id"])
                                                                                       for v in selection],
                        "vehicles": [{"record_id": str(v["upstream_record_id"]), "label": _vehicle_title(v)}
                                     for v in selection]},
                request={"research_model": model_id, "finalizer_model": client.finalizer_model,
                         "search_backend": request.tool_cfg.search_backend, "workers": workers,
                         "level15_source": load.source, "prompt_version": request.prompt_version,
                         "run_profile": getattr(request.agent_cfg, "run_profile", "") or None,
                         "acquisition_mode": getattr(request.agent_cfg, "acquisition_mode", None),
                         "acquisition_document_card": getattr(request.agent_cfg, "acquisition_document_card", None),
                         **({"series": dict(request.series)} if request.series else {})},
                owner=dict(self.owner), idempotency_key=key, notes=list(warnings),
                vehicles={str(v["upstream_record_id"]): {"status": QUEUED, "label": _vehicle_title(v)}
                          for v in selection})
            self.repository.create(record)
            cancel = threading.Event()
            self._cancel[run_id] = cancel
            thread = threading.Thread(target=self._execute_research, name=f"tripy-run-{run_id}", daemon=True,
                                      args=(run_id, request, selection, rows_by_id, load.source, workers, cancel))
            self._threads[run_id] = thread
            if key:
                self._idempotency[key] = run_id
            thread.start()
        log.info("run %s started: %s (%d vehicle(s), model %s)", run_id, request.label, len(selection), model_id)
        return StartResult(run_id, created=True, warnings=warnings)

    # -- execution (background thread) -------------------------------------------------------------------
    def _heartbeat(self, run_id: str) -> Callable[[], None]:
        last = {"t": 0.0}

        def beat() -> None:
            now = time.monotonic()
            if now - last["t"] < HEARTBEAT_S:
                return
            last["t"] = now
            try:
                self.repository.update(run_id, lambda r: setattr(r, "heartbeat_at", utc_now()), durable=False)
            except Exception:  # noqa: BLE001
                log.warning("run %s: heartbeat write failed", run_id, exc_info=True)

        return beat

    def _execute_research(self, run_id: str, request: ResearchRequest, selection: list[dict],
                          rows_by_id: dict[str, dict], level15_source: str, workers: int,
                          cancel: threading.Event) -> None:
        started = utc_now()

        def begin(r: RunRecord) -> None:
            if r.status != QUEUED:       # never resurrect a run another process already finished or reconciled
                return
            r.status, r.started_at, r.heartbeat_at, r.owner = STARTING, started, started, dict(self.owner)
            r.jobs = list(r.jobs) + [{"kind": "research", "started_at": started}]
        try:
            self.repository.update(run_id, begin)
        except Exception:  # noqa: BLE001 - research still runs; its own artifacts are the source of truth
            log.error("run %s: could not mark the run as starting", run_id, exc_info=True)
        tracker = _StageTracker(self, run_id)
        # Every A/B run starts with an empty, durable cache. Sharing the normal cache (including its
        # research memory) lets the later arm inherit the earlier arm's documents and verified facts.
        # Keep this cache for a possible finalization retry of this run.
        cache = self._cache_for_series_run(request.series) if request.series else self.cache
        make_client = self._client_factory(request.settings, self.controller, cancel)
        heartbeat = self._heartbeat(run_id)
        results: dict[str, dict] = {}

        def run_one(vehicle: dict, row: dict) -> dict:
            rid = str(vehicle["upstream_record_id"])
            return self._research_fn(vehicle, row, client=make_client(), cache=cache, runs_dir=self.runs_dir,
                                     batch_id=run_id, agent_cfg=request.agent_cfg, tool_cfg=request.tool_cfg,
                                     pricing=request.pricing, level15_source=level15_source,
                                     listener=tracker.listener_for(rid), cancel_event=cancel)

        def on_done(vehicle: dict, result: dict) -> None:
            results[str(vehicle["upstream_record_id"])] = result
            self._record_vehicle(run_id, str(vehicle["upstream_record_id"]), result, cancel.is_set())

        stats: dict = {}
        outcome, failure = None, None
        cache_before = cache.stats_snapshot()
        try:
            with self.controller.observe() as observation:
                try:
                    run_batch(selection, rows_by_id, run_one, on_done=on_done, max_workers=workers,
                              cancel_event=cancel, on_poll=heartbeat, stats=stats)
                finally:
                    try:
                        update_batch(self.runs_dir, run_id, {"concurrency_observed": batch_observability(
                            stats, observation, cache_before, cache.stats_snapshot())})
                    except Exception:  # noqa: BLE001
                        log.warning("run %s: could not write concurrency observability", run_id, exc_info=True)
        except BaseException as exc:  # noqa: BLE001 - cancellation, shutdown, or an unexpected error
            if cancel.is_set() and not self._shutting_down:
                outcome = CANCELLED
            elif self._shutting_down or not isinstance(exc, Exception):
                outcome = INTERRUPTED
            else:
                outcome = FAILED
                failure = exc
                log.error("run %s failed:\n%s", run_id, traceback.format_exc())
        finally:
            self._complete(run_id, outcome=outcome, failure=failure, cancel_requested=cancel.is_set()
                           and not self._shutting_down)
            log.info("run %s finished: %s", run_id, (self.get(run_id) or RunRecord(run_id)).status)

    def _read_result(self, run_id: str, record_id: str) -> dict | None:
        import json

        path = self.runs_dir / run_id / record_id / "result.json"
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _record_vehicle(self, run_id: str, record_id: str, result: dict, cancel_requested: bool) -> None:
        engine = result.get("status")
        vstatus = status_for_engine_result(engine, cancel_requested=cancel_requested)

        def mutate(r: RunRecord) -> None:
            entry = r.vehicles.setdefault(record_id, {})
            entry.update({"status": vstatus, "engine_status": engine, "finished_at": utc_now(),
                          "error": redact(result.get("error"))[:500] if result.get("error") else None})
        try:
            self.repository.update(run_id, mutate)
        except Exception:  # noqa: BLE001
            log.warning("run %s: could not record vehicle %s", run_id, record_id, exc_info=True)

    def _complete(self, run_id: str, *, outcome: str | None, failure: BaseException | None,
                  cancel_requested: bool) -> None:
        """Final bookkeeping from the engine's own artifacts: per-vehicle status, report, error summary."""
        record = self.repository.get(run_id)
        if record is None:
            return
        reports, statuses, first_failure = {}, [], None
        for rid in record.record_ids:
            result = self._read_result(run_id, rid)
            events = read_events(self.runs_dir / run_id / rid / "events.jsonl")
            pipeline = pipeline_from_events(events, rid) if events else None
            known = record.vehicles.get(rid) or {}
            engine = (result or {}).get("status") or (pipeline.engine_status if pipeline else None)
            if engine is None and known.get("engine_status") == "error":
                # the worker raised before the engine wrote result.json (run_batch made it an `error` result)
                engine = "error"
                result = {"record_id": rid, "status": "error", "error": known.get("error"), "output": None}
            if engine is None:
                vstatus = CANCELLED if cancel_requested else (INTERRUPTED if outcome != FAILED else FAILED)
            else:
                vstatus = status_for_engine_result(engine, cancel_requested=cancel_requested)
            statuses.append(vstatus)
            if result or pipeline:
                reports[rid] = vehicle_report(result, pipeline)
            if vstatus != COMPLETED and first_failure is None and (result or pipeline):
                info = explain(result, pipeline.view() if pipeline else None, run_status=vstatus)
                if info:
                    first_failure = {"stage": info["stage"], "code": info["code"], "message": info["reason"],
                                     "record_id": rid}
            record.vehicles.setdefault(rid, {}).update({"status": vstatus, "engine_status": engine or "not_started"})
        status = outcome if outcome in (CANCELLED, INTERRUPTED) else aggregate_status(
            statuses, cancel_requested=cancel_requested)
        if outcome == FAILED:
            status = FAILED if not any(s == COMPLETED for s in statuses) else status
        error = None
        if status != COMPLETED:
            if failure is not None:
                error = {"stage": record.stage, "code": "unexpected",
                         "message": "An unexpected error stopped the run.",
                         "detail": redact(f"{type(failure).__name__}: {failure}")[:500]}
            elif status == CANCELLED:
                error = {"stage": record.stage, "code": "cancelled", "message": "The run was stopped by a user."}
            elif status == INTERRUPTED and first_failure is None:
                error = {"stage": record.stage, "code": "server_stopped",
                         "message": "The server stopped while this run was active."}
            else:
                error = first_failure
        vehicles = record.vehicles
        finished = utc_now()

        def mutate(r: RunRecord) -> None:
            r.status, r.finished_at, r.error, r.cancel_requested = status, finished, error, cancel_requested
            r.stage = None
            r.vehicles = {**r.vehicles, **{k: {**r.vehicles.get(k, {}), **v} for k, v in vehicles.items()}}
            r.report = {"vehicles": reports, "generated_at": finished}
            if r.jobs and not r.jobs[-1].get("finished_at"):
                r.jobs[-1] = {**r.jobs[-1], "finished_at": finished, "status": status}
        try:
            self.repository.update(run_id, mutate)
        except Exception:  # noqa: BLE001
            log.error("run %s: could not write the final run state", run_id, exc_info=True)
        try:      # benchmark aggregate of this run's vehicles (observational diagnostics; src/diagnostics.py)
            from ..diagnostics import write_benchmark
            write_benchmark(self.runs_dir, [run_id], self.runs_dir / run_id / "diagnostics")
        except Exception:  # noqa: BLE001
            log.warning("run %s: could not write the diagnostics aggregate", run_id, exc_info=True)
        with self._lock:
            self._cancel.pop(run_id, None)

    # -- cancel -------------------------------------------------------------------------------------------
    def cancel(self, run_id: str) -> bool:
        """Ask an executing run to stop at its next safe point (it persists an interrupted partial result)."""
        with self._lock:
            event = self._cancel.get(run_id)
            if event is None or not self.is_executing(run_id):
                return False
            event.set()
        try:
            self.repository.update(run_id, lambda r: setattr(r, "cancel_requested", True))
        except Exception:  # noqa: BLE001
            pass
        log.info("run %s: cancellation requested", run_id)
        return True

    # -- retry from the durable checkpoint ------------------------------------------------------------------
    def retry_finalization(self, run_id: str, record_id: str, settings: Any) -> StartResult:
        """Finalize a saved vehicle run from its preserved research (finalize_existing_run): exactly the
        finalizer call, no search / fetch / harvest / sweep / recovery. Refused while the run is active."""
        record_id = str(record_id)
        with self._lock:
            record = self.repository.get(run_id)
            if record is None:
                raise RunRejected(f"Unknown run {run_id}.")
            if record.legacy:
                raise RunRejected("This run predates run tracking; use the CLI: python -m src.cli "
                                  f"--finalize-existing --batch-id {run_id} --record-id {record_id}")
            if self.is_executing(run_id) or (record.active and self._foreign_live(record)):
                raise RunRejected("This run is still active.", existing_run_id=run_id)
            if record_id not in record.record_ids:
                raise RunRejected(f"Vehicle {record_id} is not part of run {run_id}.")
            result = self._read_result(run_id, record_id)
            if result is not None and result.get("output") is not None:
                raise RunRejected("This vehicle already has a final result.")
            info = load_batch(self.runs_dir, run_id) or {}
            config = agent_config_from_saved(info.get("agent_config"))
            tool_config = tool_config_from_saved(info.get("tool_config"))
            # the run's own models (a retry never silently switches models)
            settings.model = info.get("research_model") or info.get("model") or settings.model
            settings.finalizer_model = info.get("finalizer_model") or settings.finalizer_model
            cancel = threading.Event()
            client = self._client_factory(settings, self.controller, cancel)()
            started = utc_now()

            def begin(r: RunRecord) -> None:
                r.status, r.stage, r.error, r.cancel_requested = FINALIZING, "finalization", None, False
                r.finished_at, r.heartbeat_at, r.owner = None, started, dict(self.owner)
                r.vehicles.setdefault(record_id, {})["status"] = FINALIZING
                r.jobs = list(r.jobs) + [{"kind": "finalization_retry", "record_id": record_id,
                                          "started_at": started}]
            self.repository.update(run_id, begin)
            self._cancel[run_id] = cancel
            thread = threading.Thread(target=self._execute_finalization, name=f"tripy-finalize-{run_id}",
                                      daemon=True, args=(run_id, record_id, client, config, tool_config, cancel))
            self._threads[run_id] = thread
            thread.start()
        log.info("run %s: finalization retry started for %s", run_id, record_id)
        return StartResult(run_id, created=True)

    def _execute_finalization(self, run_id: str, record_id: str, client, config: AgentConfig,
                              tool_config: ToolConfig, cancel: threading.Event) -> None:
        outcome, failure = None, None
        vehicle = {"upstream_record_id": record_id}
        try:
            from ..benchmark import benchmark_vehicles
            vehicle = next((v for v in benchmark_vehicles() if v["upstream_record_id"] == record_id), vehicle)
        except Exception:  # noqa: BLE001
            pass
        try:
            record = self.get(run_id)
            series = (record.request or {}).get("series") if record else None
            cache = self._cache_for_series_run(series) if series else self.cache
            self._finalize_fn(self.runs_dir, run_id, record_id, client=client, cache=cache, config=config,
                              tool_config=tool_config, metrics_fn=lambda r: compute_metrics(r, vehicle, cache))
        except RecoveryError as exc:
            outcome, failure = FAILED, exc
            log.warning("run %s: finalization retry refused: %s", run_id, exc)
        except BaseException as exc:  # noqa: BLE001
            if cancel.is_set() and not self._shutting_down:
                outcome = CANCELLED
            elif isinstance(exc, Exception):
                outcome, failure = FAILED, exc
                log.error("run %s: finalization retry failed:\n%s", run_id, traceback.format_exc())
            else:
                outcome = INTERRUPTED
        finally:
            self._complete(run_id, outcome=outcome, failure=failure,
                           cancel_requested=cancel.is_set() and not self._shutting_down)

    # -- A/B benchmark series ------------------------------------------------------------------------------
    # Sequencing lives here (not in a browser session), persisted next to the run records, so it survives a page
    # refresh. Runs execute strictly one after another (the next starts only once the previous one completed, failed
    # or was cancelled); a vehicle is never in two active runs (start() refuses; the series waits and retries).
    # RESTART POLICY: a series cannot outlive its process. On the next manager start, a series whose owner process is
    # gone is marked INTERRUPTED (its unstarted runs NOT_STARTED; the interrupted run itself is reconciled like any
    # run) and its benchmark is written over the runs it finished. It is never resumed automatically: a restart is
    # usually a redeploy, and resuming would mix code / prompt versions inside one A/B comparison.
    def series_dir(self, series_id: str) -> Path:
        if not series_id or "/" in series_id or "\\" in series_id or series_id.startswith("."):
            raise RunRejected(f"invalid series id {series_id!r}")
        return self.runs_dir / SERIES_DIR / series_id

    def _cache_for_series_run(self, series: dict) -> DocumentCache:
        """Private cold cache per planned run, including its own cross-run research memory."""
        index = series.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise RunRejected("Invalid A/B run index.")
        return DocumentCache(self.series_dir(series.get("series_id")) / "cache" / str(index))

    def get_series(self, series_id: str) -> dict | None:
        import json

        try:
            value = json.loads((self.series_dir(series_id) / "series.json").read_text("utf-8"))
        except (OSError, ValueError, RunRejected):
            return None
        return value if isinstance(value, dict) else None

    def list_series(self, limit: int | None = None) -> list[dict]:
        root = self.runs_dir / SERIES_DIR
        if not root.is_dir():
            return []
        out = [s for s in (self.get_series(p.name) for p in root.iterdir() if p.is_dir()) if s]
        out.sort(key=lambda s: (s.get("created_at") or "", s.get("series_id") or ""), reverse=True)
        return out[:limit] if limit else out

    def _update_series(self, series_id: str, mutate: Callable[[dict], None]) -> dict:
        from ..storage.atomic import atomic_write_json

        with self._series_lock:
            series = self.get_series(series_id)
            if series is None:
                raise RunRejected(f"Unknown series {series_id}.")
            mutate(series)
            series["updated_at"] = utc_now()
            atomic_write_json(self.series_dir(series_id) / "series.json", series, durable=True)
        return series

    def series_executing(self, series_id: str) -> bool:
        thread = self._series_threads.get(series_id)
        return bool(thread and thread.is_alive())

    def start_series(self, request: SeriesRequest) -> StartResult:
        """Plan and launch an A/B series (idempotent per `idempotency_key`; one series at a time per process)."""
        from ..storage.atomic import atomic_write_json

        arms = list(request.arm_configs)
        if not request.template.vehicles:
            raise RunRejected("Select at least one vehicle.")
        if not arms:
            raise RunRejected("Select at least one arm.")
        with self._series_lock:
            if self._shutting_down:
                raise RunRejected("The server is shutting down; try again in a moment.")
            key = request.idempotency_key
            if key and key in self._idempotency:
                return StartResult(self._idempotency[key], created=False, message="This series already started.")
            running = [sid for sid in self._series_threads if self.series_executing(sid)]
            if running:
                raise RunRejected(f"An A/B series is already running ({running[0]}).")
            series_id = time.strftime("series-%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{uuid.uuid4().hex[:6]}"
            vehicles = [{"record_id": str(v["upstream_record_id"]), "label": _vehicle_title(v)}
                        for v in request.template.vehicles]
            series = {"schema": "tripy-series/1", "series_id": series_id, "status": SERIES_RUNNING,
                      "label": request.label or f"A/B · {len(vehicles)} vehicle(s)", "created_at": utc_now(),
                      "updated_at": utc_now(), "finished_at": None, "owner": dict(self.owner), "arms": arms,
                      "repeats": int(request.repeats), "vehicles": vehicles, "planned": plan_series(arms, request.repeats),
                      "current_index": 0, "run_ids": [], "error": None, "benchmark": None,
                      "arm_configs": {arm: {"acquisition_mode": cfg.acquisition_mode,
                                            "acquisition_document_card": cfg.acquisition_document_card,
                                            "run_profile": cfg.run_profile}
                                      for arm, cfg in request.arm_configs.items()}}
            atomic_write_json(self.series_dir(series_id) / "series.json", series, durable=True)
            cancel = threading.Event()
            self._series_cancel[series_id] = cancel
            thread = threading.Thread(target=self._drive_series, name=f"tripy-series-{series_id}", daemon=True,
                                      args=(series_id, request, cancel))
            self._series_threads[series_id] = thread
            if key:
                self._idempotency[key] = series_id
            thread.start()
        log.info("series %s started: %d run(s)", series_id, len(series["planned"]))
        return StartResult(series_id, created=True)

    def cancel_series(self, series_id: str) -> bool:
        """Cancel the running run of a series and every planned one."""
        with self._series_lock:
            event = self._series_cancel.get(series_id)
            if event is None or not self.series_executing(series_id):
                return False
            event.set()
        run_id = next((i["run_id"] for i in (self.get_series(series_id) or {}).get("planned") or []
                       if i["status"] == "RUNNING" and i.get("run_id")), None)
        if run_id:
            self.cancel(run_id)
        log.info("series %s: cancellation requested", series_id)
        return True

    def _drive_series(self, series_id: str, request: SeriesRequest, cancel: threading.Event) -> None:
        from dataclasses import replace

        from ..run_profiles import profile_label

        outcome, error = SERIES_COMPLETED, None
        try:
            for item in list((self.get_series(series_id) or {}).get("planned") or []):
                if cancel.is_set() or self._shutting_down:
                    break
                index, arm = item["index"], item["arm"]
                run_request = replace(
                    request.template, agent_cfg=request.arm_configs[arm], scope="Benchmark A/B",
                    label=f"{request.label or 'A/B'} · {profile_label(arm)} · {item['repeat']}/{request.repeats}",
                    idempotency_key=f"{series_id}:{index}",
                    series={"series_id": series_id, "index": index, "repeat": item["repeat"], "arm": arm})
                started = None
                while started is None and not cancel.is_set() and not self._shutting_down:
                    try:
                        started = self.start(run_request)
                    except RunRejected as exc:
                        if exc.existing_run_id is None:      # not a transient conflict: the series cannot go on
                            raise
                        cancel.wait(self.series_poll_s)      # another run is active (same vehicle / run limit)
                if started is None:
                    break
                run_id = started.run_id

                def mark_running(s: dict, run_id=run_id, index=index) -> None:
                    s["planned"][index].update({"run_id": run_id, "status": "RUNNING", "started_at": utc_now()})
                    s["current_index"] = index
                    s["run_ids"] = list(s["run_ids"]) + [run_id]
                self._update_series(series_id, mark_running)
                thread = self._threads.get(run_id)
                cancelled = False
                while thread is not None and thread.is_alive():
                    thread.join(timeout=0.2)
                    if cancel.is_set() and not cancelled:
                        self.cancel(run_id)
                        cancelled = True
                status = (self.get(run_id) or RunRecord(run_id, status=FAILED)).status

                def mark_done(s: dict, index=index, status=status) -> None:
                    s["planned"][index].update({"status": status, "finished_at": utc_now()})
                self._update_series(series_id, mark_done)
            if self._shutting_down:
                outcome = SERIES_INTERRUPTED
            elif cancel.is_set():
                outcome = SERIES_CANCELLED
        except Exception as exc:  # noqa: BLE001 - a series problem is recorded, never raised into the server
            outcome, error = SERIES_FAILED, redact(f"{type(exc).__name__}: {exc}")[:500]
            log.error("series %s failed:\n%s", series_id, traceback.format_exc())
        self._finish_series(series_id, outcome, error)

    def _finish_series(self, series_id: str, outcome: str, error: str | None = None) -> None:
        """Final bookkeeping: unstarted runs, status, and the benchmark over EXACTLY the series' run_ids."""
        def mutate(s: dict) -> None:
            for item in s["planned"]:
                if item["status"] == PLANNED:
                    item["status"] = CANCELLED if outcome == SERIES_CANCELLED else NOT_STARTED
            s["status"], s["finished_at"] = outcome, utc_now()
            s["error"] = s.get("error") or error
        try:
            series = self._update_series(series_id, mutate)
        except Exception:  # noqa: BLE001
            log.error("series %s: could not write its final state", series_id, exc_info=True)
            return
        if series["run_ids"]:
            try:
                from ..diagnostics import PARSER_GAPS_FILE, write_benchmark

                out = self.series_dir(series_id)
                write_benchmark(self.runs_dir, run_ids=series["run_ids"], out_dir=out)
                bench = {"dir": str(out), "files": ["benchmark.json", "per_vehicle.csv", "per_vehicle.jsonl",
                                                    PARSER_GAPS_FILE], "run_ids": list(series["run_ids"]),
                         "complete": outcome == SERIES_COMPLETED, "written_at": utc_now()}
                self._update_series(series_id, lambda s: s.update({"benchmark": bench}))
            except Exception:  # noqa: BLE001
                log.warning("series %s: could not write its benchmark", series_id, exc_info=True)
        log.info("series %s finished: %s", series_id, outcome)

    def reconcile_series(self) -> list[str]:
        """Mark series whose owner process is gone as INTERRUPTED (the restart policy above). Returns their ids."""
        out = []
        for series in self.list_series():
            if series.get("status") != SERIES_RUNNING or self.series_executing(series["series_id"]):
                continue
            if (series.get("owner") or {}).get("boot_id") == self.boot_id:
                continue      # being started by this process right now
            sid = series["series_id"]

            def mutate(s: dict) -> None:
                for item in s["planned"]:
                    if item["status"] == "RUNNING" and item.get("run_id"):
                        item["status"] = (self.get(item["run_id"]) or RunRecord(item["run_id"],
                                                                                status=INTERRUPTED)).status
                s["error"] = s.get("error") or ("The server stopped while this series was running; it is not "
                                                "resumed automatically (a restart may change the code version).")
            try:
                self._update_series(sid, mutate)
            except Exception:  # noqa: BLE001
                log.warning("could not reconcile series %s", sid, exc_info=True)
                continue
            self._finish_series(sid, SERIES_INTERRUPTED)
            out.append(sid)
        if out:
            log.warning("marked %d orphaned series as INTERRUPTED: %s", len(out), ", ".join(out))
        return out

    # -- shutdown -----------------------------------------------------------------------------------------
    def shutdown(self, grace_s: float | None = None) -> None:
        """Interpreter exit (e.g. SIGTERM on redeploy): ask every executing run to stop at its next safe point so
        it persists an `interrupted` result, wait up to `grace_s`, and leave the rest to `reconcile` on the next
        start."""
        if grace_s is None:
            try:
                grace_s = float(os.environ.get("TRIPY_SHUTDOWN_GRACE_S") or DEFAULT_SHUTDOWN_GRACE_S)
            except ValueError:
                grace_s = DEFAULT_SHUTDOWN_GRACE_S
        with self._lock:
            if self._shutting_down and not self._threads:
                return
            self._shutting_down = True
            running = [(rid, t) for rid, t in self._threads.items() if t.is_alive()]
            for rid, _ in running:
                event = self._cancel.get(rid)
                if event is not None:
                    event.set()
        if running:
            log.warning("shutdown: stopping %d active run(s): %s", len(running), ", ".join(r for r, _ in running))
        deadline = time.monotonic() + max(0.0, grace_s)
        for _, thread in running:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


_MANAGERS: dict[tuple[str, str], RunManager] = {}
_MANAGERS_LOCK = threading.Lock()


def _manager_key(paths: DataPaths) -> tuple[str, str]:
    return str(Path(paths.runs_dir).resolve()), str(Path(paths.cache_dir).resolve())


def get_manager(paths: DataPaths, **kwargs) -> RunManager:
    """The process-wide manager for these paths (created once; shared by every browser session)."""
    key = _manager_key(paths)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = RunManager(paths, **kwargs)
            _MANAGERS[key] = manager
        return manager


def existing_manager(paths: DataPaths) -> RunManager | None:
    """The process-wide manager for these paths if one was already created, else None. Never creates one (creating
    a manager reconciles run state on disk); for read-only observers such as the MCP."""
    with _MANAGERS_LOCK:
        return _MANAGERS.get(_manager_key(paths))

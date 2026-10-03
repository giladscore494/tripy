"""Run lifecycle model: statuses, pipeline stages and the durable run record.

A *run* is one research request (one `run_id`, which is the batch id: the run's folder under the runs dir).
It researches one vehicle, or several (manufacturer / full benchmark scope). The research engine's own
per-vehicle artifacts (events.jsonl, result.json, the finalization checkpoint) stay the source of truth for
research; `run_state.json` adds only what the engine does not know: the job lifecycle (queued, running in
which process, heartbeat, cancelled), the request and a final observability report.

Streamlit session state is never the source of truth for any of this.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

SCHEMA = "tripy-run-state/1"

# --- job statuses ------------------------------------------------------------------------------------
QUEUED = "QUEUED"
STARTING = "STARTING"
RESEARCHING = "RESEARCHING"      # primary research = source acquisition
HARVESTING = "HARVESTING"        # deterministic harvest
SWEEPING = "SWEEPING"            # local-first document sweep
RECOVERING = "RECOVERING"        # clustered tail recovery (field detection + recovery)
FINALIZING = "FINALIZING"        # finalizer (also a finalization retry from the durable checkpoint)
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"          # stopped by the user
INTERRUPTED = "INTERRUPTED"      # the server process stopped (redeploy, crash, restart) while it was active

ACTIVE_STATUSES = (QUEUED, STARTING, RESEARCHING, HARVESTING, SWEEPING, RECOVERING, FINALIZING)
TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED, INTERRUPTED)
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES

STATUS_LABELS = {QUEUED: "Queued", STARTING: "Starting", RESEARCHING: "Source acquisition",
                 HARVESTING: "Deterministic harvest", SWEEPING: "Document sweep", RECOVERING: "Tail recovery",
                 FINALIZING: "Finalization", COMPLETED: "Completed", FAILED: "Failed", CANCELLED: "Cancelled",
                 INTERRUPTED: "Interrupted"}

# --- pipeline stages (the user-facing view of the engine's phases) -------------------------------------
STAGES: tuple[tuple[str, str], ...] = (
    ("acquisition", "Source acquisition"),
    ("harvest", "Deterministic harvest"),
    ("sweep", "Document sweep"),
    ("recovery", "Tail recovery"),
    ("finalization", "Finalization"),
)
STAGE_KEYS = tuple(k for k, _ in STAGES)
STAGE_LABELS = dict(STAGES)

# engine phase name (src/agent.py, src/recovery.py) -> stage
ENGINE_PHASE_TO_STAGE = {
    "research": "acquisition",
    "deterministic_harvest": "harvest",
    "document_sweep": "sweep",
    "field_detection": "recovery",
    "field_recovery": "recovery",
    "finalization": "finalization",
    "finalization_repair": "finalization",
    "recovery_finalization": "finalization",
}
STAGE_TO_STATUS = {"acquisition": RESEARCHING, "harvest": HARVESTING, "sweep": SWEEPING, "recovery": RECOVERING,
                   "finalization": FINALIZING}

# engine result statuses (src/agent.py STATUSES + the batch worker's "error")
ENGINE_SUCCESS = ("completed", "max_steps_finalized", "no_new_research_finalized", "acquisition_sufficient_finalized",
                  "under_acquired_finalized", "recovered_finalized")
ENGINE_FAILED = ("research_failed", "finalization_failed", "completed_unparsed", "error")
ENGINE_STOPPED = ("interrupted", "finalization_pending", "incomplete")


def status_for_engine_result(engine_status: str | None, *, cancel_requested: bool = False) -> str:
    """Terminal job status for one vehicle's engine result status."""
    if engine_status in ENGINE_SUCCESS:
        return COMPLETED
    if engine_status in ENGINE_FAILED:
        return FAILED
    if engine_status == "interrupted" and cancel_requested:
        return CANCELLED
    if engine_status in ENGINE_STOPPED:
        return INTERRUPTED
    return FAILED if engine_status else INTERRUPTED


def aggregate_status(vehicle_statuses: list[str], *, cancel_requested: bool = False) -> str:
    """Terminal status of a whole run from its vehicles' terminal statuses."""
    if not vehicle_statuses:
        return CANCELLED if cancel_requested else FAILED
    if cancel_requested and any(s in (CANCELLED, INTERRUPTED) for s in vehicle_statuses):
        return CANCELLED
    if any(s == COMPLETED for s in vehicle_statuses):
        return COMPLETED            # some vehicles may have failed: the record keeps per-vehicle statuses
    if any(s == INTERRUPTED for s in vehicle_statuses):
        return INTERRUPTED
    return FAILED


@dataclass
class RunRecord:
    run_id: str
    status: str = QUEUED
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    stage: str | None = None                    # current pipeline stage while active (see STAGES)
    target: dict = field(default_factory=dict)  # {label, scope, record_ids, vehicles: [{record_id, label}]}
    request: dict = field(default_factory=dict)  # models, search backend, workers ... (never secrets)
    owner: dict = field(default_factory=dict)   # {boot_id, pid, host} of the process executing it
    vehicles: dict = field(default_factory=dict)  # record_id -> {status, stage, engine_status, error}
    error: dict | None = None                   # user-safe failure summary {stage, code, message}
    report: dict | None = None                  # end-of-run observability (src/runstate/report.py)
    cancel_requested: bool = False
    idempotency_key: str | None = None
    jobs: list = field(default_factory=list)    # every execution attempt: research, finalization retries
    notes: list = field(default_factory=list)
    legacy: bool = False                        # derived from a run folder written before run_state.json existed
    schema: str = SCHEMA

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def record_ids(self) -> list[str]:
        return [str(r) for r in self.target.get("record_ids") or []]

    @property
    def label(self) -> str:
        return str(self.target.get("label") or self.run_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        """Tolerant: unknown keys (a newer writer) are ignored, missing keys get defaults, a bad status is kept
        visible as FAILED rather than crashing the history view."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in known}
        if not values.get("run_id"):
            raise ValueError("run state without run_id")
        record = cls(**values)
        if record.status not in ALL_STATUSES:
            record.notes = list(record.notes) + [f"unknown status {record.status!r}"]
            record.status = FAILED
        if not isinstance(record.vehicles, dict):
            record.vehicles = {}
        return record

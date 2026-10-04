"""Request / response contracts of the HTTP API.

Stable, typed top-level shapes for a frontend. Engine-owned structures that already have one representation (pipeline
counters, harvested candidate dicts, evidence items, run reports) are passed through as JSON objects instead of
being re-modelled here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..run_profiles import PRODUCTION, PROFILES

Json = dict[str, Any]


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["tripy"] = "tripy"


# --- runs ------------------------------------------------------------------------------------------------------------

class RunSummary(BaseModel):
    run_id: str
    label: str
    scope: str | None = None
    status: str
    status_label: str
    stage: str | None = None
    profile: str | None = None
    record_ids: list[str]
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    active: bool = Field(description="durable status is an active one (QUEUED ... FINALIZING)")
    executing: bool = Field(description="a worker in this server process is executing it now")
    cancel_requested: bool = False
    legacy: bool = Field(False, description="a run folder without run_state.json (CLI or an older version)")
    elapsed_s: float | None = None


class RunList(BaseModel):
    total: int
    runs: list[RunSummary]


class Stage(BaseModel):
    key: str
    label: str
    state: str
    state_label: str
    duration_s: float | None = None
    note: str | None = None


class Pipeline(BaseModel):
    current_stage: str | None = None
    activity: str | None = None
    engine_status: str | None = None
    stop_reason: str | None = None
    started_at: str | None = None
    last_event_at: str | None = None
    finished_at: str | None = None
    events_seen: int = 0
    research_model: str | None = None
    finalizer_model: str | None = None
    stages: list[Stage]
    counters: Json
    errors: list[Json]


class Failure(BaseModel):
    code: str
    title: str
    stage: str | None = None
    stage_label: str | None = None
    reason: str
    preserved: str | None = None
    actions: list[str] = Field(description="safe recoveries: finalize (from preserved research), restart (new run)")
    technical: str | None = Field(None, description="redacted technical detail; never a traceback")


class VehicleState(BaseModel):
    record_id: str
    title: str
    status: str
    engine_status: str | None = None
    stage: str | None = None
    error: str | None = None
    pipeline: Pipeline
    failure: Failure | None = None
    result_available: bool
    report: Json | None = None


class ProgressCounts(BaseModel):
    vehicles_total: int
    vehicles_finished: int
    vehicles_completed: int


class RunDetail(RunSummary):
    request: Json
    heartbeat_at: str | None = None
    error: Json | None = None
    notes: list[str]
    jobs: list[Json]
    terminal: bool
    completed: bool
    progress: ProgressCounts
    vehicles: list[VehicleState]


class VehicleProgress(BaseModel):
    record_id: str
    title: str
    status: str
    current_stage: str | None = None
    activity: str | None = None
    last_event_at: str | None = None
    resolved_fields: int | None = None
    applicable_fields: int | None = None
    stages: list[Stage]
    counters: Json


class RunProgress(ProgressCounts):
    run_id: str
    status: str
    status_label: str
    stage: str | None = None
    active: bool
    executing: bool
    completed: int = Field(description="vehicles that reached a terminal status")
    total: int = Field(description="vehicles in the run")
    updated_at: str | None = None
    heartbeat_at: str | None = None
    vehicles: list[VehicleProgress]


class EventsPage(BaseModel):
    run_id: str
    record_id: str
    after: int
    next_cursor: int = Field(description="pass as `after` to get only newer events")
    more: bool = Field(description="more complete lines are already available after next_cursor")
    returned: int
    events: list[Json] = Field(description="events.jsonl rows; each carries its 1-based `line`")


class ResultField(BaseModel):
    field: str
    value: Any = None
    unit: str | None = None
    market: str | None = None
    provenance: str | None = None
    notes: Any = None
    evidence_ids: list[Any]
    state: str | None = None


class VehicleResult(BaseModel):
    record_id: str
    title: str
    engine_status: str | None = None
    result_source: str | None = None
    synthesized: bool
    has_output: bool
    stop_reason: str | None = None
    error: Any = None
    recovered: bool
    duration_s: float | None = None
    cost: Any = None
    research_model: str | None = None
    finalizer_model: str | None = None
    prompt_version: str | None = None
    target_market: str | None = None
    finalization: Json | None = None
    summary: Any = None
    fields: list[ResultField]
    conflicts: list[Any]
    additional_findings: list[Any]
    evidence_admission: Json | None = None


class RunResults(BaseModel):
    run_id: str
    available: bool
    reason: str | None = None
    vehicles: list[VehicleResult]


class CandidateField(BaseModel):
    field: str
    group: str | None = None
    state: str | None = None
    evidence_ids: list[Any]
    candidates: list[Json]
    rejected: list[Json]


class RunCandidates(BaseModel):
    run_id: str
    record_id: str
    field: str | None = None
    summary: Json
    fields: list[CandidateField]


class RunEvidence(BaseModel):
    run_id: str
    record_id: str
    field: str | None = None
    summary: Json
    admitted: list[Json]
    rejected: list[Json]


# --- actions ---------------------------------------------------------------------------------------------------------

class StartRun(BaseModel):
    """A research target as the dashboard offers it. Model, search and provider settings are the server's
    configuration (the dashboard's untouched Advanced settings); a client cannot pass any of them."""
    model_config = ConfigDict(extra="forbid")

    scope: Literal["one", "manufacturer", "all"]
    record_id: str | None = Field(None, description="scope=one: the benchmark record id")
    manufacturer: str | None = Field(None, description="scope=manufacturer: the manufacturer name")
    profile: Literal[PROFILES] = PRODUCTION  # type: ignore[valid-type]
    idempotency_key: str | None = Field(None, min_length=1, max_length=200,
                                        description="repeat a request with the same key to get the same run back")


class RestartVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(None, min_length=1, max_length=200)


class Started(BaseModel):
    run_id: str
    created: bool
    message: str = ""
    warnings: list[str] = []
    run: RunSummary | None = None


class ActionAccepted(BaseModel):
    run_id: str
    record_id: str | None = None
    status: str
    message: str


# --- configuration ---------------------------------------------------------------------------------------------------

class Check(BaseModel):
    name: str
    level: Literal["ok", "warning", "error"]
    status: str
    detail: str = ""


class ProfileInfo(BaseModel):
    id: str
    label: str


class ConfigStatus(BaseModel):
    environment: Literal["production", "development"]
    configured: bool = Field(description="no blocking check: a run can be started")
    blocking: list[str]
    checks: list[Check]
    research_model: str | None = None
    finalizer_model: str | None = None
    search_backend: str
    level15_source: Literal["database", "snapshot"]
    storage: Json
    access_control: Json
    mcp_enabled: bool
    max_active_runs: int
    prompt_version: str
    profiles: list[ProfileInfo]
    default_profile: str
    env_overrides: list[Json]


class Vehicle(BaseModel):
    record_id: str
    label: str
    title: str
    manufacturer: str
    model: str
    year: Any = None
    trim: str | None = None
    ordinal: Any = None


class VehicleList(BaseModel):
    vehicles: list[Vehicle]
    manufacturers: list[str]

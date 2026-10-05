"""Request / response contracts of the HTTP API.

Stable, typed top-level shapes for a frontend. Engine-owned structures that already have one representation (pipeline
counters, harvested candidate dicts, evidence items, run reports) are passed through as JSON objects instead of
being re-modelled here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, create_model

from ..run_profiles import ARMS, PRODUCTION, PROFILES
from ..run_settings import RUN_OVERRIDES, RunOverride

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
    resolved_fields_text: str | None = Field(None, description="'26 / 37' from the final report, summed over vehicles")


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


class Notice(BaseModel):
    tone: Literal["info", "warn"]
    text: str
    detail: str | None = None


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
    notices: list[Notice] = []


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
    status_panel: list[list[Any]] = Field([], description="[label, value] rows: totals over every vehicle")


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
    output_source: str | None = None
    no_output_message: str | None = None


class RunResults(BaseModel):
    run_id: str
    available: bool
    reason: str | None = None
    output_source_caption: str | None = None
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


# --- per-run settings ------------------------------------------------------------------------------------------------

def _override_type(spec: RunOverride) -> Any:
    """The typed field of one per-run setting, generated from run_settings.RUN_OVERRIDES (the one source of truth:
    ranges and options are not restated here; run_settings.validate_overrides checks them again)."""
    if spec.kind == "int":
        kind = Annotated[StrictInt, Field(ge=spec.low, le=spec.high)]
    elif spec.kind == "float":
        kind = Annotated[float, Field(ge=spec.low, le=spec.high, strict=True)]
    elif spec.kind == "bool":
        kind = StrictBool
    elif spec.kind == "choice":
        kind = Literal[spec.options]  # type: ignore[valid-type]
    else:
        kind = Annotated[str, Field(max_length=128)]
    return (Optional[kind], Field(None, description=spec.label + (f". {spec.help}" if spec.help else "")))


RunSettingsOverrides = create_model(
    "RunSettingsOverrides", __config__=ConfigDict(extra="forbid"),
    __doc__="Per-run overrides of the dashboard's non-secret Advanced settings (all optional: an absent or null "
            "setting keeps the server default). Endpoints, credentials, the extra request JSON and the process-wide "
            "in-flight limits are server-controlled and cannot be set.",
    **{spec.name: _override_type(spec) for spec in RUN_OVERRIDES})


class RunSettingSpec(BaseModel):
    name: str
    kind: Literal["int", "float", "bool", "choice", "model", "text"]
    label: str
    group: str
    help: str = ""
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str]
    nullable: bool
    allow_empty: bool
    pinned_by_named_profile: bool
    # PR #46: search backend settings only: {backend: why it is unavailable here} and {named profile: its default}
    unavailable_options: dict[str, str] | None = None
    profile_defaults: dict[str, Any] | None = None


class ServerControlled(BaseModel):
    name: str
    label: str
    reason: str


class ModelLimit(BaseModel):
    model: str
    current: int
    maximum: int
    provider_limit: int | None = None


class SearchLimit(BaseModel):
    current: int
    provider_limit: int


class ConcurrencyInfo(BaseModel):
    research_model: ModelLimit | None = None
    finalizer_model: ModelLimit | None = None
    search: SearchLimit
    note: str


class ProfileRef(BaseModel):
    id: str
    label: str


class RunSettingsContract(BaseModel):
    settings: list[RunSettingSpec]
    groups: list[str]
    server_controlled: list[ServerControlled]
    concurrency: ConcurrencyInfo
    named_profiles: list[ProfileRef]
    profile_note: str
    env_overrides: list[Json]


# --- actions ---------------------------------------------------------------------------------------------------------

class StartRun(BaseModel):
    """A research target as the dashboard offers it, with optional per-run settings (the dashboard's non-secret
    Advanced settings, validated by run_settings). Endpoints, credentials and process-wide limits stay the server's."""
    model_config = ConfigDict(extra="forbid")

    scope: Literal["one", "manufacturer", "all"]
    record_id: str | None = Field(None, description="scope=one: the benchmark record id")
    manufacturer: str | None = Field(None, description="scope=manufacturer: the manufacturer name")
    profile: Literal[PROFILES] = PRODUCTION  # type: ignore[valid-type]
    idempotency_key: str | None = Field(None, min_length=1, max_length=200,
                                        description="repeat a request with the same key to get the same run back")
    settings: RunSettingsOverrides | None = Field(None, description="per-run overrides; absent = server defaults")  # type: ignore[valid-type]


class RestartVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(None, min_length=1, max_length=200)


class Started(BaseModel):
    run_id: str
    created: bool
    message: str = ""
    warnings: list[str] = []
    run: RunSummary | None = None
    settings_overridden: list[str] = Field([], description="the per-run settings this request overrode")


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


# --- A/B series ------------------------------------------------------------------------------------------------------

class StartSeries(BaseModel):
    """An A/B benchmark series: chosen benchmark vehicles, runs per arm (1-5), arms from run_profiles.ARMS and the
    optional per-run settings every run of the series uses as its template (the SAME RunSettingsOverrides as
    POST /api/runs; each arm's named profile still pins its own experiment settings). Endpoints, credentials and the
    process-wide limits stay the server's."""
    model_config = ConfigDict(extra="forbid")

    record_ids: list[str] = Field(min_length=1, max_length=200)
    repeats: int = Field(3, ge=1, le=5, strict=True)
    arms: list[Literal[ARMS]] = Field(min_length=1, max_length=len(ARMS))  # type: ignore[valid-type]
    idempotency_key: str | None = Field(None, min_length=1, max_length=200)
    settings: RunSettingsOverrides | None = Field(None, description="per-run overrides for every run of the series; "  # type: ignore[valid-type]
                                                                    "absent = server defaults")


class SeriesItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    index: int
    repeat: int
    arm: str
    arm_label: str
    run_id: str | None = None
    status: str
    started_at: str | None = None
    finished_at: str | None = None


class SeriesCounts(BaseModel):
    planned: int
    done: int
    running: int
    not_run: int


class SeriesProgress(BaseModel):
    total: SeriesCounts
    arms: dict[str, SeriesCounts]


class SeriesVehicle(BaseModel):
    record_id: str
    label: str


class SeriesState(BaseModel):
    series_id: str
    status: str
    label: str
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    arms: list[str]
    arm_labels: dict[str, str]
    repeats: int
    vehicles: list[SeriesVehicle]
    planned: list[SeriesItem]
    current_index: int | None = None
    run_ids: list[str]
    error: str | None = None
    benchmark: Json | None = Field(None, description="files / run ids of the series benchmark (no server paths)")
    arm_configs: Json | None = Field(None, description="acquisition mode / document card / profile of each arm")
    executing: bool = Field(description="this server process is driving the series now")
    series_progress: SeriesProgress


class SeriesList(BaseModel):
    total: int
    executing: str | None = Field(None, description="the series this process is driving now (one at a time)")
    arms: list[ProfileRef] = Field(description="the arms a series may run (run_profiles.ARMS, in run order)")
    default_arms: list[str] = Field(description="the dashboard's default selection")
    max_repeats: int = 5
    series: list[SeriesState]


class SeriesStarted(BaseModel):
    series_id: str
    created: bool
    message: str = ""
    series: SeriesState | None = None
    settings_overridden: list[str] = Field([], description="the per-run settings this request overrode")


# --- documents, binding replay, diagnostics --------------------------------------------------------------------------

class Paging(BaseModel):
    offset: int
    limit: int
    returned: int
    total: int
    remaining: int
    next_offset: int | None = None


class DocumentRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    doc_id: str
    url: str | None = None
    title: str | None = None
    source_domain: str | None = None
    source_authority: str | None = None
    authority_basis: str | None = None
    market: str | None = None
    market_basis: str | None = None
    kind: str | None = None
    doc_type: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    text_chars: int | None = None
    fetched_at: Any = None
    status: Any = None
    used_for_fields: list[str]


class RunDocuments(BaseModel):
    run_id: str
    record_id: str
    documents: list[DocumentRow]
    paging: Paging


class DocumentText(BaseModel):
    doc_id: str
    url: str | None = None
    title: str | None = None
    offset: int
    returned_chars: int
    total_chars: int
    remaining_chars: int
    next_offset: int | None = None
    text: str


class DocumentStructure(BaseModel):
    doc_id: str
    url: str | None = None
    title: str | None = None
    doc_type: str | None = None
    tables: list[Json]
    dom_groups: list[Json]
    variant_map: Json
    paging_applies_to: str
    paging: Paging


class BindingReplay(BaseModel):
    run_id: str
    record_id: str
    field: str | None = None
    summary: Json = Field(description="binding_replay summary: vehicle totals and per-field recorded -> now")
    items: list[Json] = Field(description="one row per replayed admitted evidence item / open-field candidate")
    paging: Paging


class RunDiagnostics(BaseModel):
    run_id: str
    vehicles: int
    aggregate: Json = Field(description="diagnostics.aggregate over the run's vehicles (the benchmark.json content, "
                                        "without per_vehicle)")
    per_vehicle: list[Json] = Field(description="the per_vehicle rows of the benchmark export")

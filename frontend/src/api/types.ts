// TypeScript mirror of the stable Pydantic contracts in src/api/schemas.py.
//
// Engine-owned structures the backend passes through unmodelled (pipeline counters, event rows, candidate and
// evidence internals, replay rows, diagnostics) are typed as JsonObject: the UI reads known keys defensively and
// never assumes a structure the backend does not guarantee.

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: unknown };

export interface ApiErrorBody {
  error: { code: string; message: string; [key: string]: unknown };
}

export interface Health {
  status: "ok";
  service: "tripy";
}

// --- runs -------------------------------------------------------------------------------------------------------------

export interface RunSummary {
  run_id: string;
  label: string;
  scope: string | null;
  status: string;
  status_label: string;
  stage: string | null;
  profile: string | null;
  record_ids: string[];
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  active: boolean;
  executing: boolean;
  cancel_requested: boolean;
  legacy: boolean;
  elapsed_s: number | null;
}

export interface RunList {
  total: number;
  runs: RunSummary[];
}

export interface Stage {
  key: string;
  label: string;
  state: string;
  state_label: string;
  duration_s: number | null;
  note: string | null;
}

export interface Pipeline {
  current_stage: string | null;
  activity: string | null;
  engine_status: string | null;
  stop_reason: string | null;
  started_at: string | null;
  last_event_at: string | null;
  finished_at: string | null;
  events_seen: number;
  research_model: string | null;
  finalizer_model: string | null;
  stages: Stage[];
  counters: JsonObject;
  errors: JsonObject[];
}

export type FailureAction = "finalize" | "restart" | string;

export interface Failure {
  code: string;
  title: string;
  stage: string | null;
  stage_label: string | null;
  reason: string;
  preserved: string | null;
  actions: FailureAction[];
  technical: string | null;
}

export interface VehicleState {
  record_id: string;
  title: string;
  status: string;
  engine_status: string | null;
  stage: string | null;
  error: string | null;
  pipeline: Pipeline;
  failure: Failure | null;
  result_available: boolean;
  report: JsonObject | null;
}

export interface ProgressCounts {
  vehicles_total: number;
  vehicles_finished: number;
  vehicles_completed: number;
}

export interface RunDetail extends RunSummary {
  request: JsonObject;
  heartbeat_at: string | null;
  error: JsonObject | null;
  notes: string[];
  jobs: JsonObject[];
  terminal: boolean;
  completed: boolean;
  progress: ProgressCounts;
  vehicles: VehicleState[];
}

export interface VehicleProgress {
  record_id: string;
  title: string;
  status: string;
  current_stage: string | null;
  activity: string | null;
  last_event_at: string | null;
  resolved_fields: number | null;
  applicable_fields: number | null;
  stages: Stage[];
  counters: JsonObject;
}

export interface RunProgress extends ProgressCounts {
  run_id: string;
  status: string;
  status_label: string;
  stage: string | null;
  active: boolean;
  executing: boolean;
  completed: number;
  total: number;
  updated_at: string | null;
  heartbeat_at: string | null;
  vehicles: VehicleProgress[];
}

/** One events.jsonl row (engine-owned): `line` is the 1-based line number the cursor counts. */
export interface RunEvent extends JsonObject {
  line: number;
  kind?: string;
  seq?: number;
  ts?: string;
  phase?: string;
}

export interface EventsPage {
  run_id: string;
  record_id: string;
  after: number;
  next_cursor: number;
  more: boolean;
  returned: number;
  events: RunEvent[];
}

export interface ResultField {
  field: string;
  value: unknown;
  unit: string | null;
  market: string | null;
  provenance: string | null;
  notes: unknown;
  evidence_ids: unknown[];
  state: string | null;
}

export interface VehicleResult {
  record_id: string;
  title: string;
  engine_status: string | null;
  result_source: string | null;
  synthesized: boolean;
  has_output: boolean;
  stop_reason: string | null;
  error: unknown;
  recovered: boolean;
  duration_s: number | null;
  cost: unknown;
  research_model: string | null;
  finalizer_model: string | null;
  prompt_version: string | null;
  target_market: string | null;
  finalization: JsonObject | null;
  summary: unknown;
  fields: ResultField[];
  conflicts: unknown[];
  additional_findings: unknown[];
  evidence_admission: JsonObject | null;
}

export interface RunResults {
  run_id: string;
  available: boolean;
  reason: string | null;
  vehicles: VehicleResult[];
}

export interface CandidateField {
  field: string;
  group: string | null;
  state: string | null;
  evidence_ids: unknown[];
  candidates: JsonObject[];
  rejected: JsonObject[];
}

export interface RunCandidates {
  run_id: string;
  record_id: string;
  field: string | null;
  summary: JsonObject;
  fields: CandidateField[];
}

export interface RunEvidence {
  run_id: string;
  record_id: string;
  field: string | null;
  summary: JsonObject;
  admitted: JsonObject[];
  rejected: JsonObject[];
}

// --- per-run settings -------------------------------------------------------------------------------------------------

export type SettingKind = "int" | "float" | "bool" | "choice" | "model" | "text";
export type SettingValue = string | number | boolean | null;

export interface RunSettingSpec {
  name: string;
  kind: SettingKind;
  label: string;
  group: string;
  help: string;
  default: SettingValue;
  min: number | null;
  max: number | null;
  step: number | null;
  options: string[];
  nullable: boolean;
  allow_empty: boolean;
  pinned_by_named_profile: boolean;
}

export interface ModelLimit {
  model: string;
  current: number;
  maximum: number;
  provider_limit: number | null;
}

export interface RunSettingsContract {
  settings: RunSettingSpec[];
  groups: string[];
  server_controlled: { name: string; label: string; reason: string }[];
  concurrency: {
    research_model: ModelLimit | null;
    finalizer_model: ModelLimit | null;
    search: { current: number; provider_limit: number };
    note: string;
  };
  named_profiles: { id: string; label: string }[];
  profile_note: string;
  env_overrides: JsonObject[];
}

/** Per-run overrides: keys are RunSettingSpec names (validated server-side against the contract). */
export type RunSettingsOverrides = Record<string, SettingValue>;

// --- actions ----------------------------------------------------------------------------------------------------------

export type Scope = "one" | "manufacturer" | "all";

export interface StartRun {
  scope: Scope;
  record_id?: string;
  manufacturer?: string;
  profile?: string;
  idempotency_key?: string;
  settings?: RunSettingsOverrides;
}

export interface Started {
  run_id: string;
  created: boolean;
  message: string;
  warnings: string[];
  run: RunSummary | null;
  settings_overridden: string[];
}

export interface ActionAccepted {
  run_id: string;
  record_id: string | null;
  status: string;
  message: string;
}

// --- configuration ----------------------------------------------------------------------------------------------------

export interface Check {
  name: string;
  level: "ok" | "warning" | "error";
  status: string;
  detail: string;
}

export interface ConfigStatus {
  environment: "production" | "development";
  configured: boolean;
  blocking: string[];
  checks: Check[];
  research_model: string | null;
  finalizer_model: string | null;
  search_backend: string;
  level15_source: "database" | "snapshot";
  storage: JsonObject;
  access_control: { required?: boolean; configured?: boolean } & JsonObject;
  mcp_enabled: boolean;
  max_active_runs: number;
  prompt_version: string;
  profiles: { id: string; label: string }[];
  default_profile: string;
  env_overrides: JsonObject[];
}

export interface Vehicle {
  record_id: string;
  label: string;
  title: string;
  manufacturer: string;
  model: string;
  year: unknown;
  trim: string | null;
  ordinal: unknown;
}

export interface VehicleList {
  vehicles: Vehicle[];
  manufacturers: string[];
}

// --- A/B series -------------------------------------------------------------------------------------------------------

export interface StartSeries {
  record_ids: string[];
  repeats: number;
  arms: string[];
  idempotency_key?: string;
}

export interface SeriesItem {
  index: number;
  repeat: number;
  arm: string;
  arm_label: string;
  run_id: string | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface SeriesCounts {
  planned: number;
  done: number;
  running: number;
  not_run: number;
}

export interface SeriesState {
  series_id: string;
  status: string;
  label: string;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  arms: string[];
  arm_labels: Record<string, string>;
  repeats: number;
  vehicles: { record_id: string; label: string }[];
  planned: SeriesItem[];
  current_index: number | null;
  run_ids: string[];
  error: string | null;
  benchmark: { files?: string[]; run_ids?: string[]; complete?: boolean; written_at?: string } | null;
  arm_configs: JsonObject | null;
  executing: boolean;
  series_progress: { total: SeriesCounts; arms: Record<string, SeriesCounts> };
}

export interface SeriesList {
  total: number;
  executing: string | null;
  arms: { id: string; label: string }[];
  default_arms: string[];
  max_repeats: number;
  series: SeriesState[];
}

export interface SeriesStarted {
  series_id: string;
  created: boolean;
  message: string;
  series: SeriesState | null;
}

// --- documents, binding replay, diagnostics ---------------------------------------------------------------------------

export interface Paging {
  offset: number;
  limit: number;
  returned: number;
  total: number;
  remaining: number;
  next_offset: number | null;
}

export interface DocumentRow extends JsonObject {
  doc_id: string;
  url: string | null;
  title: string | null;
  source_domain?: string | null;
  source_authority?: string | null;
  authority_basis?: string | null;
  market: string | null;
  market_basis: string | null;
  kind: string | null;
  doc_type: string | null;
  content_type: string | null;
  size_bytes: number | null;
  text_chars: number | null;
  fetched_at: unknown;
  status: unknown;
  used_for_fields: string[];
}

export interface RunDocuments {
  run_id: string;
  record_id: string;
  documents: DocumentRow[];
  paging: Paging;
}

export interface DocumentText {
  doc_id: string;
  url: string | null;
  title: string | null;
  offset: number;
  returned_chars: number;
  total_chars: number;
  remaining_chars: number;
  next_offset: number | null;
  text: string;
}

export interface DocumentStructure {
  doc_id: string;
  url: string | null;
  title: string | null;
  doc_type: string | null;
  tables: JsonObject[];
  dom_groups: JsonObject[];
  variant_map: JsonObject;
  paging_applies_to: string;
  paging: Paging;
}

export interface BindingReplay {
  run_id: string;
  record_id: string;
  field: string | null;
  summary: JsonObject;
  items: JsonObject[];
  paging: Paging;
}

export interface RunDiagnostics {
  run_id: string;
  vehicles: number;
  aggregate: JsonObject;
  per_vehicle: JsonObject[];
}

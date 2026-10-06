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
  /** "26 / 37" from the final report, summed over vehicles */
  resolved_fields_text?: string | null;
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
  notices?: Notice[];
}

export interface Notice {
  tone: "info" | "warn";
  text: string;
  detail?: string | null;
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
  /** [label, value] rows: totals over every vehicle */
  status_panel?: [string, unknown][];
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
  output_source?: string | null;
  no_output_message?: string | null;
}

export interface RunResults {
  run_id: string;
  available: boolean;
  reason: string | null;
  output_source_caption?: string | null;
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
  /** Search backend settings only: backends unavailable on this server (their key is not set) and why. */
  unavailable_options?: Record<string, string> | null;
  /** Search backend settings only: each named profile's default. */
  profile_defaults?: Record<string, SettingValue> | null;
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

export type Scope = "one" | "manufacturer" | "all" | "set";

export interface StartRun {
  scope: Scope;
  record_id?: string;
  record_ids?: string[];
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
  /** the same typed per-run settings as POST /api/runs; the template of every run of the series */
  settings?: RunSettingsOverrides;
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
  settings_overridden?: string[];
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

// --- technical views, benchmark metrics, multi-run exports (src/api/routes/technical.py) ------------------------------

export type Row = Record<string, unknown>;
export type Pair = [string, unknown];

export interface BriefPairs {
  acquisition: Pair[];
  sweep: Pair[] | null;
  sweep_skipped: string | null;
}

export interface DetailedDiagnostics {
  schema: unknown;
  configured_models: Record<string, unknown>;
  summary_text: string | null;
  turns: Row[];
  sweep_calls: Row[];
  sweep_fields: Row[];
  raw: JsonObject;
}

export interface FieldRecoveryView {
  requested_fields: number;
  available: boolean;
  error?: string | null;
  metrics?: { failed_after_primary: number; retried: number; recovered: number; retry_attempts: number };
  turns?: string;
  stopped?: string | null;
  not_attempted_due_to_budget?: string[];
  cut_short_by_budget?: unknown;
  mode?: string | null;
  cluster?: Row | null;
  triage?: Row[];
  fields?: Row[];
  attempts?: Row[];
  prior_excerpts_summary?: { items: number; chars: number; attempts_with_prior_excerpts: number };
  prior_excerpts?: { field: string; attempt: number; items: number; chars: number; excerpts: unknown }[];
}

export interface VehicleTechnical {
  run_id: string;
  record_id: string;
  title: string;
  available: boolean;
  reason: string | null;
  brief: BriefPairs;
  diagnostics?: DetailedDiagnostics | null;
  status?: string | null;
  duration_s?: number | null;
  synthesized?: boolean;
  recovered?: boolean;
  has_output?: boolean;
  has_research?: boolean;
  no_output_message?: string | null;
  error?: unknown;
  notices?: Notice[];
  human?: {
    is_object: boolean; variant_identity?: JsonObject | null; alternatives?: Row[];
    provenance?: { label: string; text: string }[]; level3?: unknown; other_keys?: JsonObject | null;
    research_trace?: string[];
  } | null;
  raw?: { parse_note: unknown; output: unknown; raw_final_text: string };
  partial_research?: {
    evidence_count: number; candidate_facts: Row[]; fields_with_multiple_stored_values: string[];
    model_noted_conflicts: { source: unknown; text: string }[]; response_excerpts: Row[];
    last_model_content: string | null; urls: string[]; documents: Row[];
    target_status: { no_evidence: string[]; unresolved: string[]; level3: string[] };
    error: unknown; api_errors: Row[]; bundle: JsonObject | null;
  };
  evidence_admission?: JsonObject;
  consistency_checks?: Row[];
  tool_calls?: Row[];
  model_responses?: { seq: unknown; phase: unknown; tool_calls: unknown; prompt_tokens: unknown;
                      completion_tokens: unknown; latency_ms: unknown; content: string | null;
                      reasoning_content: string | null }[];
  level15_input?: JsonObject;
  config?: { api_error: unknown; research_model: string | null; finalizer_model: string | null;
             stop_reason: string | null; prompt_version: string | null; finalization: JsonObject | null;
             effective_config: JsonObject; usage_known: boolean; usage: JsonObject };
  api_attempts?: { api_stats: JsonObject; api_errors: Row[] };
  field_recovery?: FieldRecoveryView;
  candidates?: { candidate_summary: JsonObject; primary_research: unknown; document_sweep: unknown };
  identity_anchors?: IdentityAnchors | null;
}

// identity anchors PR: the approval route / code family, the open-data match per source and the offers per field
export interface IdentityAnchors {
  fingerprint?: JsonObject & { approval_route?: { route?: string | null } | null; code_family?: number[];
                               equivalent_codes?: number[]; type_code?: string | null };
  open_data?: {
    mode?: string | null; route?: string | null; route_detail?: string | null; level?: string | null;
    designation?: string | null; lead_source?: string | null; error?: string | null;
    sources: Row[]; offers: Row[];
  };
  spec_sheets?: JsonObject;
  open_data_not_built?: string[];
}

export interface LiveView {
  run_id: string;
  record_id: string;
  active: boolean;
  columns: string[];
  fields: unknown[][];
  lines: string[];
  brief: BriefPairs;
}

export interface RunBenchmark {
  run_id: string;
  available: boolean;
  reason: string | null;
  vehicles: number;
  aggregate: JsonObject | null;
  columns: string[];
  rows: Row[];
  tool_usage: Row[];
  cost_note: string | null;
}

export interface BenchmarkBatches {
  batches: Row[];
  columns: string[];
}

export interface BenchmarkSummary {
  run_ids: string[];
  vehicles: number;
  mean_turns: number | null;
  mean_searches: number | null;
  sweep_resolution_rate: number | null;
  files: string[];
}

export interface CacheDocuments {
  total: number;
  offset: number;
  returned: number;
  remaining: number;
  next_offset: number | null;
  stats: Record<string, number>;
  documents: Row[];
}

export interface Reachability {
  checked: boolean;
  reachable: boolean | null;
  detail: string;
}

// --- search bake-off (PR #46, S4) -------------------------------------------------------------------------------------

export interface BakeoffBackendOption {
  name: string;
  label: string;
  available: boolean;
  reason: string;
  key_env: string | null;
}

export interface BakeoffOptions {
  backends: BakeoffBackendOption[];
  records: { record_id: string; label: string }[];
  defaults: { backends: string[]; record_ids: string[]; fetch_top: boolean };
  note: string;
}

export interface BakeoffJob {
  bakeoff_id: string;
  label: string;
  status: string;
  executing: boolean;
  config: { backends: string[]; records: string[]; fetch_top: number; label: string };
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  progress: { done: number; total: number | null };
  error: string | null;
}

export interface BakeoffMetrics {
  backend: string;
  queries: number;
  errors: number;
  results: number;
  full_path_ratio: number | null;
  on_site_ratio: number | null;
  version_url_hit: number;
  version_url_hit_ratio: number | null;
  first_version_rank: number | null;
  israeli_domain_share: number | null;
  unique_urls: number;
  candidates: number;
  fetched: number;
  accepted: number;
  rejected: number;
  rejected_by_reason: Record<string, number>;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  usd: number;
  usd_per_1000_queries: number | null;
  usd_per_record: number | null;
  accepted_per_usd: number | null;
}

export interface BakeoffRecordRow {
  record_id: string;
  vehicle: string | null;
  backend: string;
  queries: number;
  results: number;
  version_url_hit: boolean;
  first_version_rank: number | null;
  candidate_url: string | null;
  candidate_score: number | null;
  verdict: string | null;
  verdict_reason: string | null;
  usd: number;
}

export interface BakeoffSummary {
  schema: string;
  status: string;
  records: number;
  queries: number;
  metrics: BakeoffMetrics[];
  note: string;
}

export interface BakeoffList {
  bakeoffs: BakeoffJob[];
}

export interface BakeoffDetail {
  bakeoff: BakeoffJob;
  summary: BakeoffSummary | null;
  records: BakeoffRecordRow[];
}

export interface StartBakeoff {
  backends: string[];
  record_ids?: string[];
  fetch_top: boolean;
  label?: string;
}

export interface BakeoffStarted {
  bakeoff_id: string;
  bakeoff: BakeoffJob | null;
}

// --- the live MILO catalog (PR #47, B1 / B2) ----------------------------------------------------------------------

export interface DerivedIndexStatus {
  available: boolean;
  building: boolean;
  path: string;
  interval_days: number;
  status?: string;
  built_at?: string | null;
  rows?: number | null;
  entries?: number | null;
  error?: string | null;
  reason?: string;
}

export interface CatalogStatus {
  mode: "live" | "snapshot";
  browser_enabled: boolean;
  label: string;
  max_set: number;
  max_limit: number;
  derived_index: DerivedIndexStatus;
}

export interface CatalogItem {
  record_id: string;
  manufacturer: string;
  model: string;
  year: number | null;
  trim: string | null;
  model_code: string | null;
  degem_cd: number | null;
  body: string | null;
  propulsion: string | null;
  drivetrain: string | null;
  power_hp: number | null;
  engine_cc: number | null;
  label: string;
}

export interface CatalogPage {
  items: CatalogItem[];
  total: number;
  limit: number;
  offset: number;
  filters: Record<string, unknown>;
}

export interface CatalogQuery {
  manufacturer?: string;
  model?: string;
  year?: number;
  trim?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

// --- the Data page (identity anchors PR: open datasets + production source policy; src/api/routes/data.py) ---------
export interface DatasetBuild {
  status?: string;
  reason?: string;
  rows?: number;
  built_at?: string;
  attempted_at?: string;
  error?: string;
  report?: Record<string, unknown>;
}

export interface DatasetRow {
  dataset: string;
  label: string | null;
  source_url: string | null;
  market: string | null;
  routes: string[] | null;
  identity_only: boolean;
  policy: string | null;
  licence: string | null;
  attribution: string | null;
  schema_verified: boolean | string | null;
  snapshot_built_at: string | null;
  snapshot_rows: number | null;
  last_build: DatasetBuild | null;
  progress?: DatasetProgress | null;
  absent_columns?: string[] | Record<string, string[]> | null;
  snapshot_size_bytes?: number | null;
  build_duration_s?: number | null;
  last_file_year?: number | null;
}

// the build state of one dataset in this server process (the open-data job)
export interface DatasetProgress {
  state: "queued" | "building" | "built" | "stopped" | "failed" | "skipped" | "already_running" | string;
  detail?: string | null;
  at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface DatasetsStatus {
  folder: string;
  building: string | null;
  first_check_at?: string | null;
  scheduled?: boolean;
  interval_days: number;
  datasets: DatasetRow[];
}

export interface PolicyEntry {
  id: string;
  kind: string;
  domains: string[];
  policy: "allowed" | "identity_only" | "blocked";
  licence: string | null;
  attribution: string | null;
  bulk_store: boolean | null;
  terms_clause: string | null;
  terms_clause_source: string | null;
  checked_at: string | null;
  changed_at?: string | null;
  changed_by?: string | null;
  overlay?: boolean | null;
  note?: string | null;
}

export interface PolicyChangeLog {
  at: string;
  domain: string;
  from: string;
  to: string;
  terms_clause: string | null;
  checked_at: string | null;
  changed_by: string;
}

export interface PolicyTable {
  version: string;
  unlisted: string;
  overlay_path: string | null;
  entries: PolicyEntry[];
  changes: PolicyChangeLog[];
}

export interface PolicyChange {
  domain: string;
  policy: "allowed" | "blocked";
  terms_clause?: string | null;
  checked_at?: string | null;
}

// Typed functions for every backend route the workspace uses (src/api/routes/*). Nothing here reads /data: all
// state comes from the FastAPI backend.

import { download, request } from "./client";
import type {
  ActionAccepted,
  BakeoffDetail,
  BakeoffList,
  BakeoffOptions,
  BakeoffStarted,
  BenchmarkBatches,
  BenchmarkSummary,
  BindingReplay,
  CacheDocuments,
  CatalogPage,
  CatalogQuery,
  CatalogStatus,
  DatasetsStatus, OlderThanPlan, RetentionOverview, StorageStatus,
  DerivedIndexStatus,
  LiveView,
  Reachability,
  RunBenchmark,
  ConfigStatus,
  DocumentStructure,
  DocumentText,
  EventsPage,
  Health,
  RunCandidates,
  RunDetail,
  RunDiagnostics,
  RunDocuments,
  RunEvidence,
  RunList,
  RunProgress,
  RunResults,
  PolicyChange,
  PolicyChangeLog,
  PolicyTable,
  ShadowReport,
  RunSettingsContract,
  SeriesList,
  SeriesStarted,
  SeriesState,
  StartBakeoff,
  StartRun,
  StartSeries,
  Started,
  VehicleList,
  VehicleTechnical,
} from "./types";

const enc = encodeURIComponent;
type Opts = { signal?: AbortSignal };

export const api = {
  health: (o: Opts = {}) => request<Health>("/health", o),
  configStatus: (o: Opts = {}) => request<ConfigStatus>("/api/config/status", o),
  vehicles: (o: Opts = {}) => request<VehicleList>("/api/vehicles", o),
  runSettings: (o: Opts = {}) => request<RunSettingsContract>("/api/run-settings", o),

  runs: (limit?: number, o: Opts = {}) => request<RunList>("/api/runs", { ...o, query: { limit } }),
  run: (runId: string, o: Opts = {}) => request<RunDetail>(`/api/runs/${enc(runId)}`, o),
  progress: (runId: string, o: Opts = {}) => request<RunProgress>(`/api/runs/${enc(runId)}/progress`, o),
  events: (runId: string, after: number, recordId?: string, limit = 200, o: Opts = {}) =>
    request<EventsPage>(`/api/runs/${enc(runId)}/events`, { ...o, query: { after, limit, record_id: recordId } }),
  results: (runId: string, o: Opts = {}) => request<RunResults>(`/api/runs/${enc(runId)}/results`, o),
  candidates: (runId: string, recordId?: string, o: Opts = {}) =>
    request<RunCandidates>(`/api/runs/${enc(runId)}/candidates`, { ...o, query: { record_id: recordId } }),
  evidence: (runId: string, recordId?: string, o: Opts = {}) =>
    request<RunEvidence>(`/api/runs/${enc(runId)}/evidence`, { ...o, query: { record_id: recordId } }),
  documents: (runId: string, recordId?: string, offset = 0, limit = 200, o: Opts = {}) =>
    request<RunDocuments>(`/api/runs/${enc(runId)}/documents`,
      { ...o, query: { record_id: recordId, offset, limit } }),
  documentText: (docId: string, offset = 0, limit = 20000, o: Opts = {}) =>
    request<DocumentText>(`/api/documents/${enc(docId)}/text`, { ...o, query: { offset, limit } }),
  documentStructure: (docId: string, runId?: string, recordId?: string, offset = 0, limit = 20, o: Opts = {}) =>
    request<DocumentStructure>(`/api/documents/${enc(docId)}/structure`,
      { ...o, query: { run_id: runId, record_id: runId ? recordId : undefined, offset, limit } }),
  bindingReplay: (runId: string, recordId?: string, field?: string, offset = 0, limit = 500, o: Opts = {}) =>
    request<BindingReplay>(`/api/runs/${enc(runId)}/binding-replay`,
      { ...o, query: { record_id: recordId, field, offset, limit } }),
  diagnostics: (runId: string, o: Opts = {}) => request<RunDiagnostics>(`/api/runs/${enc(runId)}/diagnostics`, o),
  technical: (runId: string, recordId: string, o: Opts = {}) =>
    request<VehicleTechnical>(`/api/runs/${enc(runId)}/vehicles/${enc(recordId)}/technical`, o),
  live: (runId: string, recordId?: string, o: Opts = {}) =>
    request<LiveView>(`/api/runs/${enc(runId)}/live`, { ...o, query: { record_id: recordId } }),
  benchmark: (runId: string, o: Opts = {}) => request<RunBenchmark>(`/api/runs/${enc(runId)}/benchmark`, o),
  benchmarkBatches: (o: Opts = {}) => request<BenchmarkBatches>("/api/benchmark/batches", o),
  benchmarkSummary: (runIds: string[], o: Opts = {}) =>
    request<BenchmarkSummary>("/api/benchmark/summary", { ...o, query: { run_id: runIds } }),
  cacheDocuments: (offset = 0, limit = 100, o: Opts = {}) =>
    request<CacheDocuments>("/api/cache/documents", { ...o, query: { offset, limit } }),
  reachability: (o: Opts = {}) => request<Reachability>("/api/config/reachability", o),

  startRun: (body: StartRun) => request<Started>("/api/runs", { method: "POST", body }),
  cancelRun: (runId: string) => request<ActionAccepted>(`/api/runs/${enc(runId)}/cancel`, { method: "POST" }),
  // retention: delete a run folder (refused while it executes) / mark it keep
  deleteRun: (runId: string) =>
    request<{ run_id: string; deleted: boolean; bytes_freed: number }>(`/api/runs/${enc(runId)}`, { method: "DELETE" }),
  keepRun: (runId: string, keep: boolean) =>
    request<{ run_id: string; pinned: boolean }>(`/api/runs/${enc(runId)}/keep`, { method: "POST", body: { keep } }),
  finalizeVehicle: (runId: string, recordId: string) =>
    request<ActionAccepted>(`/api/runs/${enc(runId)}/vehicles/${enc(recordId)}/finalize`, { method: "POST" }),
  restartVehicle: (runId: string, recordId: string, idempotencyKey: string) =>
    request<Started>(`/api/runs/${enc(runId)}/vehicles/${enc(recordId)}/restart`,
      { method: "POST", body: { idempotency_key: idempotencyKey } }),

  series: (limit?: number, o: Opts = {}) => request<SeriesList>("/api/series", { ...o, query: { limit } }),
  seriesState: (seriesId: string, o: Opts = {}) => request<SeriesState>(`/api/series/${enc(seriesId)}`, o),
  startSeries: (body: StartSeries) => request<SeriesStarted>("/api/series", { method: "POST", body }),
  cancelSeries: (seriesId: string) => request<SeriesState>(`/api/series/${enc(seriesId)}/cancel`, { method: "POST" }),

  bakeoffOptions: (o: Opts = {}) => request<BakeoffOptions>("/api/bakeoffs/options", o),
  bakeoffs: (o: Opts = {}) => request<BakeoffList>("/api/bakeoffs", o),
  bakeoff: (bakeoffId: string, o: Opts = {}) => request<BakeoffDetail>(`/api/bakeoffs/${enc(bakeoffId)}`, o),
  startBakeoff: (body: StartBakeoff) => request<BakeoffStarted>("/api/bakeoffs", { method: "POST", body }),
  cancelBakeoff: (bakeoffId: string) =>
    request<{ cancelled: boolean }>(`/api/bakeoffs/${enc(bakeoffId)}/cancel`, { method: "POST" }),

  // the live MILO catalog (read-only; src/api/routes/catalog.py)
  catalogStatus: (o: Opts = {}) => request<CatalogStatus>("/api/catalog/status", o),
  catalogSearch: (query: CatalogQuery, o: Opts = {}) =>
    request<CatalogPage>("/api/catalog", { ...o, query: { ...query } }),
  catalogManufacturers: (o: Opts = {}) =>
    request<{ manufacturers: { manufacturer: string; variants: number }[] }>("/api/catalog/manufacturers", o),
  catalogModels: (manufacturer: string, o: Opts = {}) =>
    request<{ models: { model: string; variants: number }[] }>("/api/catalog/models", { ...o, query: { manufacturer } }),
  catalogYears: (manufacturer: string, model: string, o: Opts = {}) =>
    request<{ years: { year: number; variants: number }[] }>("/api/catalog/years",
      { ...o, query: { manufacturer, model } }),
  catalogTrims: (manufacturer: string, model: string, year: number, o: Opts = {}) =>
    request<{ trims: { trim: string | null; variants: number }[] }>("/api/catalog/trims",
      { ...o, query: { manufacturer, model, year } }),
  rebuildCatalogIndex: () =>
    request<{ started: boolean; status: DerivedIndexStatus; message: string }>("/api/catalog/index/rebuild",
      { method: "POST" }),

  // the Data page: the committed open-data snapshots, the data volume and the production source policy
  // (src/api/routes/data.py)
  dataDatasets: (o: Opts = {}) => request<DatasetsStatus>("/api/data/datasets", o),
  dataStorage: (o: Opts = {}) => request<StorageStatus>("/api/data/storage", o),
  dataRetention: (o: Opts = {}) => request<RetentionOverview>("/api/data/retention", o),
  setRetention: (keepNewest: number) =>
    request<RetentionOverview["settings"]>("/api/data/retention", { method: "PUT", body: { keep_newest: keepNewest } }),
  compactRuns: () =>
    request<{ runs: { run_id: string; bytes_freed: number }[]; bytes_freed: number; storage: StorageStatus }>(
      "/api/data/retention/compact", { method: "POST", body: { confirm: true } }),
  olderThan: (days: number) => request<OlderThanPlan>("/api/data/retention/older-than", { query: { days } }),
  deleteOlder: (days: number) =>
    request<{ runs: string[]; bytes_freed: number; storage: StorageStatus }>("/api/data/retention/delete-older",
      { method: "POST", body: { days, confirm: true } }),
  cleanCache: () =>
    request<{ documents: number; bytes_freed: number; storage: StorageStatus }>("/api/data/retention/clean-cache",
      { method: "POST", body: { confirm: true } }),
  deleteOpenData: () =>
    request<{ deleted: boolean; bytes_freed: number; path: string; storage: StorageStatus }>(
      "/api/data/storage/delete-open-data", { method: "POST", body: { confirm: true } }),
  shadowReport: (o: Opts = {}) => request<{ report: ShadowReport | null }>("/api/data/open-data/shadow-report", o),
  runShadowReport: () =>
    request<{ report: ShadowReport }>("/api/data/open-data/shadow-report", { method: "POST" }),
  dataPolicy: (o: Opts = {}) => request<PolicyTable>("/api/data/policy", o),
  changePolicy: (body: PolicyChange) =>
    request<{ change: PolicyChangeLog; policy: PolicyTable }>("/api/data/policy", { method: "POST", body }),
};

export type RunExport = "candidates.csv" | "per_vehicle.csv" | "benchmark.json";

export const exportsApi = {
  run: (runId: string, name: RunExport, recordId?: string) =>
    download(`/api/runs/${enc(runId)}/export/${name}`, `tripy_${name}`,
      name === "candidates.csv" ? { record_id: recordId } : undefined),
  series: (seriesId: string, name: string) =>
    download(`/api/series/${enc(seriesId)}/export/${enc(name)}`, `${seriesId}_${name}`),
  bakeoff: (bakeoffId: string, name: "metrics.csv" | "records.csv" | "summary.json") =>
    download(`/api/bakeoffs/${enc(bakeoffId)}/export/${name}`, `${bakeoffId}-${name}`),
  /** The Benchmark diagnostics files over several runs (repeatable run_id; run ids are not secrets). */
  benchmark: (runIds: string[], name: BenchmarkExport) =>
    download(`/api/benchmark/export/${enc(name)}`, name === "binding_replay_items.jsonl" ? name : `tripy_${name}`,
      { run_id: runIds }),
};

export type BenchmarkExport = "benchmark.json" | "per_vehicle.csv" | "parser_gaps.jsonl" | "binding_replay_items.jsonl";

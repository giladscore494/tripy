// Typed functions for every backend route the workspace uses (src/api/routes/*). Nothing here reads /data: all
// state comes from the FastAPI backend.

import { download, request } from "./client";
import type {
  ActionAccepted,
  BenchmarkBatches,
  BenchmarkSummary,
  BindingReplay,
  CacheDocuments,
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
  RunSettingsContract,
  SeriesList,
  SeriesStarted,
  SeriesState,
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
  finalizeVehicle: (runId: string, recordId: string) =>
    request<ActionAccepted>(`/api/runs/${enc(runId)}/vehicles/${enc(recordId)}/finalize`, { method: "POST" }),
  restartVehicle: (runId: string, recordId: string, idempotencyKey: string) =>
    request<Started>(`/api/runs/${enc(runId)}/vehicles/${enc(recordId)}/restart`,
      { method: "POST", body: { idempotency_key: idempotencyKey } }),

  series: (limit?: number, o: Opts = {}) => request<SeriesList>("/api/series", { ...o, query: { limit } }),
  seriesState: (seriesId: string, o: Opts = {}) => request<SeriesState>(`/api/series/${enc(seriesId)}`, o),
  startSeries: (body: StartSeries) => request<SeriesStarted>("/api/series", { method: "POST", body }),
  cancelSeries: (seriesId: string) => request<SeriesState>(`/api/series/${enc(seriesId)}/cancel`, { method: "POST" }),
};

export type RunExport = "candidates.csv" | "per_vehicle.csv" | "benchmark.json";

export const exportsApi = {
  run: (runId: string, name: RunExport, recordId?: string) =>
    download(`/api/runs/${enc(runId)}/export/${name}`, `tripy_${name}`,
      name === "candidates.csv" ? { record_id: recordId } : undefined),
  series: (seriesId: string, name: string) =>
    download(`/api/series/${enc(seriesId)}/export/${enc(name)}`, `${seriesId}_${name}`),
  /** The Benchmark diagnostics files over several runs (repeatable run_id; run ids are not secrets). */
  benchmark: (runIds: string[], name: BenchmarkExport) =>
    download(`/api/benchmark/export/${enc(name)}`, name === "binding_replay_items.jsonl" ? name : `tripy_${name}`,
      { run_id: runIds }),
};

export type BenchmarkExport = "benchmark.json" | "per_vehicle.csv" | "parser_gaps.jsonl" | "binding_replay_items.jsonl";

// Test data: JSON captured from the real FastAPI backend (a dev data directory with the PR #40 fixture run and live
// runs against scripts/fake_glm_server.py), trimmed; plus small builders for active / failed / series states.
import type {
  BindingReplay, ConfigStatus, DocumentStructure, DocumentText, EventsPage, RunCandidates, RunDetail, RunDiagnostics,
  RunDocuments, RunEvent, RunEvidence, RunList, RunProgress, RunResults, RunSettingsContract, RunSummary, SeriesList,
  SeriesState, VehicleList, LiveView, RunBenchmark, VehicleTechnical,
} from "../api/types";
import benchmarkJson from "./fixtures/benchmark.json";
import liveJson from "./fixtures/live.json";
import technicalJson from "./fixtures/technical.json";
import bindingReplayJson from "./fixtures/binding_replay.json";
import candidatesJson from "./fixtures/candidates.json";
import configJson from "./fixtures/config.json";
import diagnosticsJson from "./fixtures/diagnostics.json";
import documentStructureJson from "./fixtures/document_structure.json";
import documentTextJson from "./fixtures/document_text.json";
import documentsJson from "./fixtures/documents.json";
import evidenceJson from "./fixtures/evidence.json";
import resultsJson from "./fixtures/results.json";
import completedJson from "./fixtures/run_detail_completed.json";
import interruptedJson from "./fixtures/run_detail_interrupted.json";
import multiJson from "./fixtures/run_detail_multi.json";
import contractJson from "./fixtures/run_settings.json";
import seriesListJson from "./fixtures/series_list.json";
import vehiclesJson from "./fixtures/vehicles.json";

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export const config = configJson as unknown as ConfigStatus;
export const vehicles = vehiclesJson as unknown as VehicleList;
export const contract = contractJson as unknown as RunSettingsContract;
export const completedRun = completedJson as unknown as RunDetail;
export const multiRun = multiJson as unknown as RunDetail;
export const interruptedRun = interruptedJson as unknown as RunDetail;
export const results = resultsJson as unknown as RunResults;
export const candidates = candidatesJson as unknown as RunCandidates;
export const evidence = evidenceJson as unknown as RunEvidence;
export const documents = documentsJson as unknown as RunDocuments;
export const documentText = documentTextJson as unknown as DocumentText;
export const documentStructure = documentStructureJson as unknown as DocumentStructure;
export const bindingReplay = bindingReplayJson as unknown as BindingReplay;
export const diagnostics = diagnosticsJson as unknown as RunDiagnostics;
export const technical = technicalJson as unknown as VehicleTechnical;
export const live = liveJson as unknown as LiveView;
export const benchmark = benchmarkJson as unknown as RunBenchmark;

export const ACTIVE_ID = "20261005T100000Z-glm-5.3-flash-one";
export const MULTI_ID = multiRun.run_id;

function summaryOf(detail: RunDetail): RunSummary {
  const { run_id, label, scope, status, status_label, stage, profile, record_ids, created_at, updated_at, started_at,
          finished_at, active, executing, cancel_requested, legacy, elapsed_s } = detail;
  return { run_id, label, scope, status, status_label, stage, profile, record_ids, created_at, updated_at, started_at,
           finished_at, active, executing, cancel_requested, legacy, elapsed_s };
}

/** An active one-vehicle run: the completed fixture with a live status and a running acquisition stage. */
export function activeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  const run = clone(completedRun);
  run.run_id = ACTIVE_ID;
  run.status = "RESEARCHING";
  run.status_label = "Source acquisition";
  run.active = true;
  run.executing = true;
  run.terminal = false;
  run.completed = false;
  run.finished_at = null;
  run.progress = { vehicles_total: 1, vehicles_finished: 0, vehicles_completed: 0 };
  run.vehicles[0].status = "RESEARCHING";
  run.vehicles[0].failure = null;
  run.vehicles[0].report = null;
  run.vehicles[0].result_available = false;
  return { ...run, ...overrides };
}

export function progressOf(run: RunDetail, overrides: Partial<RunProgress> = {}): RunProgress {
  return {
    run_id: run.run_id, status: run.status, status_label: run.status_label, stage: run.stage, active: run.active,
    executing: run.executing, completed: run.progress.vehicles_finished, total: run.progress.vehicles_total,
    ...run.progress, updated_at: run.updated_at, heartbeat_at: run.heartbeat_at,
    vehicles: run.vehicles.map((v) => ({
      record_id: v.record_id, title: v.title, status: v.status, current_stage: v.pipeline.current_stage,
      activity: v.pipeline.activity, last_event_at: v.pipeline.last_event_at, resolved_fields: 3, applicable_fields: 11,
      stages: v.pipeline.stages, counters: v.pipeline.counters,
    })),
    ...overrides,
  };
}

export const runList: RunList = {
  total: 4,
  runs: [summaryOf(activeRun()), summaryOf(multiRun), summaryOf(completedRun), summaryOf(interruptedRun)],
};

export function event(line: number, kind: string, extra: Record<string, unknown> = {}): RunEvent {
  return { line, seq: line, ts: "2026-10-05T10:00:00+00:00", kind, ...extra };
}

export function eventsPage(after: number, events: RunEvent[], more = false, recordId = "101122"): EventsPage {
  return { run_id: ACTIVE_ID, record_id: recordId, after, next_cursor: events.length ? events[events.length - 1].line : after,
           more, returned: events.length, events };
}

export function seriesList(overrides: Partial<SeriesList> = {}): SeriesList {
  return { ...clone(seriesListJson as unknown as SeriesList), ...overrides };
}

export const completedSeries = (seriesListJson as unknown as SeriesList).series[0];

export function runningSeries(): SeriesState {
  const s = clone(completedSeries);
  s.series_id = "series-running-1";
  s.status = "RUNNING";
  s.executing = true;
  s.finished_at = null;
  s.benchmark = null;
  s.current_index = 0;
  s.planned = s.planned.map((item, i) => ({ ...item, status: i === 0 ? "RUNNING" : "PLANNED", run_id: i === 0 ? item.run_id : null,
                                             finished_at: null }));
  s.run_ids = s.run_ids.slice(0, 1);
  s.series_progress = { total: { planned: 2, done: 0, running: 1, not_run: 0 },
                        arms: Object.fromEntries(s.arms.map((a, i) => [a, { planned: 1, done: 0, running: i === 0 ? 1 : 0, not_run: 0 }])) };
  return s;
}

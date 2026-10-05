import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { DocumentRow } from "../api/types";
import { CandidatesView, EvidenceView, ResultsView } from "../components/run/DataViews";
import { EventTimeline } from "../components/run/EventTimeline";
import {
  AllVehiclesResults, BenchmarkView, CacheDocumentsView, LiveFieldsView, NoticeList, RunReportView, StatusPanel,
  VehicleTechnicalView,
} from "../components/run/ParityViews";
import { ExportButtons, FailureCard, PipelineView, RunHeader, VehicleList } from "../components/run/RunOverview";
import {
  BindingReplayView, DocumentDrawer, DocumentsView, RunDiagnosticsView, TabSkeleton,
} from "../components/run/TechnicalViews";
import {
  Button, Disclosure, EmptyState, ErrorState, JsonBlock, Notice, Panel, Segmented, Skeleton, SkeletonRows, Tabs,
} from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useEventStream } from "../hooks/useEventStream";
import { useNow } from "../hooks/useNow";
import { useResource } from "../hooks/useResource";
import { useRunMonitor } from "../hooks/useRunMonitor";

const TABS = ["pipeline", "timeline", "results", "candidates", "evidence", "documents", "replay", "diagnostics",
  "benchmark", "technical"] as const;
type RunTab = (typeof TABS)[number];
const TAB_LABELS: Record<RunTab, string> = {
  pipeline: "Pipeline", timeline: "Live timeline", results: "Results", candidates: "Candidates", evidence: "Evidence",
  documents: "Documents", replay: "Binding Replay", diagnostics: "Diagnostics", benchmark: "Benchmark",
  technical: "Technical",
};
const DATA_ACTIVE_MS = 10000;

export function RunPage() {
  const { runId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { config, runs } = useWorkspace();
  const { detail, progress, active } = useRunMonitor(runId);
  const run = detail.data;
  const now = useNow(active, 1000);
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [openDoc, setOpenDoc] = useState<{ id: string; title?: string | null } | null>(null);
  const [docScope, setDocScope] = useState<"vehicle" | "cache">("vehicle");

  const tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as RunTab) : "pipeline";
  const vehicles = run?.vehicles ?? [];
  const requested = params.get("vehicle");
  const selected = vehicles.find((v) => v.record_id === requested)?.record_id
    ?? vehicles.find((v) => v.pipeline.current_stage)?.record_id ?? vehicles[0]?.record_id ?? "";
  const vehicle = vehicles.find((v) => v.record_id === selected);
  const multi = vehicles.length > 1;
  const recordId = selected || undefined;

  const setParam = useCallback((key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  }, [params, setParams]);

  // pin the default vehicle once, so the selection never jumps while other vehicles start and finish
  useEffect(() => {
    if (multi && !requested && selected) setParam("vehicle", selected);
  }, [multi, requested, selected, setParam]);

  // per-vehicle data (re-keyed when the selected vehicle changes; the parent run context stays)
  const vehicleProgress = progress.data?.vehicles.find((v) => v.record_id === selected);
  const pipeline = vehicle ? {
    ...vehicle.pipeline,
    ...(active && vehicleProgress ? { stages: vehicleProgress.stages, current_stage: vehicleProgress.current_stage,
                                      activity: vehicleProgress.activity, counters: vehicleProgress.counters,
                                      last_event_at: vehicleProgress.last_event_at } : {}),
  } : null;
  const stream = useEventStream(run ? runId : null, recordId, active);
  const dataInterval = active ? DATA_ACTIVE_MS : null;
  const key = (name: string) => (run && recordId ? `${name}:${runId}:${recordId}` : null);
  const candidates = useResource(tab === "candidates" ? key("cand") : null,
    (signal) => api.candidates(runId, recordId, { signal }), { intervalMs: dataInterval });
  const evidence = useResource(tab === "evidence" ? key("evid") : null,
    (signal) => api.evidence(runId, recordId, { signal }), { intervalMs: dataInterval });
  const documents = useResource(tab === "documents" && docScope === "vehicle" ? key("docs") : null,
    (signal) => api.documents(runId, recordId, 0, 200, { signal }), { intervalMs: dataInterval });
  const replay = useResource(tab === "replay" ? key("replay") : null,
    (signal) => api.bindingReplay(runId, recordId, undefined, 0, 500, { signal }));
  const diagnostics = useResource(tab === "diagnostics" && run ? `diag:${runId}` : null,
    (signal) => api.diagnostics(runId, { signal }));
  const results = useResource(tab === "results" && run && !active ? `results:${runId}:${run.status}` : null,
    (signal) => api.results(runId, { signal }));
  const benchmark = useResource(tab === "benchmark" && run ? `bench:${runId}:${run.status}` : null,
    (signal) => api.benchmark(runId, { signal }));
  const vehicleResult = results.data?.vehicles.find((v) => v.record_id === selected);

  const cancel = async () => {
    setCancelling(true);
    setActionError(null);
    try {
      await api.cancelRun(runId);
      await detail.refresh();
      void runs.refresh();
    } catch (e) {
      setActionError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Cancel failed."));
    } finally {
      setCancelling(false);
    }
  };

  const openDocument = useCallback((id: string, title?: string | null) => setOpenDoc({ id, title }), []);
  const tabs = useMemo(() => TABS.map((id) => ({ id, label: TAB_LABELS[id] })), []);

  if (detail.loading) {
    return (
      <div className="space-y-6" role="status" aria-label="Loading run">
        <Skeleton className="h-48 rounded-panel" />
        <div className="grid gap-3 sm:grid-cols-5">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-28 rounded-card" />)}</div>
        <SkeletonRows rows={5} />
      </div>
    );
  }
  if (!run) {
    return (
      <Panel>
        {detail.error?.status === 404
          ? <EmptyState title="This run does not exist" action={<Link to="/runs"><Button>Back to runs</Button></Link>}>
              <span className="mono">{runId}</span> is not in the run history of this server.
            </EmptyState>
          : <ErrorState error={detail.error} onRetry={detail.refresh} />}
      </Panel>
    );
  }

  return (
    <div className="space-y-6">
      <RunHeader run={run} active={active} now={now} profiles={config.data?.profiles} onCancel={cancel} cancelling={cancelling} />
      {actionError && <ErrorState error={actionError} title="The action was not accepted" />}
      {detail.error && <ErrorState error={detail.error} title="Live updates are retrying" onRetry={detail.refresh} />}

      <div className={multi ? "grid gap-6 xl:grid-cols-[18rem_minmax(0,1fr)]" : ""}>
        {multi && (
          <aside className="max-h-[22rem] overflow-y-auto xl:sticky xl:top-20 xl:max-h-[calc(100vh-6rem)]">
            <p className="kicker mb-2">Vehicles · {run.progress.vehicles_finished}/{run.progress.vehicles_total} finished</p>
            <VehicleList vehicles={vehicles} progress={progress.data?.vehicles} selected={selected}
                         onSelect={(id) => setParam("vehicle", id)} />
          </aside>
        )}

        <div className="min-w-0 space-y-6">
          {vehicle?.failure && (
            <FailureCard key={`${runId}:${selected}`} runId={runId} recordId={selected} failure={vehicle.failure}
                         onChanged={(newRun) => { void detail.refresh(); void runs.refresh(); if (newRun) navigate(`/runs/${encodeURIComponent(newRun)}`); }} />
          )}
          <Panel padded={false} className="overflow-hidden">
            <div className="border-b border-line px-4 pt-4 sm:px-6">
              {multi && vehicle && <p className="mb-2 truncate text-xs text-ink-muted" dir="auto">{vehicle.title}</p>}
              <Tabs label="Run views" tabs={tabs} value={tab} onChange={(id) => setParam("tab", id)} />
            </div>
            <div className="p-4 sm:p-6">
              {tab === "pipeline" && (pipeline ? (
                <div className="space-y-5">
                  <NoticeList notices={vehicle?.notices} />
                  <PipelineView pipeline={pipeline} />
                  <Disclosure title="Live field progress and activity log" defaultOpen={active}>
                    <LiveFieldsView key={`${runId}:${selected}`} runId={runId} recordId={recordId} active={active} />
                  </Disclosure>
                </div>
              ) : <EmptyState title="No vehicle in this run" />)}
              {tab === "timeline" && <EventTimeline key={`${runId}:${selected}`} stream={stream} active={active} />}
              {tab === "results" && (
                active ? <Notice tone="accent" title="Results appear when the run finishes">The run is still active; follow the live timeline meanwhile.</Notice>
                  : results.loading ? <TabSkeleton />
                  : results.error ? <ErrorState error={results.error} onRetry={results.refresh} />
                  : (
                    <div className="space-y-5">
                      <NoticeList notices={vehicle?.notices} />
                      {vehicleResult?.no_output_message && <Notice tone="warn" title="Final structured result">{vehicleResult.no_output_message} The Technical tab shows the partial research.</Notice>}
                      {vehicleResult ? <ResultsView result={vehicleResult} />
                        : <EmptyState title="No result for this vehicle">{results.data?.reason ?? "The vehicle did not write a result."}</EmptyState>}
                      {results.data && (multi ? (
                        <Disclosure title="All vehicles of this run"><AllVehiclesResults results={results.data} /></Disclosure>
                      ) : results.data.output_source_caption && <p className="text-xs text-ink-muted">{results.data.output_source_caption}</p>)}
                    </div>
                  )
              )}
              {tab === "candidates" && (candidates.loading ? <TabSkeleton />
                : candidates.error ? <ErrorState error={candidates.error} onRetry={candidates.refresh} />
                : candidates.data && <CandidatesView data={candidates.data} />)}
              {tab === "evidence" && (evidence.loading ? <TabSkeleton />
                : evidence.error ? <ErrorState error={evidence.error} onRetry={evidence.refresh} />
                : evidence.data && <EvidenceView data={evidence.data} onOpenDocument={(id) => openDocument(id)} />)}
              {tab === "documents" && (
                <div className="space-y-4">
                  <Segmented label="Documents" value={docScope} onChange={setDocScope}
                             options={[{ id: "vehicle", label: multi ? "This vehicle" : "This run" }, { id: "cache", label: "Entire cache" }]} />
                  {docScope === "cache" ? <CacheDocumentsView onOpen={openDocument} />
                    : documents.loading ? <TabSkeleton />
                    : documents.error ? <ErrorState error={documents.error} onRetry={documents.refresh} />
                    : documents.data && <DocumentsView data={documents.data} onOpen={(d: DocumentRow) => openDocument(d.doc_id, d.title)} />}
                </div>
              )}
              {tab === "replay" && (replay.loading ? <TabSkeleton />
                : replay.error ? <ErrorState error={replay.error} title="Binding Replay is not available" onRetry={replay.refresh} />
                : replay.data && <BindingReplayView data={replay.data} onOpenDocument={(id) => openDocument(id)} />)}
              {tab === "diagnostics" && (diagnostics.loading ? <TabSkeleton />
                : diagnostics.error ? <ErrorState error={diagnostics.error} onRetry={diagnostics.refresh} />
                : diagnostics.data && <RunDiagnosticsView data={diagnostics.data} />)}
              {tab === "benchmark" && (benchmark.loading ? <TabSkeleton />
                : benchmark.error ? <ErrorState error={benchmark.error} onRetry={benchmark.refresh} />
                : benchmark.data && <BenchmarkView data={benchmark.data} />)}
              {tab === "technical" && (
                <div className="space-y-3">
                  <StatusPanel rows={run.status_panel} />
                  {!active && <Disclosure title="Run report"><RunReportView vehicles={vehicles} /></Disclosure>}
                  {recordId && <VehicleTechnicalView key={`${runId}:${recordId}`} runId={runId} recordId={recordId} active={active} />}
                  <Disclosure title="Run request" defaultOpen><JsonBlock value={run.request} /></Disclosure>
                  {vehicle?.report && <Disclosure title="Vehicle report"><JsonBlock value={vehicle.report} /></Disclosure>}
                  {run.error && <Disclosure title="Run error"><JsonBlock value={run.error} /></Disclosure>}
                  <Disclosure title="Jobs"><JsonBlock value={run.jobs} /></Disclosure>
                  <Disclosure title="Raw run detail"><JsonBlock value={run} /></Disclosure>
                </div>
              )}
            </div>
          </Panel>
          <Panel kicker="Exports" title="Canonical backend exports">
            <p className="mb-3 text-sm text-ink-muted">Downloaded through the authenticated API, byte-identical to the dashboard&apos;s files.
              {multi && " candidates.csv is for the selected vehicle."}</p>
            <ExportButtons runId={runId} recordId={recordId} />
          </Panel>
        </div>
      </div>
      <DocumentDrawer docId={openDoc?.id ?? null} title={openDoc?.title} runId={runId} recordId={recordId}
                      onClose={() => setOpenDoc(null)} />
    </div>
  );
}

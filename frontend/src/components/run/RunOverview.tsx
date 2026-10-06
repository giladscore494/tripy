import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../../api/client";
import { api, exportsApi, type RunExport } from "../../api/endpoints";
import type { Failure, Pipeline, RunDetail, Stage, VehicleProgress, VehicleState } from "../../api/types";
import { formatBytes, formatDateTime, formatDuration, humanize, num } from "../../lib/format";
import { newKey } from "../../lib/idempotency";
import { stageTone, statusTone } from "../../lib/status";
import { IconAlert, IconCheck, IconDownload, IconRefresh, IconStop } from "../ui/icons";
import { Badge, Button, Card, cx, Disclosure, Dir, KeyValue, Mono, Notice, ProgressBar } from "../ui/primitives";
import { profileLabel, StatusBadge } from "./RunBits";

// --- header -----------------------------------------------------------------------------------------------------------

/** Retention in the run view: the run's size, the keep toggle and "Delete run" (two-step inline confirm; refused by the
 * server while the run is executing). Only <data>/runs/<run_id>/ is deleted. */
export function RunStorageActions({ run, onChanged, onDeleted }: {
  run: RunDetail; onChanged?: () => void; onDeleted?: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<"delete" | "keep" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const executing = run.executing;

  const remove = async () => {
    setBusy("delete");
    setError(null);
    try {
      await api.deleteRun(run.run_id);
      setConfirming(false);
      onDeleted?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The run was not deleted.");
    } finally {
      setBusy(null);
    }
  };
  const toggleKeep = async () => {
    setBusy("keep");
    setError(null);
    try {
      await api.keepRun(run.run_id, !run.pinned);
      onChanged?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The keep mark was not changed.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col items-start gap-2 lg:items-end">
      <div className="flex flex-wrap items-center gap-2">
        {run.size_bytes != null && <span className="text-xs text-ink-muted">{formatBytes(run.size_bytes)} on disk</span>}
        {run.compacted_at && <Badge title={`Compacted ${formatDateTime(run.compacted_at)}`}>Compacted</Badge>}
        <Button size="sm" variant="ghost" onClick={toggleKeep} busy={busy === "keep"} aria-pressed={!!run.pinned}
                title="A kept run is never compacted and never deleted by the age rule">
          {run.pinned ? "Kept ✓" : "Keep"}
        </Button>
        {!confirming ? (
          <Button size="sm" variant="ghost" onClick={() => setConfirming(true)} disabled={executing}
                  title={executing ? "The run is executing" : "Delete this run's folder"}>Delete run</Button>
        ) : (
          <>
            <span className="text-xs text-ink">Delete <Mono>{run.run_id}</Mono>{run.size_bytes != null ? ` (${formatBytes(run.size_bytes)})` : ""}?</span>
            <Button size="sm" variant="danger" onClick={remove} busy={busy === "delete"}>Confirm delete</Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
          </>
        )}
      </div>
      {error && <span className="text-xs text-danger">{error}</span>}
    </div>
  );
}

export function RunHeader({ run, active, now, profiles, onCancel, cancelling, onChanged, onDeleted }: {
  run: RunDetail; active: boolean; now: number; profiles?: { id: string; label: string }[];
  onCancel: () => void; cancelling: boolean; onChanged?: () => void; onDeleted?: () => void;
}) {
  const started = run.started_at ?? run.created_at;
  const elapsed = active && started ? (now - new Date(started).getTime()) / 1000 : run.elapsed_s;
  return (
    <div className="glass rounded-panel p-5 shadow-panel sm:p-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="kicker mb-1.5">{run.scope ?? "Run"}</p>
          <h1 className="text-xl font-semibold tracking-tight text-ink sm:text-2xl">
            <Dir>{run.label}</Dir>
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={run.status} label={run.status_label} active={active} />
            {run.cancel_requested && active && <Badge tone="warn">Stopping at the next safe point</Badge>}
            <Badge tone="violet">{profileLabel(run.profile, profiles)}</Badge>
            {run.legacy && <Badge>Legacy run folder</Badge>}
            {active && !run.executing && <Badge tone="warn" title="The durable status is active but no worker of this server executes it">No live worker</Badge>}
          </div>
        </div>
        <div className="flex flex-col items-start gap-3 lg:items-end">
          {run.executing && active && (
            <Button variant="danger" onClick={onCancel} busy={cancelling} disabled={run.cancel_requested}>
              <IconStop size={15} />{run.cancel_requested ? "Stopping…" : "Cancel run"}
            </Button>
          )}
          {!run.legacy && <RunStorageActions run={run} onChanged={onChanged} onDeleted={onDeleted} />}
        </div>
      </div>
      <div className="mt-5 border-t border-line pt-4">
        <KeyValue columns={4} items={[
          { label: "Run ID", value: <Mono className="text-ink">{run.run_id}</Mono> },
          { label: "Elapsed", value: <span className="tabular-nums">{formatDuration(elapsed)}</span> },
          { label: "Started", value: formatDateTime(started) },
          { label: "Updated", value: formatDateTime(run.updated_at) },
          { label: "Vehicles", value: `${run.progress.vehicles_finished} / ${run.progress.vehicles_total} finished` },
          { label: "Research model", value: <Mono className="text-ink">{String(run.request.research_model ?? "—")}</Mono> },
          { label: "Finalizer model", value: <Mono className="text-ink">{String(run.request.finalizer_model ?? "—")}</Mono> },
          { label: "Finished", value: formatDateTime(run.finished_at) },
        ]} />
      </div>
      {run.notes.length > 0 && (
        <ul className="mt-4 space-y-1 text-xs text-warn">{run.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      )}
    </div>
  );
}

// --- exports ----------------------------------------------------------------------------------------------------------

export function ExportButtons({ runId, recordId, disabled }: { runId: string; recordId?: string; disabled?: boolean }) {
  const [busy, setBusy] = useState<RunExport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async (name: RunExport) => {
    setBusy(name);
    setError(null);
    try {
      await exportsApi.run(runId, name, recordId);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The export failed.");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {(["candidates.csv", "per_vehicle.csv", "benchmark.json"] as RunExport[]).map((name) => (
          <Button key={name} size="sm" onClick={() => run(name)} busy={busy === name} disabled={disabled || busy !== null}>
            <IconDownload size={14} />{name}
          </Button>
        ))}
      </div>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  );
}

// --- vehicles ---------------------------------------------------------------------------------------------------------

export function VehicleList({ vehicles, progress, selected, onSelect }: {
  vehicles: VehicleState[]; progress?: VehicleProgress[]; selected: string; onSelect: (id: string) => void;
}) {
  const live = new Map((progress ?? []).map((v) => [v.record_id, v]));
  return (
    <ul aria-label="Vehicles of this run" className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
      {vehicles.map((vehicle) => {
        const p = live.get(vehicle.record_id);
        const status = p?.status ?? vehicle.status;
        const resolved = p?.resolved_fields ?? num(vehicle.pipeline.counters.resolved_fields);
        const applicable = p?.applicable_fields ?? num(vehicle.pipeline.counters.applicable_fields);
        const stage = p?.current_stage ?? vehicle.pipeline.current_stage;
        const stageLabel = (p?.stages ?? vehicle.pipeline.stages).find((s) => s.key === stage)?.label;
        // the durable per-vehicle status is written when a vehicle finishes; a running pipeline stage is fresher
        const shown = (status === "QUEUED" || status === "STARTING") && stage ? "running" : status.toLowerCase();
        return (
          <li key={vehicle.record_id}>
            <Card active={vehicle.record_id === selected} onClick={() => onSelect(vehicle.record_id)} className="!p-3">
              <div className="flex items-start justify-between gap-2">
                <Dir className="min-w-0 break-words text-sm font-medium text-ink">{vehicle.title}</Dir>
                <Badge tone={statusTone(shown)} pulse={statusTone(shown) === "accent"} className="shrink-0">
                  {humanize(shown)}
                </Badge>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-ink-muted">
                <Mono className="text-[11px]">{vehicle.record_id}</Mono>
                {stageLabel && <span>{stageLabel}</span>}
                {resolved !== null && applicable !== null && <span>{resolved}/{applicable} fields</span>}
                {vehicle.failure && <span className="text-danger">{vehicle.failure.title}</span>}
                {vehicle.result_available && <span className="text-ok">result ready</span>}
              </div>
              {resolved !== null && applicable ? <div className="mt-2"><ProgressBar value={resolved} max={applicable} label="Resolved fields" /></div> : null}
            </Card>
          </li>
        );
      })}
    </ul>
  );
}

// --- pipeline ---------------------------------------------------------------------------------------------------------

function StageCard({ stage, index }: { stage: Stage; index: number }) {
  const tone = stageTone(stage.state);
  const running = stage.state === "running";
  return (
    <div className={cx("relative overflow-hidden rounded-card border p-4 transition-all duration-300",
                       running ? "border-accent/50 bg-accent/[0.08] shadow-glow"
                         : tone === "ok" ? "border-ok/25 bg-ok/[0.04]"
                         : tone === "danger" ? "border-danger/35 bg-danger/[0.06]"
                         : tone === "warn" ? "border-warn/30 bg-warn/[0.05]" : "border-line bg-white/[0.025]")}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold text-ink-faint">0{index + 1}</span>
        <Badge tone={tone} pulse={running}>{stage.state_label}</Badge>
      </div>
      <p className="mt-3 text-sm font-semibold text-ink">{stage.label}</p>
      <p className="mt-1 text-xs tabular-nums text-ink-muted">{stage.duration_s !== null ? formatDuration(stage.duration_s) : "—"}</p>
      {stage.note && <Dir as="p" className="mt-2 line-clamp-3 text-[11.5px] leading-snug text-ink-muted">{stage.note}</Dir>}
      {running && <div className="absolute inset-x-0 bottom-0 h-0.5 animate-pulse-soft bg-gradient-to-r from-transparent via-accent to-transparent" />}
    </div>
  );
}

const COUNTER_LABELS: Record<string, string> = {
  resolved_fields: "Resolved fields", applicable_fields: "Applicable fields", documents: "Documents",
  searches: "Searches", candidates: "Candidates", evidence: "Evidence", cost_usd: "Cost (USD)",
};

export function PipelineView({ pipeline }: { pipeline: Pipeline }) {
  const counters = Object.entries(pipeline.counters).filter(([, v]) => typeof v === "number" || typeof v === "string");
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {pipeline.stages.map((stage, i) => <StageCard key={stage.key} stage={stage} index={i} />)}
      </div>
      {pipeline.activity && (
        <div className="flex items-center gap-3 rounded-card border border-accent/25 bg-accent/[0.05] px-4 py-3 text-sm">
          <span className="h-2 w-2 shrink-0 animate-pulse-soft rounded-full bg-accent" />
          <Dir className="text-ink">{pipeline.activity}</Dir>
        </div>
      )}
      <KeyValue columns={4} items={[
        { label: "Engine status", value: pipeline.engine_status ?? "—" },
        { label: "Stop reason", value: pipeline.stop_reason ?? "—" },
        { label: "Events seen", value: pipeline.events_seen.toLocaleString() },
        { label: "Last event", value: formatDateTime(pipeline.last_event_at) },
      ]} />
      {counters.length > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          {counters.map(([key, value]) => (
            <div key={key} className="rounded-xl border border-line bg-white/[0.025] px-3 py-2">
              <p className="truncate text-[10.5px] uppercase tracking-wider text-ink-faint" title={key}>{COUNTER_LABELS[key] ?? humanize(key)}</p>
              <p className="mt-0.5 text-sm font-semibold tabular-nums text-ink">
                {typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(value)}
              </p>
            </div>
          ))}
        </div>
      )}
      {pipeline.errors.length > 0 && (
        <Disclosure title={`${pipeline.errors.length} error event${pipeline.errors.length === 1 ? "" : "s"}`}>
          <ul className="space-y-2 text-xs">
            {pipeline.errors.map((e, i) => (
              <li key={i} className="rounded-lg bg-danger/[0.06] p-2 text-ink-muted">
                <Dir>{String(e.message ?? e.error ?? e.kind ?? JSON.stringify(e))}</Dir>
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </div>
  );
}

// --- failure ----------------------------------------------------------------------------------------------------------

export function FailureCard({ runId, recordId, failure, onChanged }: {
  runId: string; recordId: string; failure: Failure; onChanged: (newRunId?: string) => void;
}) {
  const [busy, setBusy] = useState<"finalize" | "restart" | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string; runId?: string } | null>(null);
  const [restartKey] = useState(() => newKey("restart"));
  const can = (action: string) => failure.actions.includes(action);

  const act = async (action: "finalize" | "restart") => {
    setBusy(action);
    setMessage(null);
    try {
      if (action === "finalize") {
        const accepted = await api.finalizeVehicle(runId, recordId);
        setMessage({ tone: "ok", text: accepted.message });
        onChanged();
      } else {
        const started = await api.restartVehicle(runId, recordId, restartKey);
        setMessage({ tone: "ok", text: "A new run of this vehicle started.", runId: started.run_id });
        onChanged(started.run_id);
      }
    } catch (e) {
      setMessage({ tone: "danger", text: e instanceof ApiError ? e.message : "The action failed.",
                   runId: e instanceof ApiError && typeof e.details.existing_run_id === "string"
                     ? e.details.existing_run_id : undefined });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div role="alert" className="rounded-panel border border-danger/35 bg-gradient-to-br from-danger/[0.09] to-transparent p-5">
      <div className="flex items-start gap-3">
        <span className="rounded-xl border border-danger/40 bg-danger/10 p-2 text-danger"><IconAlert size={18} /></span>
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <p className="text-base font-semibold text-ink"><Dir>{failure.title}</Dir></p>
            <p className="mt-0.5 text-xs text-ink-muted">
              Failed during <span className="text-ink">{failure.stage_label ?? failure.stage ?? "an unknown stage"}</span>
              {" · "}<Mono className="text-[11px]">{failure.code}</Mono>
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-xl bg-white/[0.03] p-3">
              <p className="kicker mb-1">What failed</p>
              <Dir as="p" className="text-sm text-ink">{failure.reason}</Dir>
            </div>
            <div className="rounded-xl bg-white/[0.03] p-3">
              <p className="kicker mb-1">What survived</p>
              <Dir as="p" className="text-sm text-ink">{failure.preserved ?? "Nothing was preserved for this vehicle."}</Dir>
            </div>
          </div>
          {(can("finalize") || can("restart")) && (
            <div className="flex flex-wrap gap-2">
              {can("finalize") && (
                <Button variant="primary" busy={busy === "finalize"} disabled={busy !== null} onClick={() => act("finalize")}>
                  <IconCheck size={15} />Finalize preserved research
                </Button>
              )}
              {can("restart") && (
                <Button busy={busy === "restart"} disabled={busy !== null} onClick={() => act("restart")}>
                  <IconRefresh size={15} />Restart this vehicle
                </Button>
              )}
            </div>
          )}
          {message && (
            <Notice tone={message.tone} action={message.runId ? (
              <Link to={`/runs/${encodeURIComponent(message.runId)}`}><Button size="sm">Open run</Button></Link>) : undefined}>
              {message.text}
            </Notice>
          )}
          {failure.technical && (
            <Disclosure title="Technical detail"><pre dir="ltr" className="mono whitespace-pre-wrap break-words text-ink-muted">{failure.technical}</pre></Disclosure>
          )}
        </div>
      </div>
    </div>
  );
}

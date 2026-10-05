import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { api, exportsApi } from "../api/endpoints";
import type { SeriesList, SeriesState, Vehicle } from "../api/types";
import { StatusBadge } from "../components/run/RunBits";
import { defaultsOf, overridesOf, RunSettingsForm, settingsErrors, type SettingsValues } from "../components/run/RunSettingsForm";
import { IconDownload, IconPlay, IconSearch, IconStop } from "../components/ui/icons";
import {
  Badge, Button, cx, Dir, Disclosure, EmptyState, ErrorState, KeyValue, Mono, Notice, Panel, ProgressBar, Skeleton, SkeletonRows,
} from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";
import { formatDateTime, formatRelative } from "../lib/format";
import { useSubmissionKey } from "../lib/idempotency";

export const SERIES_ACTIVE_MS = 3000;
export const SERIES_IDLE_MS = 20000;

function SeriesProgressBar({ series }: { series: SeriesState }) {
  const total = series.series_progress.total;
  return (
    <div className="space-y-1.5">
      <ProgressBar value={total.done} max={total.planned} label="Series progress" />
      <p className="text-xs text-ink-muted">
        {total.done} / {total.planned} done{total.running ? ` · ${total.running} running` : ""}{total.not_run ? ` · ${total.not_run} not run` : ""}
      </p>
    </div>
  );
}

// --- create -----------------------------------------------------------------------------------------------------------

function VehicleMultiPicker({ vehicles, selected, onChange }: {
  vehicles: Vehicle[]; selected: string[]; onChange: (ids: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = vehicles.filter((v) => !q || [v.label, v.title, v.manufacturer, v.model, v.record_id]
    .some((p) => p.toLowerCase().includes(q)));
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <IconSearch size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
          <input aria-label="Search series vehicles" className="input pl-9" placeholder="Search vehicles"
                 value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <span className="text-xs text-ink-muted">{selected.length} selected</span>
        {selected.length > 0 && <Button size="sm" variant="ghost" onClick={() => onChange([])}>Clear</Button>}
      </div>
      <ul aria-label="Series vehicles" className="grid max-h-72 gap-1 overflow-y-auto rounded-card border border-line bg-white/[0.02] p-1.5 sm:grid-cols-2">
        {shown.map((v) => (
          <li key={v.record_id}>
            <label className={cx("flex cursor-pointer items-center gap-2.5 rounded-xl px-3 py-2 text-sm transition-colors",
                                 selected.includes(v.record_id) ? "bg-accent/15 text-ink" : "text-ink-muted hover:bg-white/[0.05]")}>
              <input type="checkbox" checked={selected.includes(v.record_id)} onChange={() => toggle(v.record_id)}
                     className="h-4 w-4 shrink-0 accent-[rgb(var(--c-accent))]" />
              <Dir className="min-w-0 break-words">{v.label}</Dir>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CreateSeries({ list, onStarted }: { list: SeriesList; onStarted: (id: string) => void }) {
  const { config } = useWorkspace();
  const vehicles = useResource("vehicles", (signal) => api.vehicles({ signal }));
  const contract = useResource("run-settings", (signal) => api.runSettings({ signal }));
  const [values, setValues] = useState<SettingsValues | null>(null);
  const [ids, setIds] = useState<string[]>([]);
  const [repeatsText, setRepeatsText] = useState("3");
  const repeats = Number(repeatsText);
  const repeatsValid = Number.isInteger(repeats) && repeats >= 1 && repeats <= list.max_repeats;
  const [arms, setArms] = useState<string[]>(list.default_arms);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const submission = useSubmissionKey("series");
  const ordered = list.arms.filter((a) => arms.includes(a.id));          // run order = ARMS order (the backend's)
  const running = list.executing;
  const blocked = config.data ? !config.data.configured : true;
  useEffect(() => {
    if (contract.data && !values) setValues(defaultsOf(contract.data));
  }, [contract.data, values]);
  const errors = contract.data && values ? settingsErrors(contract.data, values) : {};
  const overrides = contract.data && values ? overridesOf(contract.data, values) : {};
  const can = ids.length > 0 && ordered.length > 0 && repeatsValid && !running && !blocked && !busy
    && !Object.keys(errors).length;

  const submit = () => submission.guard(async () => {
    if (!can) return;
    setBusy(true);
    setError(null);
    let startedOk = false;
    try {
      const started = await api.startSeries({ record_ids: ids, repeats, arms: ordered.map((a) => a.id),
                                              idempotency_key: submission.current(),
                                              ...(Object.keys(overrides).length ? { settings: overrides } : {}) });
      submission.resolve();
      startedOk = true;          // navigating away: the button stays busy
      onStarted(started.series_id);
    } catch (e) {
      const apiError = e instanceof ApiError ? e : new ApiError(0, "client_error", "The series could not be started.");
      if (apiError.status !== 0) submission.resolve();
      setError(apiError);
    } finally {
      if (!startedOk) setBusy(false);
    }
  });

  return (
    <Panel kicker="Benchmark A/B" title="New A/B series">
      <div className="space-y-5">
        {vehicles.loading ? <Skeleton className="h-56 rounded-card" />
          : vehicles.error ? <ErrorState error={vehicles.error} onRetry={vehicles.refresh} />
          : <VehicleMultiPicker vehicles={vehicles.data?.vehicles ?? []} selected={ids} onChange={setIds} />}
        <div className="grid gap-5 md:grid-cols-[12rem_minmax(0,1fr)]">
          <div className="space-y-1.5">
            <label htmlFor="repeats" className="block text-xs font-medium text-ink-muted">Runs per arm</label>
            <input id="repeats" type="number" min={1} max={list.max_repeats} step={1} inputMode="numeric"
                   className={cx("input", !repeatsValid && "border-danger/60")} value={repeatsText}
                   onChange={(e) => setRepeatsText(e.target.value)} />
            <p className={cx("text-[11.5px]", repeatsValid ? "text-ink-faint" : "text-danger")}>1 to {list.max_repeats}</p>
          </div>
          <fieldset>
            <legend className="mb-1.5 text-xs font-medium text-ink-muted">Arms</legend>
            <div className="flex flex-wrap gap-2">
              {list.arms.map((arm) => (
                <label key={arm.id} className={cx("flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-sm transition-colors",
                                                  arms.includes(arm.id) ? "border-accent/50 bg-accent/10 text-ink" : "border-line text-ink-muted hover:bg-white/[0.05]")}>
                  <input type="checkbox" checked={arms.includes(arm.id)} className="h-4 w-4 accent-[rgb(var(--c-accent))]"
                         onChange={() => setArms(arms.includes(arm.id) ? arms.filter((a) => a !== arm.id) : [...arms, arm.id])} />
                  {arm.label}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
        <p className="text-sm text-ink-muted">
          <span className="text-ink">{repeatsValid ? repeats * ordered.length : "—"} run(s)</span> over {ids.length} vehicle(s), strictly one after another,
          arms interleaved ({ordered.map((a) => a.label).join(", ")}{ordered.length ? ", …" : ""}). Every run uses the per-run settings below
          (server defaults unless changed); each arm&apos;s profile pins its own experiment settings.
        </p>
        {contract.error ? <ErrorState error={contract.error} title="Per-run settings are unavailable" onRetry={contract.refresh} />
          : contract.data && values ? (
            <Disclosure title={<span className="flex items-center gap-2">Per-run settings for every run of the series
              {Object.keys(overrides).length > 0 && <Badge tone="accent">{Object.keys(overrides).length} changed</Badge>}</span>}>
              <RunSettingsForm contract={contract.data} values={values} onChange={setValues}
                               profile={ordered[0]?.id ?? list.default_arms[0] ?? ""}
                               appliesTo="the runs of this series only" />
            </Disclosure>
          ) : <Skeleton className="h-12 rounded-card" />}
        {Object.keys(errors).length > 0 && <p className="text-sm text-danger">Fix the highlighted settings first.</p>}
        {running && (
          <Notice tone="warn" title="A series is already running" action={<Link to={`/series/${encodeURIComponent(running)}`}><Button size="sm">Open it</Button></Link>}>
            Only one A/B series executes at a time on this server.
          </Notice>
        )}
        {blocked && config.data && <Notice tone="danger" title="The server is not configured to start research">{config.data.blocking.join(", ")}</Notice>}
        <div className="flex justify-end">
          <Button variant="primary" onClick={submit} busy={busy} disabled={!can}><IconPlay size={14} />Start A/B series</Button>
        </div>
        {error && (typeof error.details.existing_series_id === "string" ? (
          <Notice tone="warn" title="The series was not started" action={
            <Link to={`/series/${encodeURIComponent(error.details.existing_series_id)}`}><Button size="sm">Open running series</Button></Link>}>
            {error.message}
          </Notice>
        ) : <ErrorState error={error} title="The series was not started" onRetry={error.status === 0 ? submit : undefined} />)}
      </div>
    </Panel>
  );
}

// --- list -------------------------------------------------------------------------------------------------------------

export function SeriesListPage() {
  const navigate = useNavigate();
  const [fast, setFast] = useState(false);
  const series = useResource("series", (signal) => api.series(50, { signal }),
                             { intervalMs: fast ? SERIES_ACTIVE_MS : SERIES_IDLE_MS });
  useEffect(() => setFast(Boolean(series.data?.executing)), [series.data?.executing]);

  return (
    <div className="space-y-6">
      <PageHeader kicker="Experiments" title="A/B series">
        Benchmark arms run back to back over the same vehicles, interleaved per repeat, each with a private cold cache.
      </PageHeader>
      {series.loading ? <Skeleton className="h-80 rounded-panel" />
        : series.error && !series.data ? <ErrorState error={series.error} onRetry={series.refresh} />
        : series.data && <CreateSeries list={series.data} onStarted={(id) => navigate(`/series/${encodeURIComponent(id)}`)} />}
      <Panel kicker="History" title="Series">
        {series.loading ? <SkeletonRows rows={3} label="Loading series" />
          : series.data?.series.length ? (
            <ul className="space-y-2">
              {series.data.series.map((s) => (
                <li key={s.series_id}>
                  <Link to={`/series/${encodeURIComponent(s.series_id)}`}
                        className="grid gap-3 rounded-card border border-line bg-white/[0.025] px-4 py-3 transition-colors hover:bg-glass-hover sm:grid-cols-[minmax(0,1fr)_14rem] sm:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">{s.label}</span>
                        <StatusBadge status={s.status} active={s.executing} />
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-ink-muted">
                        <Mono className="text-[11px]">{s.series_id}</Mono>
                        <span>{s.arms.map((a) => s.arm_labels[a] ?? a).join(" · ")}</span>
                        <span>{s.repeats}× per arm</span>
                        <span>{formatRelative(s.created_at)}</span>
                      </div>
                    </div>
                    <SeriesProgressBar series={s} />
                  </Link>
                </li>
              ))}
            </ul>
          ) : <EmptyState title="No A/B series yet">Start one above to compare benchmark arms.</EmptyState>}
      </Panel>
    </div>
  );
}

// --- detail -----------------------------------------------------------------------------------------------------------

export function SeriesPage() {
  const { seriesId = "" } = useParams();
  const [fast, setFast] = useState(true);
  const series = useResource(`series:${seriesId}`, (signal) => api.seriesState(seriesId, { signal }),
                             { intervalMs: fast ? SERIES_ACTIVE_MS : null });
  useEffect(() => {
    if (series.data) setFast(series.data.executing || series.data.status === "RUNNING");
  }, [series.data]);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const s = series.data;
  const armRows = useMemo(() => (s ? Object.entries(s.series_progress.arms) : []), [s]);

  const cancel = async () => {
    setCancelling(true);
    setError(null);
    try {
      await api.cancelSeries(seriesId);
      await series.refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Cancel failed."));
    } finally {
      setCancelling(false);
    }
  };
  const download = async (name: string) => {
    setDownloading(name);
    try {
      await exportsApi.series(seriesId, name);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Download failed."));
    } finally {
      setDownloading(null);
    }
  };

  if (series.loading) return <div className="space-y-4" role="status" aria-label="Loading series"><Skeleton className="h-40 rounded-panel" /><Skeleton className="h-80 rounded-panel" /></div>;
  if (!s) {
    return <Panel>{series.error?.status === 404
      ? <EmptyState title="This series does not exist" action={<Link to="/series"><Button>Back to series</Button></Link>} />
      : <ErrorState error={series.error} onRetry={series.refresh} />}</Panel>;
  }

  return (
    <div className="space-y-6">
      <PageHeader kicker="A/B series" title={s.label}
                  actions={s.executing && (
                    <Button variant="danger" onClick={cancel} busy={cancelling}><IconStop size={15} />Cancel series</Button>
                  )}>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={s.status} active={s.executing} />
          <Mono>{s.series_id}</Mono>
        </div>
      </PageHeader>
      {error && <ErrorState error={error} title="The action was not accepted" />}
      {s.error && <Notice tone="warn" title="Series note">{s.error}</Notice>}
      <Panel kicker="Progress" title="Planned runs">
        <div className="space-y-5">
          <SeriesProgressBar series={s} />
          <div className="grid gap-3 sm:grid-cols-3">
            {armRows.map(([arm, c]) => (
              <div key={arm} className="rounded-card border border-line bg-white/[0.025] p-4">
                <p className="text-sm font-semibold text-ink">{s.arm_labels[arm] ?? arm}</p>
                <p className="mt-1 text-xs text-ink-muted">{c.done}/{c.planned} done · {c.running} running · {c.not_run} not run</p>
                <div className="mt-2"><ProgressBar value={c.done} max={c.planned} label={`${arm} progress`} /></div>
              </div>
            ))}
          </div>
          <ol className="space-y-1.5" aria-label="Planned runs">
            {s.planned.map((item) => (
              <li key={item.index} className={cx("grid items-center gap-2 rounded-xl px-3 py-2 text-sm sm:grid-cols-[3rem_6rem_minmax(0,1fr)_auto]",
                                                 item.index === s.current_index && s.executing ? "border border-accent/40 bg-accent/[0.07] shadow-glow" : "bg-white/[0.025]")}>
                <span className="text-ink-faint">#{item.index + 1}</span>
                <span className="text-ink-muted">repeat {item.repeat}</span>
                <span className="min-w-0 truncate text-ink">{item.arm_label}
                  {item.run_id && <Link className="ml-2 font-mono text-[11px] text-accent-soft hover:underline" to={`/runs/${encodeURIComponent(item.run_id)}`}>{item.run_id}</Link>}
                </span>
                <StatusBadge status={item.status} active={item.status === "RUNNING"} />
              </li>
            ))}
          </ol>
        </div>
      </Panel>
      <div className="grid gap-6 lg:grid-cols-2">
        <Panel kicker="Setup" title="Series">
          <KeyValue columns={2} items={[
            { label: "Runs per arm", value: s.repeats },
            { label: "Arms", value: s.arms.map((a) => s.arm_labels[a] ?? a).join(", ") },
            { label: "Created", value: formatDateTime(s.created_at) },
            { label: "Finished", value: formatDateTime(s.finished_at) },
          ]} />
          <p className="kicker mb-2 mt-5">Vehicles ({s.vehicles.length})</p>
          <ul className="flex flex-wrap gap-1.5">{s.vehicles.map((v) => <li key={v.record_id}><Badge><Dir>{v.label}</Dir></Badge></li>)}</ul>
        </Panel>
        <Panel kicker="Benchmark" title="Series benchmark">
          {s.benchmark?.files?.length ? (
            <div className="space-y-3">
              <p className="text-sm text-ink-muted">Written over exactly this series&apos; {s.benchmark.run_ids?.length ?? 0} run(s)
                {s.benchmark.complete ? "." : " (partial: the series did not complete)."}</p>
              <div className="flex flex-wrap gap-2">
                {s.benchmark.files.map((name) => (
                  <Button key={name} size="sm" onClick={() => download(name)} busy={downloading === name} disabled={downloading !== null}>
                    <IconDownload size={14} />{name}
                  </Button>
                ))}
              </div>
            </div>
          ) : <EmptyState title={s.executing ? "Written when the series finishes" : "No benchmark was written"} />}
        </Panel>
      </div>
    </div>
  );
}

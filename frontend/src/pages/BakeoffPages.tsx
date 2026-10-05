import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { api, exportsApi } from "../api/endpoints";
import type { BakeoffDetail, BakeoffMetrics, BakeoffOptions } from "../api/types";
import { StatusBadge } from "../components/run/RunBits";
import { IconDownload, IconPlay, IconStop } from "../components/ui/icons";
import {
  Badge, Button, cx, DataTable, Dir, EmptyState, ErrorState, KeyValue, Mono, Notice, Panel, ProgressBar, Skeleton,
  SkeletonRows,
} from "../components/ui/primitives";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";
import { formatDateTime, formatRelative } from "../lib/format";

// PR #46 (S4): the search bake-off runs on the server as a background job (src/jobs/bakeoffs.py). It measures which
// search backend finds the Israeli version pages; its results never enter a run's evidence.

export const BAKEOFF_ACTIVE_MS = 3000;
export const BAKEOFF_IDLE_MS = 20000;

const METRIC_COLUMNS: { key: keyof BakeoffMetrics; label: string; kind?: "ratio" | "usd" | "ms" }[] = [
  { key: "backend", label: "Backend" },
  { key: "queries", label: "Queries" },
  { key: "full_path_ratio", label: "Full-path URLs", kind: "ratio" },
  { key: "on_site_ratio", label: "On-site (site:)", kind: "ratio" },
  { key: "version_url_hit", label: "Version URL hit (records)" },
  { key: "first_version_rank", label: "First version rank" },
  { key: "israeli_domain_share", label: "Israeli domains", kind: "ratio" },
  { key: "unique_urls", label: "Unique URLs" },
  { key: "candidates", label: "Ranked candidates" },
  { key: "accepted", label: "Accepted pages" },
  { key: "latency_p50_ms", label: "Latency p50", kind: "ms" },
  { key: "latency_p95_ms", label: "Latency p95", kind: "ms" },
  { key: "usd_per_1000_queries", label: "USD / 1,000 queries", kind: "usd" },
  { key: "usd_per_record", label: "USD / record", kind: "usd" },
  { key: "accepted_per_usd", label: "Accepted / USD" },
];

function show(value: unknown, kind?: "ratio" | "usd" | "ms"): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  if (kind === "ratio") return `${(value * 100).toFixed(1)}%`;
  if (kind === "usd") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  if (kind === "ms") return `${Math.round(value)} ms`;
  return String(value);
}

export function MetricsTable({ metrics }: { metrics: BakeoffMetrics[] }) {
  return (
    <div className="table-wrap">
      <table className="data-table" aria-label="Bake-off metrics">
        <thead><tr>{METRIC_COLUMNS.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m.backend}>
              {METRIC_COLUMNS.map((c) => (
                <td key={c.key} className={cx("text-xs", c.key === "backend" ? "font-semibold text-ink" : "text-ink-muted")}>
                  {show(m[c.key], c.kind)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- create -----------------------------------------------------------------------------------------------------------

function CreateBakeoff({ options, running, onStarted }: {
  options: BakeoffOptions; running: boolean; onStarted: (id: string) => void;
}) {
  const [backends, setBackends] = useState<string[]>(options.defaults.backends);
  const [allRecords, setAllRecords] = useState(true);
  const [records, setRecords] = useState<string[]>([]);
  const [fetchTop, setFetchTop] = useState(options.defaults.fetch_top);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const can = backends.length > 0 && (allRecords || records.length > 0) && !running && !busy;
  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

  const submit = async () => {
    if (!can) return;
    setBusy(true);
    setError(null);
    try {
      const started = await api.startBakeoff({ backends, fetch_top: fetchTop,
                                               ...(allRecords ? {} : { record_ids: records }) });
      onStarted(started.bakeoff_id);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "The bake-off could not be started."));
      setBusy(false);
    }
  };

  return (
    <Panel kicker="Search backends" title="New search bake-off">
      <div className="space-y-5">
        <fieldset>
          <legend className="kicker mb-2">Backends</legend>
          <div className="flex flex-wrap gap-2">
            {options.backends.map((b) => (
              <label key={b.name} title={b.available ? b.label : `Unavailable: ${b.reason}`}
                     className={cx("flex items-center gap-2 rounded-xl border border-line px-3 py-2 text-sm",
                                   b.available ? "cursor-pointer text-ink" : "cursor-not-allowed text-ink-faint opacity-60")}>
                <input type="checkbox" aria-label={`Backend ${b.name}`} disabled={!b.available}
                       checked={backends.includes(b.name)} onChange={() => setBackends(toggle(backends, b.name))}
                       className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
                <span>{b.name}</span>
                {!b.available && <Badge tone="warn">unavailable: {b.reason}</Badge>}
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="space-y-2">
          <legend className="kicker mb-2">Records</legend>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input type="checkbox" aria-label="All benchmark records" checked={allRecords}
                   onChange={() => setAllRecords(!allRecords)} className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
            All {options.records.length} benchmark records
          </label>
          {!allRecords && (
            <ul aria-label="Bake-off records" className="grid max-h-60 gap-1 overflow-y-auto rounded-card border border-line p-1.5 sm:grid-cols-2">
              {options.records.map((r) => (
                <li key={r.record_id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-xl px-3 py-1.5 text-sm text-ink-muted hover:bg-white/[0.05]">
                    <input type="checkbox" checked={records.includes(r.record_id)}
                           onChange={() => setRecords(toggle(records, r.record_id))}
                           className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
                    <Dir className="min-w-0 break-words">{r.label}</Dir>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" aria-label="Fetch top candidate" checked={fetchTop} onChange={() => setFetchTop(!fetchTop)}
                 className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
          Fetch the top candidate (robots.txt, per-domain pace, shared cache) and report its version-page verdict
        </label>
        {running && <Notice tone="info" title="A bake-off is running">One bake-off runs at a time.</Notice>}
        {error && <ErrorState error={error} title="The bake-off was not started" />}
        <p className="text-xs text-ink-muted">{options.note}</p>
        <Button variant="primary" onClick={submit} disabled={!can} busy={busy}><IconPlay size={14} />Start bake-off</Button>
      </div>
    </Panel>
  );
}

export function BakeoffListPage() {
  const navigate = useNavigate();
  const [fast, setFast] = useState(false);
  const options = useResource("bakeoff-options", (signal) => api.bakeoffOptions({ signal }));
  const list = useResource("bakeoffs", (signal) => api.bakeoffs({ signal }),
                           { intervalMs: fast ? BAKEOFF_ACTIVE_MS : BAKEOFF_IDLE_MS });
  const running = Boolean(list.data?.bakeoffs.some((b) => b.executing));
  useEffect(() => setFast(running), [running]);

  return (
    <div className="space-y-6">
      <PageHeader kicker="Experiments" title="Search bake-off">
        Which search backend finds the Israeli version pages: the resolver&apos;s queries and two generic research queries
        per benchmark record, measured per backend.
      </PageHeader>
      {options.loading ? <Skeleton className="h-64 rounded-panel" />
        : options.error ? <ErrorState error={options.error} onRetry={options.refresh} />
        : options.data && <CreateBakeoff options={options.data} running={running}
                                         onStarted={(id) => navigate(`/bakeoffs/${encodeURIComponent(id)}`)} />}
      <Panel kicker="History" title="Bake-offs">
        {list.loading ? <SkeletonRows rows={3} label="Loading bake-offs" />
          : list.data?.bakeoffs.length ? (
            <ul className="space-y-2">
              {list.data.bakeoffs.map((b) => (
                <li key={b.bakeoff_id}>
                  <Link to={`/bakeoffs/${encodeURIComponent(b.bakeoff_id)}`}
                        className="grid gap-3 rounded-card border border-line bg-white/[0.025] px-4 py-3 transition-colors hover:bg-glass-hover sm:grid-cols-[minmax(0,1fr)_14rem] sm:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">{b.label}</span>
                        <StatusBadge status={b.status} active={b.executing} />
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-ink-muted">
                        <Mono className="text-[11px]">{b.bakeoff_id}</Mono>
                        <span>{b.config.backends.join(" · ")}</span>
                        <span>{formatRelative(b.created_at)}</span>
                      </div>
                    </div>
                    <ProgressBar value={b.progress.done} max={b.progress.total ?? Math.max(1, b.progress.done)}
                                 label={`${b.bakeoff_id} progress`} />
                  </Link>
                </li>
              ))}
            </ul>
          ) : <EmptyState title="No bake-off yet">Start one above to compare the search backends.</EmptyState>}
      </Panel>
    </div>
  );
}

// --- detail -----------------------------------------------------------------------------------------------------------

export function BakeoffPage() {
  const { bakeoffId = "" } = useParams();
  const [fast, setFast] = useState(true);
  const detail = useResource<BakeoffDetail>(`bakeoff:${bakeoffId}`, (signal) => api.bakeoff(bakeoffId, { signal }),
                                            { intervalMs: fast ? BAKEOFF_ACTIVE_MS : null });
  useEffect(() => {
    if (detail.data) setFast(detail.data.bakeoff.executing || detail.data.bakeoff.status === "RUNNING");
  }, [detail.data]);
  const [cancelling, setCancelling] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const d = detail.data;
  const rows = useMemo(() => (d ? d.records.map((r) => ({ ...r })) : []), [d]);

  const cancel = async () => {
    setCancelling(true);
    setError(null);
    try {
      await api.cancelBakeoff(bakeoffId);
      await detail.refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Cancel failed."));
    } finally {
      setCancelling(false);
    }
  };
  const download = async (name: "metrics.csv" | "records.csv" | "summary.json") => {
    setDownloading(name);
    try {
      await exportsApi.bakeoff(bakeoffId, name);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Download failed."));
    } finally {
      setDownloading(null);
    }
  };

  if (detail.loading) return <div role="status" aria-label="Loading bake-off"><Skeleton className="h-80 rounded-panel" /></div>;
  if (!d) {
    return <Panel>{detail.error?.status === 404
      ? <EmptyState title="This bake-off does not exist" action={<Link to="/bakeoffs"><Button>Back to bake-offs</Button></Link>} />
      : <ErrorState error={detail.error} onRetry={detail.refresh} />}</Panel>;
  }
  const b = d.bakeoff;
  return (
    <div className="space-y-6">
      <PageHeader kicker="Search bake-off" title={b.label}
                  actions={b.executing && (
                    <Button variant="danger" onClick={cancel} busy={cancelling}><IconStop size={15} />Cancel bake-off</Button>
                  )}>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={b.status} active={b.executing} />
          <Mono>{b.bakeoff_id}</Mono>
        </div>
      </PageHeader>
      {error && <ErrorState error={error} title="The action was not accepted" />}
      {b.error && <Notice tone="warn" title="Bake-off note">{b.error}</Notice>}
      <Panel kicker="Progress" title="Records">
        <ProgressBar value={b.progress.done} max={b.progress.total ?? Math.max(1, b.progress.done)} label="Bake-off progress" />
        <p className="mt-1.5 text-xs text-ink-muted">{b.progress.done} / {b.progress.total ?? "…"} records</p>
        <div className="mt-4">
          <KeyValue columns={2} items={[
            { label: "Backends", value: b.config.backends.join(", ") },
            { label: "Fetch top candidate", value: b.config.fetch_top ? "on" : "off" },
            { label: "Records", value: b.config.records.length ? b.config.records.length : "all benchmark records" },
            { label: "Created", value: formatDateTime(b.created_at) },
            { label: "Finished", value: formatDateTime(b.finished_at) },
          ]} />
        </div>
      </Panel>
      <Panel kicker="Metrics" title="Per backend"
             actions={d.summary && (
               <div className="flex flex-wrap gap-2">
                 <Button size="sm" onClick={() => download("metrics.csv")} busy={downloading === "metrics.csv"}
                         disabled={downloading !== null}><IconDownload size={14} />metrics.csv</Button>
                 <Button size="sm" onClick={() => download("records.csv")} busy={downloading === "records.csv"}
                         disabled={downloading !== null}><IconDownload size={14} />records.csv</Button>
               </div>
             )}>
        {d.summary?.metrics.length ? <MetricsTable metrics={d.summary.metrics} />
          : <EmptyState title={b.executing ? "Written when the bake-off finishes" : "No metrics were written"} />}
      </Panel>
      <Panel kicker="Records" title="Per record and backend">
        {rows.length ? <DataTable rows={rows} label="Bake-off records" columns={[
          "record_id", "vehicle", "backend", "version_url_hit", "first_version_rank", "candidate_url", "candidate_score",
          "verdict", "verdict_reason", "usd"]} />
          : <EmptyState title="No record measured yet" />}
      </Panel>
    </div>
  );
}

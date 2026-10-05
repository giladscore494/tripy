import { useEffect, useState } from "react";

import { ApiError } from "../api/client";
import { api, exportsApi, type BenchmarkExport } from "../api/endpoints";
import { RunDiagnosticsView, TabSkeleton } from "../components/run/TechnicalViews";
import { IconDownload } from "../components/ui/icons";
import {
  Badge, Button, cx, DataTable, EmptyState, ErrorState, KeyValue, Mono, Panel, SkeletonRows,
} from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";
import { formatValue, humanize, str } from "../lib/format";

const BENCHMARK_FILES: BenchmarkExport[] = ["benchmark.json", "per_vehicle.csv", "parser_gaps.jsonl",
  "binding_replay_items.jsonl"];
const DEFAULT_RUNS = 15;

/** The dashboard's "Benchmark diagnostics": aggregate diagnostics over chosen runs, downloaded through the API. */
function BenchmarkExports() {
  const { runs } = useWorkspace();
  const list = runs.data?.runs ?? [];
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (chosen === null && runs.data) setChosen(runs.data.runs.slice(0, DEFAULT_RUNS).map((r) => r.run_id));
  }, [chosen, runs.data]);
  const ids = chosen ?? [];
  const summary = useResource(ids.length ? `bench-summary:${ids.join(",")}` : null,
                              (signal) => api.benchmarkSummary(ids, { signal }));
  const toggle = (id: string) => setChosen(ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]);
  const download = async (name: BenchmarkExport) => {
    setDownloading(name);
    setError(null);
    try {
      await exportsApi.benchmark(ids, name);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "client_error", "Download failed."));
    } finally {
      setDownloading(null);
    }
  };
  const s = summary.data;
  return (
    <Panel kicker="Benchmark" title="Benchmark diagnostics over several runs">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          <span>{ids.length} run(s) chosen</span>
          <Button size="sm" variant="ghost" onClick={() => setChosen(list.slice(0, DEFAULT_RUNS).map((r) => r.run_id))}>Latest {DEFAULT_RUNS}</Button>
          <Button size="sm" variant="ghost" onClick={() => setChosen(list.map((r) => r.run_id))}>All</Button>
          <Button size="sm" variant="ghost" onClick={() => setChosen([])}>None</Button>
        </div>
        <ul aria-label="Runs for benchmark diagnostics" className="grid max-h-64 gap-1 overflow-y-auto rounded-card border border-line bg-white/[0.02] p-1.5 md:grid-cols-2">
          {list.map((r) => (
            <li key={r.run_id}>
              <label className={cx("flex cursor-pointer items-center gap-2.5 rounded-xl px-3 py-1.5 text-sm",
                                   ids.includes(r.run_id) ? "bg-accent/15 text-ink" : "text-ink-muted hover:bg-white/[0.05]")}>
                <input type="checkbox" checked={ids.includes(r.run_id)} onChange={() => toggle(r.run_id)}
                       className="h-4 w-4 shrink-0 accent-[rgb(var(--c-accent))]" />
                <span dir="auto" className="min-w-0 truncate">{r.label}</span>
                <Mono className="ml-auto shrink-0 text-[10.5px]">{r.run_id}</Mono>
              </label>
            </li>
          ))}
          {!list.length && <li className="px-3 py-4 text-sm text-ink-muted">No runs yet.</li>}
        </ul>
        {ids.length > 0 && (summary.loading ? <SkeletonRows rows={1} label="Computing diagnostics" />
          : summary.error ? <ErrorState error={summary.error} onRetry={summary.refresh} />
          : s && (
            <p className="text-sm text-ink-muted">
              {s.vehicles} vehicle run(s) · mean turns {formatValue(s.mean_turns)} · mean searches {formatValue(s.mean_searches)}
              {" "}· sweep resolution rate {formatValue(s.sweep_resolution_rate)}
            </p>
          ))}
        <div className="flex flex-wrap gap-2">
          {BENCHMARK_FILES.map((name) => (
            <Button key={name} size="sm" busy={downloading === name}
                    disabled={!ids.length || !s || !s.files.includes(name) || downloading !== null}
                    onClick={() => void download(name)}><IconDownload size={14} />{name}</Button>
          ))}
        </div>
        {error && <ErrorState error={error} title="The download failed" />}
      </div>
    </Panel>
  );
}

/** "Compare batches": aggregate observation metrics of every recorded batch (computed on demand). */
function CompareBatches() {
  const [open, setOpen] = useState(false);
  const batches = useResource(open ? "benchmark-batches" : null, (signal) => api.benchmarkBatches({ signal }));
  return (
    <Panel kicker="Benchmark" title="Compare batches">
      {!open ? (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-ink-muted">Observation metrics of every recorded batch side by side (reads every run; may take a moment).</p>
          <Button size="sm" onClick={() => setOpen(true)}>Compare batches</Button>
        </div>
      ) : batches.loading ? <SkeletonRows rows={3} label="Loading batches" />
        : batches.error ? <ErrorState error={batches.error} onRetry={batches.refresh} />
        : batches.data?.batches.length ? <DataTable rows={batches.data.batches} columns={batches.data.columns} label="Batches" />
        : <EmptyState title="No batches recorded yet" />}
    </Panel>
  );
}

export function DiagnosticsPage() {
  const { config, health, runs } = useWorkspace();
  const cfg = config.data;
  const [runId, setRunId] = useState("");
  const diag = useResource(runId ? `diag:${runId}` : null, (signal) => api.diagnostics(runId, { signal }));
  const storage = cfg?.storage ?? {};
  const access = cfg?.access_control ?? {};
  const reach = useResource(cfg?.configured ? "reachability" : null, (signal) => api.reachability({ signal }));

  return (
    <div className="space-y-6">
      <PageHeader kicker="System" title="Diagnostics">
        Server health and configuration (presence only, never a secret value), and the benchmark diagnostics of any run.
      </PageHeader>
      <div className="grid gap-6 xl:grid-cols-2">
        <Panel kicker="Server" title="Health and environment">
          {config.loading ? <SkeletonRows rows={5} label="Loading configuration" />
            : config.error && !cfg ? <ErrorState error={config.error} onRetry={config.refresh} />
            : cfg && (
              <KeyValue columns={2} items={[
                { label: "Backend", value: <Badge tone={health.data?.status === "ok" ? "ok" : "danger"}>{health.data?.status === "ok" ? "healthy" : "unreachable"}</Badge> },
                { label: "Environment", value: <Badge tone="violet">{cfg.environment}</Badge> },
                { label: "Configured", value: <Badge tone={cfg.configured ? "ok" : "danger"}>{cfg.configured ? "yes" : "no"}</Badge> },
                { label: "Blocking checks", value: cfg.blocking.length ? cfg.blocking.join(", ") : "none" },
                { label: "GLM endpoint", value: reach.data?.checked
                    ? <Badge tone={reach.data.reachable ? "ok" : "warn"}>{reach.data.reachable ? "Connected" : "Configured · unreachable"} · {reach.data.detail}</Badge>
                    : reach.loading ? "checking…" : "not checked" },
                { label: "Research model", value: <Mono className="text-ink">{cfg.research_model ?? "not set"}</Mono> },
                { label: "Finalizer model", value: <Mono className="text-ink">{cfg.finalizer_model ?? "the research model"}</Mono> },
                { label: "Search backend", value: cfg.search_backend },
                { label: "Level 1.5 source", value: cfg.level15_source },
                { label: "Access control", value: `${access.required ? "required" : "not required"} · token ${access.configured ? "configured" : "not configured"}` },
                { label: "MCP", value: cfg.mcp_enabled ? "enabled (read-only)" : "disabled" },
                { label: "Max active runs", value: cfg.max_active_runs },
                { label: "Prompt version", value: <Mono>{cfg.prompt_version}</Mono> },
              ]} />
            )}
        </Panel>
        <Panel kicker="Server" title="Checks and storage">
          {cfg ? (
            <div className="space-y-4">
              <ul className="space-y-2">
                {cfg.checks.map((c) => (
                  <li key={c.name} className="rounded-xl bg-white/[0.03] px-3 py-2">
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <span className="text-ink">{c.name}</span>
                      <Badge tone={c.level === "ok" ? "ok" : c.level === "warning" ? "warn" : "danger"}>{c.status}</Badge>
                    </div>
                    {c.detail && <p className="mt-1 text-xs text-ink-muted">{c.detail}</p>}
                  </li>
                ))}
              </ul>
              <KeyValue columns={2} items={Object.entries(storage).map(([k, v]) => ({ label: humanize(k), value: k === "data_dir" || k === "volume_mount" ? <Mono>{formatValue(v)}</Mono> : formatValue(v) }))} />
            </div>
          ) : <SkeletonRows rows={4} />}
        </Panel>
      </div>
      <Panel kicker="Environment" title="Environment overrides">
        {cfg?.env_overrides.length ? (
          <ul className="space-y-1.5">
            {cfg.env_overrides.map((o, i) => <li key={i}><Mono className="text-ink">{str(o.text) ?? JSON.stringify(o)}</Mono></li>)}
          </ul>
        ) : <p className="text-sm text-ink-muted">No experiment-relevant environment variable differs from its code default.</p>}
      </Panel>
      <BenchmarkExports />
      <CompareBatches />
      <Panel kicker="Runs" title="Run diagnostics">
        <select aria-label="Run for diagnostics" className="input mb-4 lg:max-w-xl" value={runId} onChange={(e) => setRunId(e.target.value)}>
          <option value="">Choose a run…</option>
          {runs.data?.runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.label} · {r.run_id}</option>)}
        </select>
        {!runId ? <EmptyState title="Choose a run to see its benchmark diagnostics" />
          : diag.loading ? <TabSkeleton />
          : diag.error ? <ErrorState error={diag.error} onRetry={diag.refresh} />
          : diag.data && <RunDiagnosticsView data={diag.data} />}
      </Panel>
    </div>
  );
}

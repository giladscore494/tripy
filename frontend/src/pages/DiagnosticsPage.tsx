import { useState } from "react";

import { api } from "../api/endpoints";
import { RunDiagnosticsView, TabSkeleton } from "../components/run/TechnicalViews";
import { Badge, EmptyState, ErrorState, KeyValue, Mono, Panel, SkeletonRows } from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";
import { formatValue, humanize, str } from "../lib/format";

export function DiagnosticsPage() {
  const { config, health, runs } = useWorkspace();
  const cfg = config.data;
  const [runId, setRunId] = useState("");
  const diag = useResource(runId ? `diag:${runId}` : null, (signal) => api.diagnostics(runId, { signal }));
  const storage = cfg?.storage ?? {};
  const access = cfg?.access_control ?? {};

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

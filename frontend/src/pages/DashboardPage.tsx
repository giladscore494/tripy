import { Link } from "react-router-dom";

import { IconSpark } from "../components/ui/icons";
import { Badge, Button, EmptyState, ErrorState, KeyValue, Mono, Panel, SkeletonRows, Stat } from "../components/ui/primitives";
import { RunRow } from "../components/run/RunBits";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useNow } from "../hooks/useNow";
import { PageHeader } from "../layouts/AppShell";
import { str } from "../lib/format";

export function DashboardPage() {
  const { runs, config, activeRuns } = useWorkspace();
  const now = useNow(activeRuns.length > 0, 5000);
  const list = runs.data?.runs ?? [];
  const failed = list.filter((r) => r.status === "FAILED" || r.status === "INTERRUPTED");
  const recent = list.filter((r) => !r.active).slice(0, 8);
  const cfg = config.data;
  const storage = cfg?.storage ?? {};

  return (
    <div className="space-y-6">
      <PageHeader kicker="TRIPY" title="Research Intelligence Workspace"
                  actions={<Link to="/research/new"><Button variant="primary"><IconSpark size={16} />New Research</Button></Link>}>
        Vehicle specification research over the TRIPY engine: live runs, evidence, documents and diagnostics.
      </PageHeader>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Active research" value={runs.data ? activeRuns.length : "—"} tone={activeRuns.length ? "accent" : undefined}
              hint={cfg ? `limit ${cfg.max_active_runs} at once` : undefined} />
        <Stat label="Runs recorded" value={runs.data ? runs.data.total : "—"} />
        <Stat label="Failed / interrupted" value={runs.data ? failed.length : "—"} tone={failed.length ? "warn" : undefined} />
        <Stat label="Configuration" value={cfg ? (cfg.configured ? "Ready" : "Blocked") : "—"}
              tone={cfg && !cfg.configured ? "danger" : undefined}
              hint={cfg?.blocking.length ? cfg.blocking.join(", ") : cfg ? "no blocking check" : undefined} />
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="space-y-6">
          <Panel kicker="Live" title="Active research">
            {runs.loading ? <SkeletonRows rows={2} label="Loading active runs" />
              : runs.error && !runs.data ? <ErrorState error={runs.error} onRetry={runs.refresh} />
              : activeRuns.length ? (
                <ul className="space-y-2">{activeRuns.map((r) => <RunRow key={r.run_id} run={r} profiles={cfg?.profiles} now={now} />)}</ul>
              ) : (
                <EmptyState title="No research is running">Start a run from New Research; it keeps running on the server if you close this tab.</EmptyState>
              )}
          </Panel>
          <Panel kicker="History" title="Recent runs" actions={<Link to="/runs" className="text-sm text-accent-soft hover:underline">All runs</Link>}>
            {runs.loading ? <SkeletonRows rows={4} label="Loading runs" />
              : recent.length ? <ul className="space-y-2">{recent.map((r) => <RunRow key={r.run_id} run={r} profiles={cfg?.profiles} now={now} />)}</ul>
              : !runs.error && <EmptyState title="No finished runs yet" />}
          </Panel>
          {failed.length > 0 && (
            <Panel kicker="Attention" title="Failed or interrupted">
              <ul className="space-y-2">{failed.slice(0, 5).map((r) => <RunRow key={r.run_id} run={r} profiles={cfg?.profiles} now={now} />)}</ul>
            </Panel>
          )}
        </div>

        <Panel kicker="System" title="Server configuration"
               actions={<Link to="/diagnostics" className="text-sm text-accent-soft hover:underline">Diagnostics</Link>}>
          {config.loading ? <SkeletonRows rows={4} label="Loading configuration" />
            : config.error && !cfg ? <ErrorState error={config.error} onRetry={config.refresh} />
            : cfg && (
              <div className="space-y-5">
                <KeyValue columns={2} items={[
                  { label: "Research model", value: <Mono className="text-ink">{cfg.research_model ?? "not set"}</Mono> },
                  { label: "Finalizer model", value: <Mono className="text-ink">{cfg.finalizer_model ?? "research model"}</Mono> },
                  { label: "Search backend", value: cfg.search_backend },
                  { label: "Level 1.5 source", value: cfg.level15_source },
                  { label: "Storage", value: <Badge tone={storage.level === "ok" ? "ok" : storage.level === "error" ? "danger" : "warn"}>
                      {str(storage.message) ?? String(storage.level ?? "unknown")}</Badge> },
                  { label: "Prompt version", value: <Mono>{cfg.prompt_version}</Mono> },
                ]} />
                <ul className="space-y-1.5">
                  {cfg.checks.map((c) => (
                    <li key={c.name} className="flex items-start justify-between gap-3 rounded-xl bg-white/[0.03] px-3 py-2 text-sm">
                      <span className="text-ink">{c.name}</span>
                      <Badge tone={c.level === "ok" ? "ok" : c.level === "warning" ? "warn" : "danger"} title={c.detail}>{c.status}</Badge>
                    </li>
                  ))}
                </ul>
              </div>
            )}
        </Panel>
      </div>
    </div>
  );
}

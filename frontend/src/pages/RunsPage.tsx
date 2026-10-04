import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { RunRow } from "../components/run/RunBits";
import { IconSearch, IconSpark } from "../components/ui/icons";
import { Button, EmptyState, ErrorState, Panel, SkeletonRows, Tabs } from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useNow } from "../hooks/useNow";
import { PageHeader } from "../layouts/AppShell";
import { STATUS_FILTERS } from "../lib/status";

type StatusFilter = (typeof STATUS_FILTERS)[number]["id"];

export function RunsPage() {
  const { runs, config, activeRuns } = useWorkspace();
  const now = useNow(activeRuns.length > 0, 5000);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [profile, setProfile] = useState("");
  const all = runs.data?.runs ?? [];

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return all.filter((run) => {
      if (status === "active" ? !run.active : status !== "all" && run.status !== status) return false;
      if (profile && run.profile !== profile) return false;
      if (!q) return true;
      return [run.label, run.run_id, run.status_label, run.profile ?? "", ...run.record_ids]
        .some((part) => part.toLowerCase().includes(q));
    });
  }, [all, query, status, profile]);

  const counts = useMemo(() => Object.fromEntries(STATUS_FILTERS.map((f) => [f.id,
    f.id === "all" ? all.length : f.id === "active" ? all.filter((r) => r.active).length
      : all.filter((r) => r.status === f.id).length])), [all]);

  return (
    <div className="space-y-6">
      <PageHeader kicker="History" title="Runs"
                  actions={<Link to="/research/new"><Button variant="primary"><IconSpark size={16} />New Research</Button></Link>}>
        Every run recorded on the server, newest first. Active runs update automatically.
      </PageHeader>
      <Panel>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <IconSearch size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
            <input aria-label="Search runs" className="input pl-9" placeholder="Search label, run id, vehicle record id…"
                   value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <select aria-label="Profile" className="input lg:w-60" value={profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">All profiles</option>
            {config.data?.profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
        </div>
        <div className="mt-4">
          <Tabs label="Status filter" value={status} onChange={setStatus}
                tabs={STATUS_FILTERS.map((f) => ({ id: f.id, label: f.label, count: runs.data ? counts[f.id] : null }))} />
        </div>
        <div className="mt-4">
          {runs.loading ? <SkeletonRows rows={6} label="Loading runs" />
            : runs.error && !runs.data ? <ErrorState error={runs.error} onRetry={runs.refresh} />
            : filtered.length ? (
              <ul className="space-y-2">
                {filtered.map((run) => <RunRow key={run.run_id} run={run} profiles={config.data?.profiles} now={now} />)}
              </ul>
            ) : (
              <EmptyState title={all.length ? "No run matches these filters" : "No runs yet"}>
                {all.length ? "Clear the search or pick another status." : "Start the first research run from New Research."}
              </EmptyState>
            )}
        </div>
      </Panel>
    </div>
  );
}

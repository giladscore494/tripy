import { useState } from "react";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import { Badge, Button, ErrorState, KeyValue, Mono, Notice, Panel, SkeletonRows } from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";

export function SettingsPage() {
  const { config } = useWorkspace();
  const contract = useResource("run-settings", (signal) => api.runSettings({ signal }));
  const catalog = useResource("catalog-status", (signal) => api.catalogStatus({ signal }));
  const [rebuild, setRebuild] = useState<{ busy: boolean; message: string | null; error: ApiError | null }>(
    { busy: false, message: null, error: null });
  const cfg = config.data;
  const c = contract.data;
  const index = catalog.data?.derived_index;
  const rebuildNow = async () => {
    setRebuild({ busy: true, message: null, error: null });
    try {
      const out = await api.rebuildCatalogIndex();
      setRebuild({ busy: false, message: out.message, error: null });
      void catalog.refresh();
    } catch (e) {
      setRebuild({ busy: false, message: null,
                   error: e instanceof ApiError ? e : new ApiError(0, "client_error", "The rebuild could not start.") });
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader kicker="Settings" title="Settings">
        Server configuration is read-only here. Per-run settings are chosen on New Research for one run at a time.
      </PageHeader>
      <Notice tone="info" title="Nothing on this page changes the server">
        Railway environment variables and the server&apos;s defaults are managed on the deployment. The workspace never
        mutates them; per-run settings apply only to the run they are submitted with.
      </Notice>
      <Panel kicker="Read-only" title="Server configuration">
        {config.loading ? <SkeletonRows rows={4} /> : config.error && !cfg ? <ErrorState error={config.error} onRetry={config.refresh} /> : cfg && (
          <KeyValue columns={3} items={[
            { label: "Environment", value: cfg.environment },
            { label: "Research model", value: <Mono className="text-ink">{cfg.research_model ?? "not set"}</Mono> },
            { label: "Finalizer model", value: <Mono className="text-ink">{cfg.finalizer_model ?? "the research model"}</Mono> },
            { label: "Search backend", value: cfg.search_backend },
            { label: "Level 1.5 source", value: cfg.level15_source },
            { label: "Default run profile", value: cfg.profiles.find((p) => p.id === cfg.default_profile)?.label ?? cfg.default_profile },
            { label: "Max active runs", value: cfg.max_active_runs },
            { label: "Prompt version", value: <Mono>{cfg.prompt_version}</Mono> },
            { label: "MCP", value: cfg.mcp_enabled ? "enabled" : "disabled" },
          ]} />
        )}
      </Panel>
      <Panel kicker="Live catalog" title="Catalog and derived trim index">
        {catalog.loading ? <SkeletonRows rows={3} /> : catalog.error ? <ErrorState error={catalog.error} onRetry={catalog.refresh} />
          : catalog.data && index && (
            <div className="space-y-3">
              <KeyValue columns={3} items={[
                { label: "Level 1.5 catalog", value: catalog.data.label },
                { label: "Derived trim index", value: index.available ? (index.status ?? "never built") : "unavailable (no DATABASE_URL)" },
                { label: "Last build", value: <Mono>{index.built_at ?? "—"}</Mono> },
                { label: "Catalog rows", value: index.rows ?? "—" },
                { label: "Index entries", value: index.entries ?? "—" },
                { label: "Schedule", value: `every ${index.interval_days} days` },
              ]} />
              {index.error && <Notice tone="danger" title="Last build failed">{index.error}</Notice>}
              <div className="flex items-center gap-3">
                <Button size="sm" onClick={rebuildNow} busy={rebuild.busy || index.building} disabled={!index.available || index.building}>
                  Rebuild now
                </Button>
                {rebuild.message && <span className="text-xs text-ink-muted">{rebuild.message}</span>}
              </div>
              {rebuild.error && <ErrorState error={rebuild.error} title="The rebuild did not start" />}
              <p className="text-xs text-ink-faint">The engine reads this copy (on the data volume) when it is newer than the
                repository&apos;s data/catalog_trim_index.json. Nothing is written to the MILO catalog.</p>
            </div>
          )}
      </Panel>
      <Panel kicker="Per run" title="Per-run settings contract">
        {contract.loading ? <SkeletonRows rows={6} label="Loading per-run settings" />
          : contract.error ? <ErrorState error={contract.error} onRetry={contract.refresh} />
          : c && (
            <div className="space-y-6">
              <p className="text-sm text-ink-muted">{c.profile_note}</p>
              {c.groups.map((group) => (
                <div key={group}>
                  <p className="kicker mb-2">{group}</p>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr><th>Setting</th><th>Default</th><th>Allowed</th><th>Named profiles</th></tr></thead>
                      <tbody>
                        {c.settings.filter((s) => s.group === group).map((s) => (
                          <tr key={s.name}>
                            <td><span className="text-ink">{s.label}</span><Mono className="block text-[11px]">{s.name}</Mono></td>
                            <td><Mono className="text-ink">{s.default === null ? "not set" : s.default === "" ? "empty" : String(s.default)}</Mono></td>
                            <td className="text-xs text-ink-muted">
                              {s.options.length ? s.options.join(" · ") : s.min !== null ? `${s.min} – ${s.max}` : s.kind === "bool" ? "on / off" : s.kind}
                            </td>
                            <td>{s.pinned_by_named_profile ? <Badge tone="violet">pinned</Badge> : <span className="text-xs text-ink-faint">applies</span>}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
              <div className="grid gap-6 lg:grid-cols-2">
                <div>
                  <p className="kicker mb-2">Server-controlled</p>
                  <ul className="space-y-2 text-sm">
                    {c.server_controlled.map((s) => <li key={s.name}><span className="text-ink">{s.label}</span><span className="block text-xs text-ink-muted">{s.reason}</span></li>)}
                  </ul>
                </div>
                <div>
                  <p className="kicker mb-2">In-flight limits (process-wide, read-only)</p>
                  <KeyValue columns={1} items={[
                    ...(c.concurrency.research_model ? [{ label: `Research model · ${c.concurrency.research_model.model}`,
                      value: `${c.concurrency.research_model.current} in flight · provider limit ${c.concurrency.research_model.provider_limit ?? "unknown"}` }] : []),
                    ...(c.concurrency.finalizer_model ? [{ label: `Finalizer model · ${c.concurrency.finalizer_model.model}`,
                      value: `${c.concurrency.finalizer_model.current} in flight · provider limit ${c.concurrency.finalizer_model.provider_limit ?? "unknown"}` }] : []),
                    { label: "Search", value: `${c.concurrency.search.current} in flight · provider limit ${c.concurrency.search.provider_limit}` },
                  ]} />
                  <p className="mt-2 text-xs text-ink-faint">{c.concurrency.note}</p>
                </div>
              </div>
            </div>
          )}
      </Panel>
    </div>
  );
}

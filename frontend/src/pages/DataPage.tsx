import { useState } from "react";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { PolicyEntry } from "../api/types";
import {
  Badge, Button, ErrorState, ExternalLink, Field, Mono, Notice, Panel, SkeletonRows,
} from "../components/ui/primitives";
import { useResource } from "../hooks/useResource";
import type { Tone } from "../lib/status";
import { PageHeader } from "../layouts/AppShell";

// The Data page (identity anchors PR): the open-data snapshots (last build, rows, source URL, Rebuild now), the
// licence / attribution list, and the production source policy with the operator's blocked <-> allowed switch.
// Nothing here edits the environment: a switch is an overlay in the data volume and every change is logged.

const POLICY_TONE: Record<string, Tone> = {
  allowed: "ok", identity_only: "warn", blocked: "danger",
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export function DataPage() {
  const datasets = useResource("data-datasets", (signal) => api.dataDatasets({ signal }));
  const policy = useResource("data-policy", (signal) => api.dataPolicy({ signal }));
  const [rebuild, setRebuild] = useState<{ busy: boolean; message: string | null; error: ApiError | null }>(
    { busy: false, message: null, error: null });
  const [form, setForm] = useState({ domain: "", policy: "allowed" as "allowed" | "blocked", terms: "",
                                     checked: today() });
  const [saving, setSaving] = useState<{ busy: boolean; message: string | null; error: ApiError | null }>(
    { busy: false, message: null, error: null });

  const rebuildNow = async (dataset?: string) => {
    setRebuild({ busy: true, message: null, error: null });
    try {
      const out = await api.rebuildDatasets(dataset);
      setRebuild({ busy: false, message: out.message, error: null });
      void datasets.refresh();
    } catch (e) {
      setRebuild({ busy: false, message: null,
                   error: e instanceof ApiError ? e : new ApiError(0, "client_error", "The rebuild could not start.") });
    }
  };

  const submit = async () => {
    setSaving({ busy: true, message: null, error: null });
    try {
      const out = await api.changePolicy({ domain: form.domain.trim(), policy: form.policy,
                                           terms_clause: form.terms.trim() || null, checked_at: form.checked || null });
      setSaving({ busy: false, message: `${out.change.domain}: ${out.change.from} → ${out.change.to}`, error: null });
      setForm({ ...form, domain: "", terms: "" });
      void policy.refresh();
    } catch (e) {
      setSaving({ busy: false, message: null,
                  error: e instanceof ApiError ? e : new ApiError(0, "client_error", "The change was not saved.") });
    }
  };

  const pick = (entry: PolicyEntry, domain: string) =>
    setForm({ domain, policy: entry.policy === "allowed" ? "blocked" : "allowed", terms: entry.terms_clause ?? "",
              checked: today() });

  return (
    <div className="space-y-6">
      <PageHeader kicker="Data" title="Open data and source policy">
        The open structured datasets the engine reads (local snapshots in the data volume) and the production source
        policy: a domain is fetched and used as evidence only when it is listed as allowed.
      </PageHeader>

      <Panel kicker="Open datasets" title="Snapshots"
        actions={<Button size="sm" onClick={() => rebuildNow()} busy={rebuild.busy || !!datasets.data?.building}
                         disabled={!!datasets.data?.building}>Rebuild all now</Button>}>
        {datasets.loading ? <SkeletonRows rows={4} /> : datasets.error ? <ErrorState error={datasets.error} onRetry={datasets.refresh} />
          : datasets.data && (
            <div className="space-y-3">
              <div className="table-wrap">
                <table className="data-table">
                  <thead><tr><th>Dataset</th><th>Policy</th><th>Last build</th><th>Rows</th><th>Source</th><th /></tr></thead>
                  <tbody>
                    {datasets.data.datasets.map((d) => (
                      <tr key={d.dataset}>
                        <td><span className="text-ink">{d.label ?? d.dataset}</span><Mono className="block text-[11px]">{d.dataset}</Mono></td>
                        <td><Badge tone={POLICY_TONE[d.policy ?? "blocked"] ?? "neutral"}>{d.policy ?? "blocked"}</Badge></td>
                        <td>
                          <Mono>{d.snapshot_built_at ?? "never built"}</Mono>
                          {d.last_build?.status && d.last_build.status !== "built" && (
                            <span className="block text-[11px] text-danger">
                              {d.last_build.status}: {d.last_build.reason ?? d.last_build.error ?? ""}
                            </span>
                          )}
                        </td>
                        <td>{d.snapshot_rows ?? "—"}</td>
                        <td>{d.source_url ? <ExternalLink href={d.source_url}>source</ExternalLink> : "—"}</td>
                        <td>
                          {!d.identity_only && (
                            <Button size="sm" variant="ghost" onClick={() => rebuildNow(d.dataset)}
                                    disabled={!!datasets.data?.building}>Rebuild</Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {rebuild.message && <span className="text-xs text-ink-muted">{rebuild.message}</span>}
              {rebuild.error && <ErrorState error={rebuild.error} title="The rebuild did not start" />}
              <p className="text-xs text-ink-faint">Monthly schedule (every {datasets.data.interval_days} days). A build
                reads the live header first and stops with a report when a mapped column is absent; the previous
                snapshot stays in use.</p>
            </div>
          )}
      </Panel>

      <Panel kicker="Licences" title="Licences and attribution">
        {datasets.data && (
          <ul className="space-y-2 text-sm">
            {datasets.data.datasets.map((d) => (
              <li key={d.dataset}>
                <span className="text-ink">{d.label ?? d.dataset}</span>
                <span className="block text-xs text-ink-muted">{d.licence ?? "no licence recorded"}</span>
                {d.attribution && <span className="block text-xs text-ink-faint">Attribution: {d.attribution}</span>}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel kicker="Production" title="Source policy">
        {policy.loading ? <SkeletonRows rows={6} /> : policy.error ? <ErrorState error={policy.error} onRetry={policy.refresh} />
          : policy.data && (
            <div className="space-y-5">
              <Notice tone="info" title="Unlisted domains are blocked">
                Search results on a blocked domain are dropped before fetch, the fetch tools refuse it and its cached
                documents are never evidence (they stay on disk). Switching a domain is stored as an overlay in the
                data volume; the repository file data/source_policy.json stays the reviewed baseline.
              </Notice>
              <div className="table-wrap">
                <table className="data-table">
                  <thead><tr><th>Source</th><th>Domains</th><th>Policy</th><th>Licence</th><th>Terms clause</th><th>Checked</th></tr></thead>
                  <tbody>
                    {policy.data.entries.map((e) => (
                      <tr key={e.id}>
                        <td><span className="text-ink">{e.id}</span>{e.overlay && <Badge tone="violet" className="ml-2">overlay</Badge>}</td>
                        <td className="text-xs">
                          {e.domains.map((d) => (
                            <button key={d} type="button" className="mr-2 underline decoration-dotted" onClick={() => pick(e, d)}
                                    title="Prefill the switch below">{d}</button>
                          ))}
                        </td>
                        <td><Badge tone={POLICY_TONE[e.policy] ?? "neutral"}>{e.policy}</Badge></td>
                        <td className="text-xs text-ink-muted">{e.licence ?? "—"}</td>
                        <td className="text-xs text-ink-muted">{e.terms_clause ?? "—"}
                          {e.terms_clause_source && <span className="block text-ink-faint">({e.terms_clause_source})</span>}</td>
                        <td><Mono>{e.checked_at ?? "—"}</Mono></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <Field label="Domain" htmlFor="policy-domain">
                  <input id="policy-domain" className="input" dir="ltr" value={form.domain} placeholder="bmw.co.il"
                         onChange={(e) => setForm({ ...form, domain: e.target.value })} />
                </Field>
                <Field label="Policy" htmlFor="policy-value">
                  <select id="policy-value" className="input" value={form.policy}
                          onChange={(e) => setForm({ ...form, policy: e.target.value as "allowed" | "blocked" })}>
                    <option value="allowed">allowed</option>
                    <option value="blocked">blocked</option>
                  </select>
                </Field>
                <Field label="Terms clause the decision rests on" htmlFor="policy-terms"
                       hint="Quote the site's own terms (required to allow a domain).">
                  <textarea id="policy-terms" className="input min-h-[4rem]" dir="auto" value={form.terms}
                            onChange={(e) => setForm({ ...form, terms: e.target.value })} />
                </Field>
                <Field label="Checked on" htmlFor="policy-checked">
                  <input id="policy-checked" type="date" className="input" value={form.checked}
                         onChange={(e) => setForm({ ...form, checked: e.target.value })} />
                </Field>
              </div>
              <div className="flex items-center gap-3">
                <Button size="sm" onClick={submit} busy={saving.busy} disabled={!form.domain.trim()}>Save policy</Button>
                {saving.message && <span className="text-xs text-ink-muted">{saving.message}</span>}
              </div>
              {saving.error && <ErrorState error={saving.error} title="The policy was not changed" />}
              <div>
                <p className="kicker mb-2">Change log</p>
                {policy.data.changes.length === 0 ? <p className="text-xs text-ink-faint">No operator change yet.</p> : (
                  <ul className="space-y-1 text-xs">
                    {[...policy.data.changes].reverse().map((c, i) => (
                      <li key={`${c.at}-${i}`}><Mono>{c.at}</Mono> {c.domain}: {c.from} → {c.to}
                        {c.terms_clause && <span className="text-ink-faint"> — “{c.terms_clause}”</span>}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
      </Panel>
    </div>
  );
}

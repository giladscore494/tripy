import { useState } from "react";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { DatasetRow, PolicyEntry, StorageStatus } from "../api/types";
import {
  Badge, Button, ErrorState, ExternalLink, Field, KeyValue, Mono, Notice, Panel, SkeletonRows, Stat,
} from "../components/ui/primitives";
import { useResource } from "../hooks/useResource";
import type { Tone } from "../lib/status";
import { PageHeader } from "../layouts/AppShell";

// The Data page: the open-data snapshots (built by the build-open-data GitHub Action, committed to data/open/ and read
// from the deploy image; nothing is built on the server), the data volume's storage with the "Delete open-data files"
// action, the licence / attribution list, and the production source policy with the operator's blocked <-> allowed
// switch (an overlay in the data volume; every change logged).

const POLICY_TONE: Record<string, Tone> = {
  allowed: "ok", identity_only: "warn", blocked: "danger",
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function megabytes(bytes?: number | null): string {
  if (bytes == null) return "—";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function absentText(row: DatasetRow): string | null {
  const absent = row.absent_columns;
  if (!absent) return null;
  if (Array.isArray(absent)) return absent.length ? absent.join(", ") : null;
  const parts = Object.entries(absent).filter(([, cols]) => cols.length).map(([k, cols]) => `${k}: ${cols.join(", ")}`);
  return parts.length ? parts.join(" · ") : null;
}

function yearsText(row: DatasetRow): string | null {
  const years = (row.years ?? []).filter((y) => y.status === "built").map((y) => String(y.year));
  if (years.length) return years.join(", ");
  const files = (row.files ?? []).filter((f) => f.status === "built").length;
  return files ? `${files} file(s)${row.last_file_year ? `, last file year ${row.last_file_year}` : ""}` : null;
}

function unknownUnits(row: DatasetRow): string[] {
  return Object.entries(row.column_units ?? {}).filter(([, u]) => u.unit === "unknown" && !u.unit_of).map(([k]) => k);
}

function StoragePanel() {
  const storage = useResource("data-storage", (signal) => api.dataStorage({ signal }));
  const [confirming, setConfirming] = useState(false);
  const [state, setState] = useState<{ busy: boolean; message: string | null; error: ApiError | null }>(
    { busy: false, message: null, error: null });
  const [fresh, setFresh] = useState<StorageStatus | null>(null);
  const data = fresh ?? storage.data;

  const remove = async () => {
    setState({ busy: true, message: null, error: null });
    try {
      const out = await api.deleteOpenData();
      setFresh(out.storage);
      setConfirming(false);
      setState({ busy: false, error: null,
                 message: out.deleted ? `Deleted ${out.path} (${megabytes(out.bytes_freed)} freed).` : "Nothing to delete." });
    } catch (e) {
      setState({ busy: false, message: null,
                 error: e instanceof ApiError ? e : new ApiError(0, "client_error", "The files were not deleted.") });
    }
  };

  return (
    <Panel kicker="Data volume" title="Storage">
      {storage.loading && !data ? <SkeletonRows rows={4} /> : storage.error && !data
        ? <ErrorState error={storage.error} onRetry={storage.refresh} />
        : data && (
          <div className="space-y-4">
            {data.low && (
              <Notice tone="danger" title="The data volume is almost full">
                Less than {megabytes(data.min_free_bytes)} free: new runs and builds are refused (insufficient_disk)
                until space is freed.
              </Notice>
            )}
            <div className="grid gap-3 sm:grid-cols-3">
              <Stat label="Total" value={megabytes(data.volume.total)} />
              <Stat label="Used" value={megabytes(data.volume.used)} />
              <Stat label="Free" value={megabytes(data.volume.free)} tone={data.low ? "danger" : undefined} />
            </div>
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>Folder</th><th>Size</th></tr></thead>
                <tbody>
                  {data.folders.map((f) => (
                    <tr key={f.path}><td><Mono>{f.name}</Mono></td><td>{megabytes(f.bytes)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            {data.startup_cleanup && data.startup_cleanup.removed.length > 0 && (
              <p className="text-xs text-ink-muted">At startup {data.startup_cleanup.removed.length} partial or unbuilt
                open-data file(s) were removed ({megabytes(data.startup_cleanup.bytes_freed)} freed).</p>
            )}
            <div className="flex flex-wrap items-center gap-3">
              {!confirming ? (
                <Button size="sm" variant="ghost" onClick={() => setConfirming(true)}>Delete open-data files</Button>
              ) : (
                <>
                  <span className="text-xs text-ink">Delete everything in <Mono>{data.open_data_dir}</Mono>? The
                    snapshots the engine reads come from the deploy image and are not affected.</span>
                  <Button size="sm" onClick={remove} busy={state.busy}>Confirm delete</Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
                </>
              )}
              {state.message && <span className="text-xs text-ink-muted">{state.message}</span>}
            </div>
            {state.error && <ErrorState error={state.error} title="The open-data files were not deleted" />}
          </div>
        )}
    </Panel>
  );
}

export function DataPage() {
  const datasets = useResource("data-datasets", (signal) => api.dataDatasets({ signal }));
  const policy = useResource("data-policy", (signal) => api.dataPolicy({ signal }));
  const [form, setForm] = useState({ domain: "", policy: "allowed" as "allowed" | "blocked", terms: "",
                                     checked: today() });
  const [saving, setSaving] = useState<{ busy: boolean; message: string | null; error: ApiError | null }>(
    { busy: false, message: null, error: null });

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
        The open structured datasets the engine reads (snapshots built by the build-open-data GitHub Action and shipped
        in the deploy image), the data volume, and the production source policy: a domain is fetched and used as
        evidence only when it is listed as allowed.
      </PageHeader>

      <Panel kicker="Open datasets" title="Snapshots">
        {datasets.loading ? <SkeletonRows rows={4} /> : datasets.error ? <ErrorState error={datasets.error} onRetry={datasets.refresh} />
          : datasets.data && (
            <div className="space-y-3">
              <KeyValue columns={2} items={[
                { label: "Built", value: <Mono>{datasets.data.manifest_built_at ?? "never (run the build-open-data Action)"}</Mono> },
                { label: "Action run", value: datasets.data.run_url
                  ? <ExternalLink href={datasets.data.run_url}>build-open-data</ExternalLink> : "—" },
              ]} />
              <div className="table-wrap">
                <table className="data-table">
                  <thead><tr><th>Dataset</th><th>Policy</th><th>Snapshot</th><th>Rows</th><th>Years / files</th><th>Source</th></tr></thead>
                  <tbody>
                    {datasets.data.datasets.map((d) => (
                      <tr key={d.dataset}>
                        <td><span className="text-ink">{d.label ?? d.dataset}</span><Mono className="block text-[11px]">{d.dataset}</Mono></td>
                        <td><Badge tone={POLICY_TONE[d.policy ?? "blocked"] ?? "neutral"}>{d.policy ?? "blocked"}</Badge></td>
                        <td>
                          {d.identity_only ? <span className="text-ink-faint">identity only (on-demand decode)</span>
                            : d.available ? <Badge tone="ok">available</Badge>
                            : <Badge tone="warn">{d.problem === "no_manifest_entry" ? "not built" : d.problem ?? "not built"}</Badge>}
                          {d.built_at && <Mono className="block text-[11px]">{d.built_at}</Mono>}
                          {d.last_attempt?.status && d.last_attempt.status !== "built" && (
                            <span className="block text-[11px] text-warn">
                              last attempt: {d.last_attempt.status}{d.last_attempt.reason ? ` (${d.last_attempt.reason})` : ""}
                            </span>
                          )}
                        </td>
                        <td>
                          {d.rows ?? "—"}
                          {d.bytes != null && <span className="block text-[11px] text-ink-faint">{megabytes(d.bytes)} compressed</span>}
                          {absentText(d) && <span className="block text-[11px] text-warn">absent: {absentText(d)}</span>}
                          {unknownUnits(d).length > 0 && (
                            <span className="block text-[11px] text-warn">unit unknown (no offers): {unknownUnits(d).join(", ")}</span>
                          )}
                        </td>
                        <td className="text-xs">{yearsText(d) ?? "—"}</td>
                        <td>{d.source_url ? <ExternalLink href={d.source_url}>source</ExternalLink> : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-ink-faint">Rebuilt monthly (and on demand) by the build-open-data GitHub Action,
                which opens a pull request with the new snapshots and its coverage report. The server never builds or
                writes open data.</p>
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

      <StoragePanel />

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

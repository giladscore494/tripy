import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { CatalogItem, Scope, StartRun, Vehicle } from "../api/types";
import { CatalogPicker } from "../components/run/CatalogPicker";
import { defaultsOf, overridesOf, RunSettingsForm, settingsErrors, type SettingsValues } from "../components/run/RunSettingsForm";
import { IconSearch, IconSpark } from "../components/ui/icons";
import { Badge, Button, Dir, Disclosure, ErrorState, Field, Mono, Notice, Panel, Segmented, Skeleton } from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { useResource } from "../hooks/useResource";
import { PageHeader } from "../layouts/AppShell";
import { useSubmissionKey } from "../lib/idempotency";

const SCOPES: { id: Scope; label: string; hint: string }[] = [
  { id: "one", label: "One vehicle", hint: "A single benchmark vehicle" },
  { id: "manufacturer", label: "Manufacturer", hint: "Every benchmark vehicle of one manufacturer" },
  { id: "all", label: "Whole benchmark", hint: "Every vehicle of the benchmark sample" },
  { id: "set", label: "Live catalog", hint: "Any MILO catalog variant: one, or a filtered set of up to 50" },
];

function matches(vehicle: Vehicle, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [vehicle.label, vehicle.title, vehicle.manufacturer, vehicle.model, vehicle.record_id, String(vehicle.year ?? "")]
    .some((part) => part.toLowerCase().includes(q));
}

export function VehiclePicker({ vehicles, value, onChange }: {
  vehicles: Vehicle[]; value: string | null; onChange: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => vehicles.filter((v) => matches(v, query)), [vehicles, query]);
  return (
    <div className="space-y-2">
      <div className="relative">
        <IconSearch size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
        <input aria-label="Search vehicles" className="input pl-9" placeholder="Search by manufacturer, model, year or record id"
               value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <ul role="listbox" aria-label="Vehicles" className="max-h-80 space-y-1 overflow-y-auto rounded-card border border-line bg-white/[0.02] p-1.5">
        {filtered.slice(0, 200).map((v) => (
          <li key={v.record_id} role="option" aria-selected={v.record_id === value}>
            <button type="button" onClick={() => onChange(v.record_id)}
                    className={`flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left text-sm transition-colors ${
                      v.record_id === value ? "bg-accent/15 text-ink shadow-glow" : "text-ink-muted hover:bg-white/[0.05] hover:text-ink"}`}>
              <Dir className="min-w-0 break-words">{v.label}</Dir>
              <Mono className="shrink-0 text-[11px]">{v.record_id}</Mono>
            </button>
          </li>
        ))}
        {!filtered.length && <li className="px-3 py-6 text-center text-sm text-ink-muted">No vehicle matches “{query}”.</li>}
      </ul>
    </div>
  );
}

export function NewResearchPage() {
  const navigate = useNavigate();
  const { config, activeRuns, runs } = useWorkspace();
  const vehicles = useResource("vehicles", (signal) => api.vehicles({ signal }));
  const contract = useResource("run-settings", (signal) => api.runSettings({ signal }));
  const catalogStatus = useResource("catalog-status", (signal) => api.catalogStatus({ signal }));
  const [catalogSet, setCatalogSet] = useState<CatalogItem[]>([]);
  const [scope, setScope] = useState<Scope>("one");
  const [recordId, setRecordId] = useState<string | null>(null);
  const [manufacturer, setManufacturer] = useState<string>("");
  const [profile, setProfile] = useState<string>("");
  const [values, setValues] = useState<SettingsValues | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [existingRun, setExistingRun] = useState<string | null>(null);
  const submission = useSubmissionKey("run");

  const cfg = config.data;
  useEffect(() => {
    if (cfg && !profile) setProfile(cfg.default_profile);
  }, [cfg, profile]);
  useEffect(() => {
    if (contract.data && !values) setValues(defaultsOf(contract.data));
  }, [contract.data, values]);
  useEffect(() => {
    const list = vehicles.data;
    if (list && !manufacturer && list.manufacturers.length) setManufacturer(list.manufacturers[0]);
  }, [vehicles.data, manufacturer]);

  const list = vehicles.data?.vehicles ?? [];
  const selection: { record_id: string }[] = scope === "set" ? catalogSet
    : scope === "one" ? list.filter((v) => v.record_id === recordId)
    : scope === "manufacturer" ? list.filter((v) => v.manufacturer === manufacturer) : list;
  const ids = new Set(selection.map((v) => v.record_id));
  const busyRun = activeRuns.find((r) => r.record_ids.some((id) => ids.has(id)));
  const errors = contract.data && values ? settingsErrors(contract.data, values) : {};
  const overrides = contract.data && values ? overridesOf(contract.data, values) : {};
  const blocked = cfg ? !cfg.configured : true;
  const canSubmit = selection.length > 0 && !busyRun && !blocked && !Object.keys(errors).length && !busy;

  const submit = () => submission.guard(async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setExistingRun(null);
    const body: StartRun = { scope, profile, idempotency_key: submission.current() };
    if (scope === "one" && recordId) body.record_id = recordId;
    if (scope === "manufacturer") body.manufacturer = manufacturer;
    if (scope === "set") body.record_ids = catalogSet.map((v) => v.record_id);
    if (Object.keys(overrides).length) body.settings = overrides;
    let startedOk = false;
    try {
      const started = await api.startRun(body);
      submission.resolve();
      startedOk = true;          // navigating away: the button stays busy
      void runs.refresh();
      navigate(`/runs/${encodeURIComponent(started.run_id)}`);
    } catch (e) {
      const apiError = e instanceof ApiError ? e : new ApiError(0, "client_error", "The run could not be started.");
      // a network failure keeps the key: retrying returns the same run if the first request did reach the server
      if (apiError.status !== 0) submission.resolve();
      if (apiError.status === 409 && typeof apiError.details.existing_run_id === "string") {
        setExistingRun(apiError.details.existing_run_id);
      }
      setError(apiError);
    } finally {
      if (!startedOk) setBusy(false);
    }
  });

  return (
    <div className="space-y-6">
      <PageHeader kicker="Research" title="New research">
        Choose a target and a run profile. The run executes on the server; you can close this page at any time.
      </PageHeader>

      <Panel kicker="Step 1" title="Research target">
        <Segmented label="Scope" options={SCOPES} value={scope} onChange={setScope} />
        <div className="mt-5">
          {vehicles.loading ? <div className="space-y-2"><Skeleton className="h-10" /><Skeleton className="h-56 rounded-card" /></div>
            : vehicles.error ? <ErrorState error={vehicles.error} onRetry={vehicles.refresh} />
            : scope === "set" ? (
              <CatalogPicker status={catalogStatus.data ?? (catalogStatus.error ? {
                mode: "snapshot", browser_enabled: false, label: "snapshot (50 benchmark records)", max_set: 50,
                max_limit: 200, derived_index: { available: false, building: false, path: "", interval_days: 7 } }
                : undefined)} selected={catalogSet} onChange={setCatalogSet} max={catalogStatus.data?.max_set ?? 50} />
            )
            : scope === "one" ? <VehiclePicker vehicles={list} value={recordId} onChange={setRecordId} />
            : scope === "manufacturer" ? (
              <Field label="Manufacturer" htmlFor="manufacturer"
                     hint={`${selection.length} benchmark vehicle${selection.length === 1 ? "" : "s"} will be researched.`}>
                <select id="manufacturer" className="input" dir="auto" value={manufacturer}
                        onChange={(e) => setManufacturer(e.target.value)}>
                  {vehicles.data?.manufacturers.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </Field>
            ) : (
              <Notice tone="accent" title={`${list.length} benchmark vehicles will run`}>
                The whole benchmark sample, researched with the run&apos;s vehicle workers in parallel.
              </Notice>
            )}
        </div>
      </Panel>

      <Panel kicker="Step 2" title="Run profile and settings">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Run profile" htmlFor="profile"
                 hint="A named profile sets the experiment settings itself; Custom uses the per-run settings below.">
            <select id="profile" className="input" value={profile} onChange={(e) => setProfile(e.target.value)}>
              {cfg?.profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </Field>
          <div className="text-sm text-ink-muted sm:pt-6">
            {cfg && <>Research model <Mono className="text-ink">{cfg.research_model ?? "not set"}</Mono> · search <span className="text-ink">{cfg.search_backend}</span></>}
          </div>
        </div>
        <div className="mt-5">
          {contract.error ? <ErrorState error={contract.error} title="Per-run settings are unavailable" onRetry={contract.refresh} />
            : contract.data && values ? (
              <Disclosure title={<span className="flex items-center gap-2">Per-run settings
                {Object.keys(overrides).length > 0 && <Badge tone="accent">{Object.keys(overrides).length} changed</Badge>}</span>}>
                <RunSettingsForm contract={contract.data} values={values} onChange={setValues} profile={profile} />
              </Disclosure>
            ) : <Skeleton className="h-12 rounded-card" />}
        </div>
      </Panel>

      <Panel>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0 text-sm text-ink-muted">
            <span className="text-ink">{selection.length}</span> vehicle{selection.length === 1 ? "" : "s"}
            {cfg && <> · {cfg.profiles.find((p) => p.id === profile)?.label ?? profile} · prompt <Mono>{cfg.prompt_version}</Mono></>}
            {blocked && cfg && <p className="mt-1 text-danger">The server is not configured to start research: {cfg.blocking.join(", ")}.</p>}
            {busyRun && (
              <p className="mt-1 text-warn">A run for this target is already active —{" "}
                <Link className="underline" to={`/runs/${encodeURIComponent(busyRun.run_id)}`}>open it</Link>.</p>
            )}
            {Object.keys(errors).length > 0 && <p className="mt-1 text-danger">Fix the highlighted settings first.</p>}
          </div>
          <Button variant="primary" onClick={submit} busy={busy} disabled={!canSubmit} className="sm:min-w-48">
            <IconSpark size={16} />Start research
          </Button>
        </div>
        {error && (
          <div className="mt-4">
            {error.code === "insufficient_disk" ? (
              <Notice tone="danger" title="Not enough free space on the data volume" action={
                <Link to="/data#storage"><Button size="sm">Open Data → Storage</Button></Link>}>
                {error.message}
              </Notice>
            ) : existingRun ? (
              <Notice tone="warn" title="A conflicting run is already active" action={
                <Link to={`/runs/${encodeURIComponent(existingRun)}`}><Button size="sm">Open that run</Button></Link>}>
                {error.message}
              </Notice>
            ) : (
              <ErrorState error={error} title="The run was not started" onRetry={error.status === 0 ? submit : undefined} />
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}

// The run views the former dashboard offered beyond the core workspace: the live field table and activity log, the
// status totals and run report, a vehicle's technical sections (raw output, partial research, tool calls, model
// responses, Level 1.5 input, config & cost, API attempts, field recovery, detailed diagnostics), the Benchmark tab,
// the cross-vehicle results table and the shared document cache. Every value comes from the backend; nothing here
// computes a metric.

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/endpoints";
import type {
  IdentityAnchors,
  BriefPairs, CacheDocuments, DetailedDiagnostics, FieldRecoveryView, JsonObject, LiveView, Notice, Row, RunBenchmark,
  RunResults, VehicleState, VehicleTechnical,
} from "../../api/types";
import { useResource } from "../../hooks/useResource";
import { compactJson, formatDuration, formatValue, humanize, str } from "../../lib/format";
import {
  Badge, Button, DataTable, Dir, Disclosure, EmptyState, ErrorState, JsonBlock, KeyValue, Mono, Notice as NoticeBox,
  Segmented, SkeletonRows, Stat, Tabs,
} from "../ui/primitives";

export const LIVE_MS = 3000;

function obj(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function Section({ title, children, count }: { title: string; children: React.ReactNode; count?: number }) {
  return (
    <Disclosure title={<span className="flex items-center gap-2">{title}{count !== undefined && <Badge>{count}</Badge>}</span>}>
      <div className="space-y-3">{children}</div>
    </Disclosure>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-ink-muted">{children}</p>;
}

function TextBlock({ text, maxHeight = "20rem" }: { text: string | null | undefined; maxHeight?: string }) {
  return (
    <pre dir="auto" style={{ maxHeight }}
         className="mono overflow-auto whitespace-pre-wrap break-words rounded-xl bg-bg-deep/70 p-3 text-ink-muted">{text || "—"}</pre>
  );
}

// --- notices, brief, status, report -----------------------------------------------------------------------------------

export function NoticeList({ notices }: { notices: Notice[] | undefined }) {
  if (!notices?.length) return null;
  return (
    <div className="space-y-2">
      {notices.map((n, i) => (
        <NoticeBox key={i} tone={n.tone === "warn" ? "warn" : "info"}>
          <Dir as="p" className="whitespace-pre-wrap text-ink">{n.text}</Dir>
          {n.detail && <Mono className="mt-1 block whitespace-pre-wrap text-[11px]">{n.detail}</Mono>}
        </NoticeBox>
      ))}
    </div>
  );
}

export function BriefView({ brief }: { brief: BriefPairs }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div>
        <p className="kicker mb-2">Source acquisition</p>
        {brief.acquisition.length
          ? <KeyValue columns={3} items={brief.acquisition.map(([k, v]) => ({ label: k, value: formatValue(v) }))} />
          : <Caption>No acquisition activity yet.</Caption>}
      </div>
      {(brief.sweep || brief.sweep_skipped) && (
        <div>
          <p className="kicker mb-2">Document sweep</p>
          {brief.sweep_skipped ? <Caption>Skipped: {brief.sweep_skipped}</Caption>
            : <KeyValue columns={3} items={(brief.sweep ?? []).map(([k, v]) => ({ label: k, value: formatValue(v) }))} />}
        </div>
      )}
    </div>
  );
}

export function StatusPanel({ rows }: { rows: [string, unknown][] | undefined }) {
  if (!rows?.length) return null;
  return <KeyValue columns={4} items={rows.map(([k, v]) => ({ label: k, value: <span className="tabular-nums">{formatValue(v)}</span> }))} />;
}

const REPORT_DURATIONS: [string, string][] = [["acquisition", "Acquisition"], ["harvest", "Harvest"], ["sweep", "Sweep"],
  ["recovery", "Recovery"], ["finalization", "Finalizer"]];

/** The dashboard's "Run report" of each finished vehicle (the run_state.json report, else rebuilt by the server). */
export function RunReportView({ vehicles }: { vehicles: VehicleState[] }) {
  const reported = vehicles.filter((v) => v.report);
  if (!reported.length) return <Caption>No run report yet (it is written when the run finishes).</Caption>;
  return (
    <div className="space-y-4">
      {reported.map((v) => {
        const r = obj(v.report);
        const durations = obj(r.durations_s);
        const byStage = obj(r.model_calls_by_stage);
        const calls = Object.entries(byStage).filter(([, n]) => n).map(([k, n]) => `${k} ${String(n)}`).join(" · ");
        const rows: [string, unknown][] = [
          ["Total duration", formatDuration(r.total_duration_s as number | null)],
          ...REPORT_DURATIONS.map(([key, label]): [string, unknown] => [label, formatDuration(durations[key] as number | null)]),
          ["Search calls", r.search_calls], ["Model calls", calls || r.model_calls],
          ["Fetched documents", r.fetched_documents], ["Official documents", r.official_documents],
          ["Target-market documents", r.target_market_documents], ["Candidates", r.candidate_count],
          ["Candidate fields", r.candidate_fields], ["Evidence admitted", r.evidence_admitted],
          ["Evidence rejected", r.evidence_rejected],
          ["Resolved fields", r.applicable_fields ? `${String(r.resolved_fields)} / ${String(r.applicable_fields)}` : null],
          ["Stop reason", r.stop_reason],
          ["Recorded cost", typeof r.cost_usd === "number" ? `$${r.cost_usd.toFixed(4)}` : null],
        ];
        return (
          <div key={v.record_id}>
            {vehicles.length > 1 && <Dir as="p" className="mb-2 text-sm font-medium text-ink">{v.title}</Dir>}
            <KeyValue columns={4} items={rows.filter(([, val]) => val !== null && val !== undefined)
              .map(([k, val]) => ({ label: k, value: formatValue(val) }))} />
          </div>
        );
      })}
    </div>
  );
}

// --- live field table and activity log --------------------------------------------------------------------------------

export function LiveFieldsView({ runId, recordId, active }: { runId: string; recordId?: string; active: boolean }) {
  const live = useResource(`live:${runId}:${recordId ?? ""}`, (signal) => api.live(runId, recordId, { signal }),
                           { intervalMs: active ? LIVE_MS : null });
  const data: LiveView | undefined = live.data;
  if (live.loading) return <SkeletonRows rows={3} label="Loading live view" />;
  if (live.error && !data) return <ErrorState error={live.error} onRetry={live.refresh} />;
  if (!data) return null;
  return (
    <div className="space-y-4">
      <BriefView brief={data.brief} />
      {data.fields.length > 0 && (
        <div>
          <p className="kicker mb-2">Field progress</p>
          <div className="table-wrap max-h-[28rem]" dir="rtl">
            <table className="data-table" aria-label="Live field progress">
              <thead><tr>{data.columns.map((c) => <th key={c} className="!text-right">{c}</th>)}</tr></thead>
              <tbody>{data.fields.map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j} className="text-right text-xs text-ink-muted"><Dir>{formatValue(cell)}</Dir></td>)}</tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )}
      <div>
        <p className="kicker mb-2">Activity log (last {data.lines.length} lines)</p>
        <TextBlock text={data.lines.join("\n")} maxHeight="22rem" />
      </div>
    </div>
  );
}

// --- one vehicle's technical sections ---------------------------------------------------------------------------------

function FieldRecovery({ view }: { view: FieldRecoveryView }) {
  if (!view.available) {
    return <Caption>{view.requested_fields} requested enrichment field(s). Field detection did not run for this run (older run, or research did not finish).</Caption>;
  }
  const m = view.metrics;
  return (
    <div className="space-y-3">
      <Caption>{view.requested_fields} requested enrichment field(s). Fields with a usable candidate after primary research,
        or marked not applicable, are never retried. States come from the model&apos;s own evidence and declarations; this is not a fact check.</Caption>
      {view.error && <NoticeBox tone="danger">{String(view.error)}</NoticeBox>}
      {m && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Failed after primary" value={m.failed_after_primary} />
          <Stat label="Retried" value={m.retried} />
          <Stat label="Recovered" value={m.recovered} tone="accent" />
          <Stat label="Retry attempts" value={m.retry_attempts} />
        </div>
      )}
      <Caption>Recovery turns used: {view.turns}{view.stopped ? ` · retries stopped early: ${view.stopped}` : ""}</Caption>
      {view.not_attempted_due_to_budget?.length ? (
        <NoticeBox tone="warn">Not attempted because the recovery turn budget was exhausted: {view.not_attempted_due_to_budget.join(", ")}
          {view.cut_short_by_budget ? ` (cut short: ${formatValue(view.cut_short_by_budget)})` : ""}</NoticeBox>
      ) : null}
      {view.cluster && (
        <Caption>Clustered tail recovery · {Object.entries(view.cluster).map(([k, v]) => `${humanize(k).toLowerCase()} ${formatValue(v)}`).join(" · ")}</Caption>
      )}
      {view.triage?.length ? <DataTable rows={view.triage} label="Recovery triage" /> : null}
      {view.fields?.length ? <DataTable rows={view.fields} label="Field recovery states" /> : null}
      {view.attempts?.length ? (
        <>
          <p className="kicker">Retry attempts</p>
          <DataTable rows={view.attempts} label="Retry attempts" />
          {view.prior_excerpts_summary && (
            <Caption>prior excerpts supplied: {view.prior_excerpts_summary.items} · prior excerpt chars: {view.prior_excerpts_summary.chars.toLocaleString()} · attempts with prior excerpts: {view.prior_excerpts_summary.attempts_with_prior_excerpts}</Caption>
          )}
        </>
      ) : null}
      {view.prior_excerpts?.map((e, i) => (
        <Disclosure key={i} title={`Prior excerpts · ${e.field} · attempt ${e.attempt} · ${e.items} item(s), ${e.chars} chars`}>
          <JsonBlock value={e.excerpts} />
        </Disclosure>
      ))}
    </div>
  );
}

function DetailedDiagnosticsView({ diag }: { diag: DetailedDiagnostics }) {
  const [tab, setTab] = useState<"summary" | "turns" | "calls" | "fields" | "raw">("summary");
  const models = Object.entries(diag.configured_models).filter(([, v]) => v).map(([k, v]) => `${k} ${String(v)}`).join(", ");
  return (
    <div className="space-y-3">
      <Caption>Observational telemetry of source acquisition and the document sweep. Schema {formatValue(diag.schema)}
        {models && <> · models configured: {models}</>}</Caption>
      <Tabs label="Detailed diagnostics" value={tab} onChange={setTab} tabs={[
        { id: "summary", label: "Summary" }, { id: "turns", label: "Acquisition turns" }, { id: "calls", label: "Sweep calls" },
        { id: "fields", label: "Sweep fields" }, { id: "raw", label: "Raw" }]} />
      {tab === "summary" && <TextBlock text={diag.summary_text} />}
      {tab === "turns" && (diag.turns.length ? <DataTable rows={diag.turns} label="Acquisition turns" /> : <Caption>No primary-research turns recorded.</Caption>)}
      {tab === "calls" && (diag.sweep_calls.length ? <DataTable rows={diag.sweep_calls} label="Sweep calls" /> : <Caption>No document-sweep call was made.</Caption>)}
      {tab === "fields" && (diag.sweep_fields.length ? <DataTable rows={diag.sweep_fields} label="Sweep fields" /> : <Caption>No field entered the document sweep.</Caption>)}
      {tab === "raw" && <JsonBlock value={diag.raw} />}
    </div>
  );
}

function Human({ human }: { human: NonNullable<VehicleTechnical["human"]> }) {
  if (!human.is_object) return <Caption>The model&apos;s final answer was not a JSON object; see Raw output for the raw text.</Caption>;
  return (
    <div className="space-y-3">
      {human.variant_identity && <><p className="kicker">Variant identity (as reported by the model)</p><JsonBlock value={human.variant_identity} maxHeight="14rem" /></>}
      {human.alternatives?.length ? <><p className="kicker">Per-field alternatives</p><DataTable rows={human.alternatives} /></> : null}
      {human.provenance?.length ? (
        <><p className="kicker">Provenance (as reported by the model)</p>
          <ul className="list-disc space-y-1 pl-5 text-sm">{human.provenance.map((p) => <li key={p.label}><strong className="text-ink">{p.label}:</strong> <Dir>{p.text}</Dir></li>)}</ul></>
      ) : null}
      {human.level3 ? <><p className="kicker">Level 3 (open research)</p><JsonBlock value={human.level3} maxHeight="14rem" /></> : null}
      {human.other_keys ? <><p className="kicker">Other keys returned by the model</p><JsonBlock value={human.other_keys} maxHeight="14rem" /></> : null}
      {human.research_trace?.length ? (
        <><p className="kicker">Research trace (model&apos;s own account)</p>
          <ul className="list-disc space-y-1 pl-5 text-sm">{human.research_trace.map((s, i) => <li key={i}><Dir>{s}</Dir></li>)}</ul></>
      ) : null}
    </div>
  );
}

function IdentityAnchorsView({ anchors }: { anchors: IdentityAnchors }) {
  const fp = anchors.fingerprint;
  const od = anchors.open_data;
  return (
    <Section title="Identity anchors and open data">
      <Caption>The government identity fingerprint, the approval route and the international variant per open dataset.
        Offers are admitted only in admit mode and only for allowlisted (source, field, route) triples; shadow offers
        are recorded, never evidence.</Caption>
      <KeyValue columns={4} items={[
        { label: "Approval route", value: fp?.approval_route?.route ?? od?.route ?? "—" },
        { label: "Type code", value: <Mono>{fp?.type_code ?? "—"}</Mono> },
        { label: "Code family", value: <Mono className="break-all">{(fp?.code_family ?? []).join(", ") || "—"}</Mono> },
        { label: "Equivalent codes", value: <Mono>{(fp?.equivalent_codes ?? []).join(", ") || "—"}</Mono> },
        { label: "Open data mode", value: od?.mode ?? "—" },
        { label: "Match level", value: od?.level ?? "—" },
        { label: "International variant", value: <Dir>{od?.designation ?? "—"}</Dir> },
        { label: "Lead source", value: od?.lead_source ?? "—" },
      ]} />
      {(anchors.open_data_not_built ?? []).length > 0 && (
        <NoticeBox tone="warn" title="Open data not built">
          No local snapshot for {(anchors.open_data_not_built ?? []).join(", ")} when this run started, so those
          sources could not be matched. See the build status on the <Link className="underline" to="/data">Data page</Link>.
        </NoticeBox>
      )}
      {od?.error && <NoticeBox tone="danger">{od.error}</NoticeBox>}
      {od && od.sources.length > 0 && <><p className="kicker">International variant per source</p><DataTable rows={od.sources} /></>}
      {od && od.offers.length > 0 && <><p className="kicker">Offers per field</p><DataTable rows={od.offers} /></>}
      {anchors.spec_sheets && <Disclosure title="Importer spec-sheet discovery"><JsonBlock value={anchors.spec_sheets} /></Disclosure>}
    </Section>
  );
}

export function VehicleTechnicalView({ runId, recordId, active }: { runId: string; recordId: string; active: boolean }) {
  const tech = useResource(`tech:${runId}:${recordId}:${active}`, (signal) => api.technical(runId, recordId, { signal }));
  const t = tech.data;
  if (tech.loading) return <SkeletonRows rows={4} label="Loading technical details" />;
  if (tech.error && !t) return <ErrorState error={tech.error} onRetry={tech.refresh} />;
  if (!t) return null;
  const pr = t.partial_research;
  return (
    <div className="space-y-3">
      <BriefView brief={t.brief} />
      {!t.available && <NoticeBox tone="accent">{t.reason}</NoticeBox>}
      <NoticeList notices={t.notices} />
      {t.no_output_message && <NoticeBox tone="warn" title="Final structured result">{t.no_output_message}{t.has_research && " See Partial research for everything the research collected."}</NoticeBox>}
      {t.error ? <NoticeBox tone="danger"><Dir>{formatValue(t.error)}</Dir></NoticeBox> : null}
      {t.identity_anchors && <IdentityAnchorsView anchors={t.identity_anchors} />}
      {t.diagnostics && <Section title="Detailed diagnostics"><DetailedDiagnosticsView diag={t.diagnostics} /></Section>}
      {t.human && <Section title="Human view (model output beyond the fields)"><Human human={t.human} /></Section>}
      {t.raw && (
        <Section title="Raw output">
          <Caption>parse: {formatValue(t.raw.parse_note)}</Caption>
          {t.raw.output !== null && t.raw.output !== undefined && <JsonBlock value={t.raw.output} />}
          <p className="kicker">Raw final text</p>
          <TextBlock text={t.raw.raw_final_text} />
        </Section>
      )}
      {pr && (
        <Section title="Partial research">
          <Caption>Research material as collected. These are not final structured fields and no value was derived from them by code.</Caption>
          <Caption>{pr.evidence_count ? `Stored evidence: ${pr.evidence_count} (see the Evidence tab).` : "No evidence stored."}</Caption>
          {pr.candidate_facts.length > 0 && <><p className="kicker">Candidate facts (grouped by field, as stored)</p><DataTable rows={pr.candidate_facts} /></>}
          {pr.fields_with_multiple_stored_values.length > 0 && <Caption>Fields with more than one stored value: {pr.fields_with_multiple_stored_values.join(", ")}</Caption>}
          {pr.model_noted_conflicts.length > 0 && (
            <><p className="kicker">Conflicts mentioned during research</p>
              <ul className="list-disc space-y-1 pl-5 text-sm">{pr.model_noted_conflicts.map((c, i) => <li key={i}><em>{formatValue(c.source)}</em>: <Dir>{c.text}</Dir></li>)}</ul></>
          )}
          {pr.response_excerpts.length > 0 && <><p className="kicker">Model response excerpts</p><DataTable rows={pr.response_excerpts} /></>}
          {pr.last_model_content && <><p className="kicker">Last successful model response</p><TextBlock text={pr.last_model_content} maxHeight="12rem" /></>}
          {pr.urls.length > 0 && (
            <><p className="kicker">URLs ({pr.urls.length})</p>
              <ul className="space-y-0.5 text-xs">{pr.urls.map((u) => <li key={u}><Mono className="break-all">{u}</Mono></li>)}</ul></>
          )}
          {pr.documents.length > 0 && <><p className="kicker">Documents ({pr.documents.length})</p><DataTable rows={pr.documents} /></>}
          {pr.target_status.no_evidence.length > 0 && <Caption><strong className="text-ink">Targets with no stored evidence</strong> (Level 2): {pr.target_status.no_evidence.join(", ")}</Caption>}
          {pr.target_status.unresolved.length > 0 && (
            <><p className="text-xs font-semibold text-ink">Targets still unresolved (Level 2, current state; evidence may exist):</p>
              <ul className="list-disc pl-5 text-xs text-ink-muted">{pr.target_status.unresolved.map((l) => <li key={l}>{l}</li>)}</ul></>
          )}
          {pr.target_status.level3.length > 0 && <Caption><strong className="text-ink">Level 3 topics not researched / without evidence:</strong> {pr.target_status.level3.join(", ")}</Caption>}
          {(pr.error || pr.api_errors.length > 0) && (
            <><p className="kicker">Errors</p>{pr.error ? <NoticeBox tone="danger"><Dir>{formatValue(pr.error)}</Dir></NoticeBox> : null}
              {pr.api_errors.length > 0 && <DataTable rows={pr.api_errors} />}</>
          )}
          {pr.bundle && <Disclosure title="Partial research bundle (JSON)"><JsonBlock value={pr.bundle} /></Disclosure>}
        </Section>
      )}
      {t.consistency_checks && t.consistency_checks.length > 0 && (
        <Section title="Cross-field consistency checks" count={t.consistency_checks.length}>
          <Caption>QA signals only; they never change a value.</Caption>
          <DataTable rows={t.consistency_checks} />
        </Section>
      )}
      {t.tool_calls && <Section title="Tool calls" count={t.tool_calls.length}>{t.tool_calls.length ? <DataTable rows={t.tool_calls} /> : <Caption>No tool calls.</Caption>}</Section>}
      {t.model_responses && (
        <Section title="Model responses" count={t.model_responses.length}>
          {t.model_responses.map((r, i) => (
            <Disclosure key={i} title={`seq ${formatValue(r.seq)} · ${formatValue(r.phase)} · ${formatValue(r.tool_calls)} tool call(s) · ${formatValue(r.prompt_tokens)}/${formatValue(r.completion_tokens)} tokens · ${formatValue(r.latency_ms)} ms`}>
              {r.content && <><p className="kicker mb-1">Content</p><TextBlock text={r.content} /></>}
              {r.reasoning_content && <><p className="kicker my-1">Reasoning content</p><TextBlock text={r.reasoning_content} /></>}
              {!r.content && !r.reasoning_content && <Caption>No content.</Caption>}
            </Disclosure>
          ))}
        </Section>
      )}
      {t.level15_input && <Section title="Level 1.5 input"><JsonBlock value={t.level15_input} /></Section>}
      {t.config && (
        <Section title="Run config & cost">
          {t.config.api_error ? <><NoticeBox tone="danger" title="Raw GLM API error" /><JsonBlock value={t.config.api_error} /></> : null}
          <KeyValue columns={4} items={[
            { label: "Research model", value: <Mono className="text-ink">{t.config.research_model ?? "—"}</Mono> },
            { label: "Finalizer model", value: <Mono className="text-ink">{t.config.finalizer_model ?? "—"}</Mono> },
            { label: "Stop reason", value: t.config.stop_reason ?? "—" },
            { label: "Prompt version", value: <Mono>{t.config.prompt_version ?? "—"}</Mono> },
          ]} />
          {t.config.finalization && <Disclosure title="Finalization"><JsonBlock value={t.config.finalization} /></Disclosure>}
          <Disclosure title="Effective configuration"><JsonBlock value={t.config.effective_config} /></Disclosure>
          <Caption>{t.config.usage_known ? "Recorded API cost from returned usage." : "Some attempts returned no usage; the recorded cost is incomplete."}</Caption>
          <Disclosure title="Usage, search calls and cost"><JsonBlock value={t.config.usage} /></Disclosure>
        </Section>
      )}
      {t.api_attempts && (
        <Section title="API attempts" count={t.api_attempts.api_errors.length}>
          <JsonBlock value={t.api_attempts.api_stats} maxHeight="14rem" />
          {t.api_attempts.api_errors.length ? <DataTable rows={t.api_attempts.api_errors} /> : <Caption>No failed API attempts.</Caption>}
        </Section>
      )}
      {t.field_recovery && <Section title="Field recovery"><FieldRecovery view={t.field_recovery} /></Section>}
      {t.candidates && (
        <Section title="Layered candidate metrics">
          {Object.keys(t.candidates.candidate_summary).length > 0 && (
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Stat label="Documents harvested" value={formatValue(t.candidates.candidate_summary.documents_harvested ?? 0)} />
              <Stat label="Candidates" value={formatValue(t.candidates.candidate_summary.candidate_count_total ?? 0)} />
              <Stat label="Fields with candidates" value={formatValue(t.candidates.candidate_summary.candidate_fields_total ?? 0)} />
              <Stat label="Resolved by the sweep" value={formatValue(t.candidates.candidate_summary.document_sweep_fields_resolved ?? 0)} />
            </div>
          )}
          <Disclosure title="Raw candidates / layered metrics (JSON)"><JsonBlock value={t.candidates} /></Disclosure>
        </Section>
      )}
    </div>
  );
}

// --- the Benchmark tab ------------------------------------------------------------------------------------------------

export function BenchmarkView({ data }: { data: RunBenchmark }) {
  if (!data.available) return <NoticeBox tone="accent">{data.reason}</NoticeBox>;
  const agg = obj(data.aggregate);
  if (!data.vehicles) return <EmptyState title="No runs in this batch yet" />;
  const cost = agg.cost_usd_total;
  return (
    <div className="space-y-4">
      <Caption>Observation metrics only: what the model and tools did. Not a correctness score; human fact-checking happens outside TRIPY. Runs without a final JSON are counted too.</Caption>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Vehicles" value={formatValue(agg.vehicles)} />
        <Stat label="Without final JSON" value={formatValue(agg.runs_without_final_output)} />
        <Stat label="Mean coverage" value={`${formatValue(agg.coverage_pct_mean)}%`} tone="accent" />
        <Stat label="Tool calls" value={formatValue(agg.tool_calls_total)} />
        <Stat label="Doc cache hit rate" value={`${formatValue(agg.document_cache_hit_rate_pct)}%`} />
        <Stat label="Known tokens" value={formatValue(agg.total_tokens_total)} />
        <Stat label="Timeouts" value={formatValue(agg.timeout_count_total)} />
        <Stat label="Recorded cost (USD)" value={typeof cost === "number" ? cost.toFixed(4) : "n/a"} />
      </div>
      {data.cost_note && <Caption>{data.cost_note}</Caption>}
      <DataTable rows={data.rows} columns={data.columns} label="Per-vehicle observation metrics" />
      <Disclosure title="Batch aggregate (raw)"><JsonBlock value={data.aggregate} /></Disclosure>
      <Disclosure title="Tool usage by name"><DataTable rows={data.tool_usage} label="Tool usage by name" /></Disclosure>
    </div>
  );
}

// --- every vehicle's results in one table -----------------------------------------------------------------------------

export function AllVehiclesResults({ results }: { results: RunResults }) {
  const [layout, setLayout] = useState<"long" | "wide">("long");
  const long = useMemo<Row[]>(() => results.vehicles.flatMap((v) => v.fields.map((f) => ({
    vehicle: v.title, field: f.field, value: formatValue(f.value, f.unit), market: f.market, provenance: f.provenance,
    state: f.state, evidence: f.evidence_ids.join(", ") }))), [results]);
  const wide = useMemo<{ columns: string[]; rows: Row[] }>(() => {
    const fields = [...new Set(long.map((r) => String(r.field)))];
    return { columns: ["vehicle", ...fields], rows: results.vehicles.map((v) => ({
      vehicle: v.title, ...Object.fromEntries(v.fields.map((f) => [f.field, formatValue(f.value, f.unit)])) })) };
  }, [long, results]);
  const without = results.vehicles.filter((v) => !v.has_output);
  return (
    <div className="space-y-3">
      {results.output_source_caption && <Caption>{results.output_source_caption}</Caption>}
      <Segmented label="Layout" value={layout} onChange={setLayout}
                 options={[{ id: "long", label: "Long" }, { id: "wide", label: "Wide (vehicle × field)" }]} />
      {long.length ? (layout === "long" ? <DataTable rows={long} label="Results of every vehicle" limit={300} />
        : <DataTable rows={wide.rows} columns={wide.columns} label="Results, vehicle × field" />)
        : <Caption>No vehicle produced structured fields.</Caption>}
      {without.map((v) => <NoticeBox key={v.record_id} tone="warn" title={v.title}>{v.no_output_message ?? "No final structured result."}</NoticeBox>)}
    </div>
  );
}

// --- the shared document cache ----------------------------------------------------------------------------------------

export function CacheDocumentsView({ onOpen }: { onOpen: (docId: string, title?: string | null) => void }) {
  const [offset, setOffset] = useState(0);
  const page = useResource(`cache-docs:${offset}`, (signal) => api.cacheDocuments(offset, 100, { signal }));
  const data: CacheDocuments | undefined = page.data;
  if (page.loading) return <SkeletonRows rows={4} label="Loading the document cache" />;
  if (page.error && !data) return <ErrorState error={page.error} onRetry={page.refresh} />;
  if (!data) return null;
  return (
    <div className="space-y-3">
      <Caption>Shared cache: {data.total} documents · cache counters of this server process {compactJson(data.stats, 400)}</Caption>
      {data.documents.length ? (
        <div className="table-wrap max-h-[36rem]">
          <table className="data-table" aria-label="Document cache">
            <thead><tr><th>Document</th><th>Kind</th><th>URL</th><th>Status</th><th>Type</th><th>Text chars</th><th>Fetched</th></tr></thead>
            <tbody>{data.documents.map((d) => {
              const id = String(d.document_id ?? "");
              return (
                <tr key={id}>
                  <td><button type="button" className="mono text-[11px] text-accent-soft hover:underline" onClick={() => onOpen(id, str(d.title))}>{id}</button></td>
                  <td className="text-xs">{formatValue(d.kind)}</td>
                  <td className="max-w-[22rem] text-xs"><Mono className="break-all">{formatValue(d.final_url ?? d.url)}</Mono></td>
                  <td className="text-xs">{formatValue(d.status)}</td>
                  <td className="text-xs">{formatValue(d.content_type)}</td>
                  <td className="text-xs tabular-nums">{formatValue(d.text_chars)}</td>
                  <td className="text-xs">{formatValue(d.fetched_at)}</td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      ) : <EmptyState title="The document cache is empty" />}
      <div className="flex gap-2">
        <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</Button>
        <Button size="sm" disabled={data.next_offset === null} onClick={() => setOffset(data.next_offset ?? offset)}>Next</Button>
        <span className="self-center text-xs text-ink-faint">{data.offset + 1}–{data.offset + data.returned} of {data.total}</span>
      </div>
    </div>
  );
}

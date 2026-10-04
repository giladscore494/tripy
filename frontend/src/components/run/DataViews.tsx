import { useMemo, useState } from "react";

import type { CandidateField, ResultField, RunCandidates, RunEvidence, VehicleResult } from "../../api/types";
import { compactJson, formatCost, formatDuration, formatValue, humanize, str } from "../../lib/format";
import { fieldStateTone } from "../../lib/status";
import { IconSearch } from "../ui/icons";
import { Badge, cx, Dir, Disclosure, EmptyState, ExternalLink, JsonBlock, KeyValue, Mono, Stat } from "../ui/primitives";

function FieldState({ state }: { state: string | null | undefined }) {
  if (!state) return <span className="text-ink-faint">—</span>;
  return <Badge tone={fieldStateTone(state)}>{humanize(state)}</Badge>;
}

function TextValue({ value, unit }: { value: unknown; unit?: string | null }) {
  return <Dir className="text-ink">{formatValue(value, unit)}</Dir>;
}

function SearchBox({ value, onChange, label }: { value: string; onChange: (v: string) => void; label: string }) {
  return (
    <div className="relative min-w-0 flex-1">
      <IconSearch size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
      <input aria-label={label} className="input !py-1.5 pl-9 text-sm" placeholder={label} value={value}
             onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function listOfText(items: unknown[]): string[] {
  return items.map((item) => typeof item === "string" ? item
    : item && typeof item === "object" ? str((item as Record<string, unknown>).text) ?? str((item as Record<string, unknown>).note)
      ?? compactJson(item) : String(item));
}

// --- results ----------------------------------------------------------------------------------------------------------

export function ResultsView({ result }: { result: VehicleResult }) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const states = useMemo(() => [...new Set(result.fields.map((f) => f.state).filter(Boolean))] as string[], [result]);
  const fields = result.fields.filter((f) => (!state || f.state === state)
    && (!query || f.field.toLowerCase().includes(query.toLowerCase()) || formatValue(f.value).toLowerCase().includes(query.toLowerCase())));
  const ok = result.fields.filter((f) => f.state === "ok").length;
  const finalization = result.finalization ?? {};
  const admission = result.evidence_admission ?? {};
  const summary = typeof result.summary === "string" ? result.summary : result.summary ? compactJson(result.summary, 2000) : null;

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Fields ok" value={`${ok} / ${result.fields.length}`} tone="accent" />
        <Stat label="Duration" value={formatDuration(result.duration_s)} />
        <Stat label="Cost" value={formatCost(result.cost)} hint="reported from returned usage" />
        <Stat label="Target market" value={result.target_market ?? "—"} />
      </div>
      {summary && (
        <div className="rounded-card border border-line bg-white/[0.03] p-4">
          <p className="kicker mb-2">Summary</p>
          <Dir as="p" className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{summary}</Dir>
        </div>
      )}
      <KeyValue columns={4} items={[
        { label: "Engine status", value: result.engine_status ?? "—" },
        { label: "Result source", value: result.result_source ?? "—" },
        { label: "Research model", value: <Mono className="text-ink">{result.research_model ?? "—"}</Mono> },
        { label: "Finalizer model", value: <Mono className="text-ink">{result.finalizer_model ?? "—"}</Mono> },
        { label: "Finalization", value: str(finalization.status) ?? str(finalization.mode) ?? (result.synthesized ? "synthesized" : "—") },
        { label: "Recovered", value: result.recovered ? "yes" : "no" },
        { label: "Stop reason", value: result.stop_reason ?? "—" },
        { label: "Prompt version", value: <Mono>{result.prompt_version ?? "—"}</Mono> },
      ]} />

      <div className="flex flex-col gap-2 sm:flex-row">
        <SearchBox value={query} onChange={setQuery} label="Filter fields or values" />
        <select aria-label="Field state" className="input !py-1.5 text-sm sm:!w-56" value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">All states</option>
          {states.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
        </select>
      </div>
      {fields.length ? (
        <div className="table-wrap max-h-[36rem]">
          <table className="data-table">
            <thead><tr><th>Field</th><th>Value</th><th>State</th><th>Market</th><th>Provenance</th><th>Evidence</th><th>Notes</th></tr></thead>
            <tbody>
              {fields.map((f: ResultField) => (
                <tr key={f.field}>
                  <td><Mono className="text-ink">{f.field}</Mono></td>
                  <td className="min-w-[10rem]"><TextValue value={f.value} unit={f.unit} /></td>
                  <td><FieldState state={f.state} /></td>
                  <td>{f.market ?? "—"}</td>
                  <td className="text-ink-muted">{f.provenance ?? "—"}</td>
                  <td><Mono className="text-[11px]">{f.evidence_ids.map(String).join(", ") || "—"}</Mono></td>
                  <td className="min-w-[12rem] text-xs text-ink-muted">{f.notes ? <Dir>{typeof f.notes === "string" ? f.notes : compactJson(f.notes)}</Dir> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EmptyState title={result.fields.length ? "No field matches the filter" : "The result has no fields"} />}

      {result.conflicts.length > 0 && (
        <div className="rounded-card border border-warn/30 bg-warn/[0.05] p-4">
          <p className="kicker mb-2">Conflicts</p>
          <ul className="space-y-1.5 text-sm">{listOfText(result.conflicts).map((c, i) => <li key={i}><Dir>{c}</Dir></li>)}</ul>
        </div>
      )}
      {result.additional_findings.length > 0 && (
        <div className="rounded-card border border-line bg-white/[0.03] p-4">
          <p className="kicker mb-2">Additional findings</p>
          <ul className="list-disc space-y-1.5 pl-5 text-sm">{listOfText(result.additional_findings).map((c, i) => <li key={i}><Dir>{c}</Dir></li>)}</ul>
        </div>
      )}
      {Object.keys(admission).length > 0 && (
        <Disclosure title="Evidence admission">
          <KeyValue columns={3} items={Object.entries(admission).map(([k, v]) => ({
            label: humanize(k), value: typeof v === "object" && v ? <span className="mono">{compactJson(v, 400)}</span> : formatValue(v) }))} />
        </Disclosure>
      )}
      <Disclosure title="Raw result JSON (technical)"><JsonBlock value={result} /></Disclosure>
    </div>
  );
}

// --- candidates -------------------------------------------------------------------------------------------------------

type CandidateMode = "live" | "rejected" | "all";

function candidateValue(c: Record<string, unknown>) {
  return formatValue(c.value ?? c.raw_value, str(c.unit) ?? str(c.raw_unit));
}

export function CandidatesView({ data }: { data: RunCandidates }) {
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("");
  const [origin, setOrigin] = useState("");
  const [mode, setMode] = useState<CandidateMode>("all");
  const groups = useMemo(() => [...new Set(data.fields.map((f) => f.group).filter(Boolean))] as string[], [data]);
  const origins = useMemo(() => [...new Set(data.fields.flatMap((f) => [...f.candidates, ...f.rejected])
    .map((c) => str(c.origin)).filter(Boolean))] as string[], [data]);

  const rows = data.fields.filter((f) => (!group || f.group === group)
    && (!query || f.field.toLowerCase().includes(query.toLowerCase())));
  const pick = (f: CandidateField) => {
    const live = mode === "rejected" ? [] : f.candidates;
    const rejected = mode === "live" ? [] : f.rejected;
    const keep = (c: Record<string, unknown>) => !origin || c.origin === origin;
    return { live: live.filter(keep), rejected: rejected.filter(keep) };
  };
  const summary = data.summary;
  const visible = rows.map((f) => ({ f, ...pick(f) })).filter(({ live, rejected }) => live.length || rejected.length || (!origin && mode === "all"));

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Candidates" value={formatValue(summary.candidate_count)} />
        <Stat label="Fields with candidates" value={`${formatValue(summary.fields_with_candidates)} / ${formatValue(summary.applicable_fields)}`} />
        <Stat label="Grounded candidates" value={formatValue(summary.grounded_candidate_count)} />
        <Stat label="Documents" value={formatValue(summary.documents)} />
      </div>
      <div className="flex flex-col gap-2 lg:flex-row">
        <SearchBox value={query} onChange={setQuery} label="Filter fields" />
        <select aria-label="Group" className="input !py-1.5 text-sm lg:!w-48" value={group} onChange={(e) => setGroup(e.target.value)}>
          <option value="">All groups</option>{groups.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
        <select aria-label="Origin" className="input !py-1.5 text-sm lg:!w-44" value={origin} onChange={(e) => setOrigin(e.target.value)}>
          <option value="">All origins</option>{origins.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        <select aria-label="Candidate status" className="input !py-1.5 text-sm lg:!w-44" value={mode} onChange={(e) => setMode(e.target.value as CandidateMode)}>
          <option value="all">Live and rejected</option><option value="live">Live candidates</option><option value="rejected">Rejected candidates</option>
        </select>
      </div>
      {visible.length ? (
        <ul className="space-y-2">
          {visible.map(({ f, live, rejected }) => (
            <li key={f.field} className="rounded-card border border-line bg-white/[0.02]">
              <details>
                <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-4 py-3">
                  <Mono className="text-ink">{f.field}</Mono>
                  {f.group && <span className="text-xs text-ink-faint">{f.group}</span>}
                  <FieldState state={f.state} />
                  <span className="ml-auto text-xs text-ink-muted">{live.length} live · {rejected.length} rejected</span>
                </summary>
                <div className="space-y-3 border-t border-line px-4 py-3">
                  {live.length > 0 && <CandidateTable rows={live} />}
                  {rejected.length > 0 && <CandidateTable rows={rejected} rejected />}
                  {!live.length && !rejected.length && <p className="text-sm text-ink-muted">No candidate was harvested for this field.</p>}
                </div>
              </details>
            </li>
          ))}
        </ul>
      ) : <EmptyState title={data.fields.length ? "No candidate matches the filters" : "No candidates yet"}>
          {data.fields.length ? undefined : "Candidates appear once the vehicle declared its fields and harvested documents."}
        </EmptyState>}
    </div>
  );
}

function CandidateTable({ rows, rejected }: { rows: Record<string, unknown>[]; rejected?: boolean }) {
  return (
    <div>
      <p className={cx("kicker mb-1.5", rejected && "text-danger/80")}>{rejected ? "Rejected" : "Live candidates"}</p>
      <div className="table-wrap">
        <table className="data-table">
          <thead><tr><th>Value</th><th>Origin</th><th>Method</th><th>Source</th><th>Quote</th><th>{rejected ? "Rejection" : "Hints"}</th></tr></thead>
          <tbody>
            {rows.map((c, i) => (
              <tr key={i}>
                <td className="min-w-[7rem]"><Dir className="text-ink">{candidateValue(c)}</Dir></td>
                <td>{str(c.origin) ?? "—"}</td>
                <td className="text-ink-muted">{str(c.extraction_method) ?? str(c.matched_alias) ?? "—"}</td>
                <td className="max-w-[14rem]">
                  {str(c.source_url) ? <ExternalLink href={str(c.source_url)} /> : <Mono className="text-[11px]">{str(c.document_id) ?? "—"}</Mono>}
                </td>
                <td className="min-w-[14rem] max-w-[28rem]"><Dir className="text-xs text-ink-muted">{str(c.quote) ?? "—"}</Dir></td>
                <td className="min-w-[10rem] text-xs text-ink-muted">
                  {rejected ? <Dir>{[str(c.reason), str(c.rejection_reason), str(c.note)].filter(Boolean).join(" · ") || "—"}</Dir>
                    : [str(c.market_hint) && `market ${c.market_hint}`, typeof c.parser_confidence === "number" && `confidence ${c.parser_confidence}`,
                       str(c.variant_hint), typeof c.occurrences === "number" && `×${c.occurrences}`].filter(Boolean).join(" · ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- evidence ---------------------------------------------------------------------------------------------------------

export function EvidenceView({ data, onOpenDocument }: { data: RunEvidence; onOpenDocument?: (docId: string) => void }) {
  const [query, setQuery] = useState("");
  const [show, setShow] = useState<"admitted" | "rejected">("admitted");
  const rows = (show === "admitted" ? data.admitted : data.rejected).filter((e) => !query
    || String(e.field ?? "").toLowerCase().includes(query.toLowerCase())
    || String(e.source_url ?? "").toLowerCase().includes(query.toLowerCase()));
  const s = data.summary;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Admitted" value={formatValue(s.admitted)} tone="accent" />
        <Stat label="Rejected by admission" value={formatValue(s.rejected)} tone={Number(s.rejected) ? "warn" : undefined} />
        <Stat label="Exact variant" value={formatValue((s.admitted_by_variant_match as Record<string, unknown> | undefined)?.exact ?? 0)} />
        <Stat label="Official sources" value={formatValue(Object.entries((s.admitted_by_source_authority as Record<string, number> | undefined) ?? {})
          .filter(([k]) => k.startsWith("official") || k === "government").reduce((a, [, v]) => a + (Number(v) || 0), 0))} />
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <SearchBox value={query} onChange={setQuery} label="Filter by field or source" />
        <select aria-label="Evidence status" className="input !py-1.5 text-sm sm:!w-52" value={show} onChange={(e) => setShow(e.target.value as "admitted" | "rejected")}>
          <option value="admitted">Admitted ({data.admitted.length})</option>
          <option value="rejected">Rejected ({data.rejected.length})</option>
        </select>
      </div>
      {rows.length ? (
        <div className="table-wrap max-h-[40rem]">
          <table className="data-table">
            <thead><tr><th>Field</th><th>Value</th><th>Source</th><th>Authority</th><th>Market</th><th>Binding</th><th>Variant</th><th>{show === "admitted" ? "Admission" : "Rejection"}</th><th>Quote</th></tr></thead>
            <tbody>
              {rows.map((e, i) => {
                const request = (e.request && typeof e.request === "object" ? e.request : {}) as Record<string, unknown>;
                const doc = str(e.document_id) ?? str(request.document_id);
                return (
                  <tr key={str(e.evidence_id) ?? i}>
                    <td><Mono className="text-ink">{String(e.field ?? request.field ?? "—")}</Mono></td>
                    <td className="min-w-[7rem]"><Dir className="text-ink">{formatValue(e.value ?? request.value, str(e.unit) ?? str(request.unit))}</Dir></td>
                    <td className="max-w-[14rem]">
                      <ExternalLink href={str(e.source_url) ?? str(request.source_url)}>{str(e.source_domain) ?? undefined}</ExternalLink>
                      {doc && (onOpenDocument
                        ? <button type="button" className="mono block text-[11px] text-ink-faint hover:text-accent-soft" onClick={() => onOpenDocument(doc)}>{doc}</button>
                        : <Mono className="block text-[11px]">{doc}</Mono>)}
                    </td>
                    <td>{str(e.source_authority) ? <Badge>{humanize(str(e.source_authority))}</Badge> : "—"}</td>
                    <td>{str(e.market) ?? "—"}</td>
                    <td className="text-xs">
                      <span className="text-ink">{str(e.binding_level) ?? "—"}</span>
                      {str(e.binding_basis) && <span className="block text-ink-faint">{str(e.binding_basis)}</span>}
                    </td>
                    <td>{str(e.variant_match) ? <Badge tone={e.variant_match === "exact" ? "ok" : e.variant_match === "different" ? "danger" : "warn"}>{str(e.variant_match)}</Badge> : "—"}</td>
                    <td className="text-xs text-ink-muted">
                      {show === "admitted" ? str(e.admission_status) ?? "accepted"
                        : <Dir>{[str(e.reason), str(e.rejection_reason), str(e.note)].filter(Boolean).join(" · ") || "—"}</Dir>}
                    </td>
                    <td className="min-w-[14rem] max-w-[26rem]"><Dir className="text-xs text-ink-muted">{str(e.quote) ?? str(request.quote) ?? "—"}</Dir></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <EmptyState title={show === "admitted" ? "No admitted evidence" : "No rejected evidence"} />}
    </div>
  );
}

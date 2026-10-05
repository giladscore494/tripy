import { useMemo, useState } from "react";

import { api } from "../../api/endpoints";
import type { BindingReplay, DocumentRow, JsonObject, RunDiagnostics, RunDocuments } from "../../api/types";
import { useResource } from "../../hooks/useResource";
import { compactJson, formatBytes, formatDateTime, formatValue, humanize, str } from "../../lib/format";
import { fieldStateTone } from "../../lib/status";
import { IconDoc, IconSearch } from "../ui/icons";
import {
  Badge, Button, Dir, Disclosure, Drawer, EmptyState, ErrorState, ExternalLink, JsonBlock, KeyValue, Mono, Skeleton,
  SkeletonRows, Stat, Tabs,
} from "../ui/primitives";

function obj(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

// --- documents --------------------------------------------------------------------------------------------------------

export function DocumentsView({ data, onOpen }: { data: RunDocuments; onOpen: (doc: DocumentRow) => void }) {
  const [query, setQuery] = useState("");
  const [used, setUsed] = useState(false);
  const rows = data.documents.filter((d) => (!used || d.used_for_fields.length)
    && (!query || [d.title ?? "", d.url ?? "", d.doc_id, d.market ?? "", ...d.used_for_fields]
      .some((p) => p.toLowerCase().includes(query.toLowerCase()))));
  if (!data.documents.length) {
    return <EmptyState icon={<IconDoc />} title="No documents yet">Documents appear once the vehicle fetched sources.</EmptyState>;
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative min-w-0 flex-1">
          <IconSearch size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
          <input aria-label="Filter documents" className="input !py-1.5 pl-9 text-sm" placeholder="Filter by title, URL, market or field"
                 value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <input type="checkbox" checked={used} onChange={(e) => setUsed(e.target.checked)} className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
          Supplied evidence only
        </label>
      </div>
      <ul className="grid gap-3 lg:grid-cols-2">
        {rows.map((doc) => (
          <li key={doc.doc_id} className="flex flex-col gap-3 rounded-card border border-line bg-white/[0.025] p-4 transition-colors hover:border-line-strong">
            <div className="min-w-0">
              <Dir as="p" className="truncate text-sm font-semibold text-ink" title={doc.title ?? undefined}>{doc.title ?? doc.url ?? doc.doc_id}</Dir>
              <ExternalLink href={doc.url} className="text-xs" />
            </div>
            <div className="flex flex-wrap gap-1.5">
              {doc.source_authority && <Badge tone={String(doc.source_authority).startsWith("official") || doc.source_authority === "government" ? "accent" : "neutral"}>{humanize(doc.source_authority)}</Badge>}
              {doc.market && <Badge tone="violet" title={doc.market_basis ?? undefined}>{doc.market}</Badge>}
              {doc.doc_type && <Badge>{doc.doc_type}</Badge>}
              {doc.status !== null && doc.status !== undefined && <Badge tone={Number(doc.status) >= 400 ? "danger" : "ok"}>HTTP {String(doc.status)}</Badge>}
            </div>
            <KeyValue columns={3} items={[
              { label: "Content type", value: <Mono>{doc.content_type ?? "—"}</Mono> },
              { label: "Size / text", value: `${formatBytes(doc.size_bytes)} · ${formatValue(doc.text_chars)} chars` },
              { label: "Fetched", value: formatDateTime(str(doc.fetched_at)) },
            ]} />
            <div className="text-xs text-ink-muted">
              <span className="text-ink-faint">Evidence for: </span>
              {doc.used_for_fields.length ? doc.used_for_fields.map((f) => <Mono key={f} className="mr-2 text-accent-soft">{f}</Mono>) : "no admitted evidence"}
            </div>
            <div className="mt-auto flex items-center justify-between gap-2">
              <Mono className="text-[11px]">{doc.doc_id}</Mono>
              <Button size="sm" onClick={() => onOpen(doc)}><IconDoc size={14} />Open</Button>
            </div>
          </li>
        ))}
      </ul>
      {!rows.length && <EmptyState title="No document matches the filter" />}
      {data.paging.remaining > 0 && <p className="text-xs text-ink-faint">{data.paging.remaining} more documents not shown.</p>}
    </div>
  );
}

const TEXT_PAGE = 20000;

export function DocumentDrawer({ docId, title, runId, recordId, onClose }: {
  docId: string | null; title?: string | null; runId: string; recordId?: string; onClose: () => void;
}) {
  const [tab, setTab] = useState<"text" | "structure">("text");
  const text = useResource(docId ? `doctext:${docId}` : null,
    (signal) => api.documentText(docId!, 0, TEXT_PAGE, { signal }));
  const structure = useResource(docId && tab === "structure" ? `docstruct:${docId}:${runId}:${recordId}` : null,
    (signal) => api.documentStructure(docId!, runId, recordId, 0, 50, { signal }));
  // further text pages of THIS document (character offsets from the backend's next_offset)
  const [extra, setExtra] = useState<{ doc: string; text: string; next: number | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const own = extra && extra.doc === docId ? extra : null;
  const nextOffset = own ? own.next : text.data?.next_offset ?? null;
  const full = (text.data?.text ?? "") + (own?.text ?? "");
  const more = async () => {
    if (nextOffset === null || !docId) return;
    setLoadingMore(true);
    try {
      const page = await api.documentText(docId, nextOffset, TEXT_PAGE);
      setExtra({ doc: docId, text: (own?.text ?? "") + page.text, next: page.next_offset });
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <Drawer open={docId !== null} onClose={onClose} wide title={
      <div className="min-w-0">
        <Dir as="p" className="truncate">{title ?? text.data?.title ?? docId ?? "Document"}</Dir>
        <Mono className="text-[11px] font-normal">{docId}</Mono>
      </div>}>
      <div className="space-y-4">
        {text.data?.url && <ExternalLink href={text.data.url} className="text-sm" />}
        <Tabs label="Document view" value={tab} onChange={setTab}
              tabs={[{ id: "text", label: "Text" }, { id: "structure", label: "Structure" }]} />
        {tab === "text" ? (
          text.loading ? <SkeletonRows rows={8} label="Loading document text" />
            : text.error ? <ErrorState error={text.error} onRetry={text.refresh} />
            : text.data && (
              <div className="space-y-3">
                <p className="text-xs text-ink-faint">{full.length.toLocaleString()} of {text.data.total_chars.toLocaleString()} characters</p>
                <Dir as="div" className="whitespace-pre-wrap break-words rounded-xl bg-bg-deep/60 p-4 text-sm leading-relaxed text-ink">{full}</Dir>
                {nextOffset !== null && <Button size="sm" onClick={more} busy={loadingMore}>Load more text</Button>}
              </div>
            )
        ) : structure.loading ? <SkeletonRows rows={6} label="Loading document structure" />
          : structure.error ? <ErrorState error={structure.error} onRetry={structure.refresh} />
          : structure.data && <StructureView data={structure.data} />}
      </div>
    </Drawer>
  );
}

function StructureView({ data }: { data: { tables: JsonObject[]; dom_groups: JsonObject[]; variant_map: JsonObject;
                                           paging: { total: number; remaining: number } } }) {
  const map = obj(data.variant_map);
  const computed = obj(map.computed_for);
  const variant = obj(map.map);
  const regions = Array.isArray(variant.regions) ? (variant.regions as JsonObject[]) : [];
  return (
    <div className="space-y-5">
      <section>
        <p className="kicker mb-2">Tables ({data.paging.total})</p>
        {data.tables.length ? data.tables.map((table, ti) => {
          const rows = Array.isArray(table.rows) ? (table.rows as unknown[][]) : [];
          return (
            <div key={ti} className="mb-4 space-y-1.5">
              <p className="text-xs text-ink-muted">
                Table {String(table.index ?? ti)}{str(table.caption) && <> · <Dir>{str(table.caption)}</Dir></>}
                {str(table.column_identity) && <> · column identity <Dir>{str(table.column_identity)}</Dir></>}
              </p>
              <div className="table-wrap">
                <table className="data-table">
                  <tbody>
                    {rows.slice(0, 80).map((row, ri) => (
                      <tr key={ri}>{(Array.isArray(row) ? row : [row]).map((cell, ci) => (
                        <td key={ci} className={ri === 0 ? "font-semibold text-ink" : "text-ink-muted"}><Dir>{formatValue(cell)}</Dir></td>
                      ))}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        }) : <p className="text-sm text-ink-muted">No tables were extracted.</p>}
      </section>
      <section>
        <p className="kicker mb-2">DOM pairs ({data.dom_groups.length} groups)</p>
        {data.dom_groups.length ? data.dom_groups.slice(0, 30).map((group, gi) => {
          const pairs = Array.isArray(group.pairs) ? (group.pairs as JsonObject[]) : [];
          return (
            <Disclosure key={gi} className="mb-2" title={<Dir>{`${str(group.header) ?? "No header"} · ${pairs.length} pairs`}</Dir>}>
              <ul className="space-y-1 text-sm">
                {pairs.slice(0, 100).map((p, pi) => (
                  <li key={pi} className="flex justify-between gap-4 border-b border-white/[0.05] py-1">
                    <Dir className="text-ink-muted">{String(p.label ?? "")}</Dir><Dir className="text-ink">{formatValue(p.value)}</Dir>
                  </li>
                ))}
              </ul>
            </Disclosure>
          );
        }) : <p className="text-sm text-ink-muted">No label / value pairs (not an HTML document, or none found).</p>}
      </section>
      <section>
        <p className="kicker mb-2">Variant map</p>
        {Object.keys(computed).length > 0 && (
          <p className="mb-2 text-xs text-ink-muted">Computed in memory for <Mono>{String(computed.run_id)}</Mono> / <Mono>{String(computed.record_id)}</Mono></p>
        )}
        {regions.length > 0 && (
          <div className="table-wrap mb-3">
            <table className="data-table">
              <thead><tr><th>Region</th><th>Kind</th><th>Text</th><th>Status</th></tr></thead>
              <tbody>{regions.slice(0, 60).map((r, i) => (
                <tr key={i}><td><Mono>{String(r.id ?? "")}</Mono></td><td>{String(r.kind ?? "—")}</td>
                  <td><Dir className="text-xs">{str(r.text) ?? "—"}</Dir></td><td>{str(r.status) ?? "—"}</td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <Disclosure title="Variant map JSON"><JsonBlock value={data.variant_map} /></Disclosure>
      </section>
    </div>
  );
}

// --- binding replay ---------------------------------------------------------------------------------------------------

function joined(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join(", ") : str(value) ?? "";
}

/** The column the item was read from: its header and (effective) column identity, else the candidate match count. */
function columnLabel(item: JsonObject): string {
  const parts = [...new Set([str(item.column_header), str(item.effective_column_identity) ?? str(item.column_identity)]
    .filter(Boolean))];
  if (parts.length) return parts.join(" · ");
  const count = Number(item.candidate_match_count);
  return count ? `${count} candidates${item.candidate_match_ambiguous ? " (ambiguous)" : ""}` : "—";
}

/** "table:0:col:2 target (AWD 486 כ"ס) catalog 2 → 1" (the Document Variant Map region that proves the level). */
function regionLabel(region: JsonObject): string {
  if (!Object.keys(region).length) return "";
  const before = Array.isArray(region.candidates_before) ? region.candidates_before : [];
  const after = Array.isArray(region.candidates_after) ? region.candidates_after : [];
  const parts = [str(region.region_id), str(region.status)];
  if (str(region.identity_text)) parts.push(`(${String(region.identity_text).slice(0, 60)})`);
  if (before.length || after.length) parts.push(`catalog ${before.length} → ${after.length}`);
  if (Array.isArray(region.blocked_by) && region.blocked_by.length) parts.push(`blocked: ${region.blocked_by.join(",")}`);
  return parts.filter(Boolean).join(" ");
}

export function BindingReplayView({ data, onOpenDocument }: { data: BindingReplay; onOpenDocument?: (docId: string) => void }) {
  const [field, setField] = useState("");
  const [kind, setKind] = useState("");
  const [changed, setChanged] = useState(false);
  const vehicle = obj(data.summary.vehicle);
  const fields = obj(data.summary.fields);
  const names = useMemo(() => Object.keys(fields).sort(), [fields]);
  const items = data.items.filter((i) => (!field || i.field === field) && (!kind || i.kind === kind)
    && (!changed || (i.kind === "evidence" && i.binding_level_recorded !== i.binding_level_now)));

  return (
    <div className="space-y-5">
      <p className="text-sm text-ink-muted">
        Today&apos;s server-side binding over this run&apos;s admitted evidence and open-field candidates — no model, no
        network, nothing in the run changes. <em>Would be ok</em> is the field evaluator on an in-memory copy.
      </p>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Fields ok (recorded → now)" value={`${formatValue(vehicle.fields_ok_recorded)} → ${formatValue(vehicle.fields_ok_now)}`} tone="accent" />
        <Stat label="Exact evidence (recorded → now)" value={`${formatValue(vehicle.evidence_exact_recorded)} → ${formatValue(vehicle.evidence_exact_now)}`} />
        <Stat label="Rejected now" value={formatValue(vehicle.rejected_now)} tone={Number(vehicle.rejected_now) ? "warn" : undefined} />
        <Stat label="Missing documents" value={formatValue(vehicle.missing_documents)} />
      </div>
      <p className="text-xs text-ink-muted">
        Rose by the year rules: {formatValue(vehicle.evidence_rose_by_year_rules)}
        {Object.keys(obj(vehicle.gap_counts)).length > 0 && <> · Blocking dimensions now: {Object.entries(obj(vehicle.gap_counts))
          .sort((a, b) => Number(b[1]) - Number(a[1])).map(([k, v]) => `${k} ${String(v)}`).join(", ")}</>}
      </p>
      {names.length > 0 && (
        <div className="table-wrap max-h-80">
          <table className="data-table">
            <thead><tr><th>Field</th><th>Recorded</th><th>Now</th><th>Best match now</th><th>Best level now</th><th>Raised by</th><th>Evidence / candidates</th></tr></thead>
            <tbody>
              {names.map((name) => {
                const f = obj(fields[name]);
                return (
                  <tr key={name} className="cursor-pointer" onClick={() => setField(field === name ? "" : name)}>
                    <td><Mono className={field === name ? "text-accent-soft" : "text-ink"}>{name}</Mono></td>
                    <td><Badge tone={fieldStateTone(str(f.state_recorded))}>{humanize(str(f.state_recorded))}</Badge></td>
                    <td><Badge tone={fieldStateTone(str(f.would_be_state))}>{humanize(str(f.would_be_state))}</Badge></td>
                    <td>{str(f.best_variant_match_now) ?? "—"}</td>
                    <td>{str(f.best_binding_level_now) ?? "—"}</td>
                    <td className="text-xs text-ink-muted">{joined(f.raised_by) || "—"}</td>
                    <td className="tabular-nums">{formatValue(f.evidence)} / {formatValue(f.candidates)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <select aria-label="Replay field" className="input !py-1.5 text-sm sm:!w-64" value={field} onChange={(e) => setField(e.target.value)}>
          <option value="">All fields</option>{names.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <select aria-label="Replay item kind" className="input !py-1.5 text-sm sm:!w-48" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Evidence and candidates</option><option value="evidence">Evidence</option><option value="candidate">Candidates</option>
        </select>
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <input type="checkbox" checked={changed} onChange={(e) => setChanged(e.target.checked)} className="h-4 w-4 accent-[rgb(var(--c-accent))]" />
          Level changed only
        </label>
      </div>
      {items.length ? (
        <div className="table-wrap max-h-[40rem]">
          <table className="data-table">
            <thead><tr><th>Field</th><th>Value</th><th>Kind</th><th>Column header / identity</th><th>Recorded</th><th>Now</th><th>Would be ok</th><th>Basis now</th><th>Variant map region · year</th><th>Flags / vetoes</th><th>Source</th></tr></thead>
            <tbody>
              {items.map((item, i) => {
                const now = item.missing_document ? "missing document" : str(item.replay_error) ?? str(item.binding_level_now) ?? "—";
                const rejected = obj(item.rejected_now);
                const flags = [joined(item.binding_veto_now), joined(item.binding_gap_now),
                               str(rejected.reason) && `rejected: ${rejected.reason}`].filter(Boolean).join(" · ");
                const doc = str(item.document_id);
                return (
                  <tr key={i}>
                    <td><Mono className="text-ink">{String(item.field ?? "")}</Mono></td>
                    <td className="min-w-[7rem]"><Dir className="text-ink">{formatValue(item.value)}</Dir></td>
                    <td><Badge>{String(item.kind ?? "")}</Badge></td>
                    <td className="text-xs text-ink-muted"><Dir>{columnLabel(item)}</Dir></td>
                    <td className="text-xs">{item.kind === "evidence" ? str(item.binding_level_recorded) ?? "—" : "candidate"}
                      {str(item.variant_match_recorded) && <span className="block text-ink-faint">{str(item.variant_match_recorded)}</span>}</td>
                    <td className="text-xs text-ink">{now}
                      {str(item.variant_match_now) && <span className="block text-ink-faint">{str(item.variant_match_now)}</span>}</td>
                    <td>{item.would_be_ok ? <Badge tone="ok">yes</Badge> : <Badge>no</Badge>}</td>
                    <td className="text-xs text-ink-muted">{[str(item.binding_basis_now), joined(item.binding_rules_now)].filter(Boolean).join(" · ") || "—"}</td>
                    <td className="text-xs text-ink-muted"><Dir>{[regionLabel(obj(item.variant_map_region_now)), str(obj(item.year_context).status)].filter(Boolean).join(" · ") || "—"}</Dir></td>
                    <td className="min-w-[9rem] text-xs text-warn">{flags || <span className="text-ink-faint">—</span>}</td>
                    <td className="max-w-[14rem] text-xs">
                      <ExternalLink href={str(item.source_url)} />
                      {doc && (onOpenDocument
                        ? <button type="button" className="mono block text-[11px] text-ink-faint hover:text-accent-soft" onClick={() => onOpenDocument(doc)}>{doc}</button>
                        : <Mono className="block text-[11px]">{doc}</Mono>)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <EmptyState title="No replayed item matches the filters" />}
      {data.paging.remaining > 0 && <p className="text-xs text-ink-faint">{data.paging.remaining} more items not shown.</p>}
      <Disclosure title="Replay summary JSON (technical)"><JsonBlock value={data.summary} /></Disclosure>
    </div>
  );
}

// --- run diagnostics --------------------------------------------------------------------------------------------------

const DIAG_KEYS: [string, string][] = [
  ["run_status", "Run status"], ["cfg_acquisition_mode", "Acquisition"], ["acq_turns", "Acquisition turns"],
  ["acq_search_calls", "Searches"], ["acq_useful_documents", "Useful documents"],
  ["acq_official_documents", "Official documents"], ["acq_candidates", "Candidates"],
  ["acq_final_scoped_coverage_pct", "Scoped coverage %"],
];

export function RunDiagnosticsView({ data }: { data: RunDiagnostics }) {
  if (!data.vehicles) {
    return <EmptyState title="No vehicle diagnostics yet">Diagnostics are built from each vehicle&apos;s events once it has some.</EmptyState>;
  }
  const extra = data.per_vehicle.length ? Object.keys(data.per_vehicle[0]).filter((k) => !DIAG_KEYS.some(([key]) => key === k)
    && !["run_id", "record_id", "vehicle"].includes(k)) : [];
  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-muted">
        The benchmark export&apos;s own diagnostics for this run (the server computes every metric; nothing is recalculated here).
      </p>
      <div className="table-wrap">
        <table className="data-table">
          <thead><tr><th>Vehicle</th>{DIAG_KEYS.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead>
          <tbody>
            {data.per_vehicle.map((row, i) => (
              <tr key={i}>
                <td><Dir className="text-ink">{String(row.vehicle || row.record_id)}</Dir><Mono className="block text-[11px]">{String(row.record_id)}</Mono></td>
                {DIAG_KEYS.map(([key]) => <td key={key} className="tabular-nums">{formatValue(row[key])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.per_vehicle.map((row, i) => (
        <Disclosure key={i} title={`All diagnostics · ${String(row.record_id)}`}>
          <div className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {extra.map((k) => (
              <div key={k} className="flex justify-between gap-3 border-b border-white/[0.05] py-1">
                <span className="mono truncate text-ink-faint" title={k}>{k}</span>
                <span className="truncate text-right text-ink" title={compactJson(row[k], 500)}>{formatValue(row[k])}</span>
              </div>
            ))}
          </div>
        </Disclosure>
      ))}
      <Disclosure title="Aggregate (benchmark.json content)"><JsonBlock value={data.aggregate} /></Disclosure>
    </div>
  );
}

export function TabSkeleton() {
  return (
    <div className="space-y-4" role="status" aria-label="Loading">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-20 rounded-card" />)}</div>
      <Skeleton className="h-9 rounded-xl" />
      <Skeleton className="h-64 rounded-card" />
    </div>
  );
}

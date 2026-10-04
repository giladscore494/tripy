import { useMemo, useState } from "react";

import type { RunEvent } from "../../api/types";
import type { EventStream } from "../../hooks/useEventStream";
import { compactJson, domainOf, formatTime, formatValue, humanize, str, truncate } from "../../lib/format";
import type { Tone } from "../../lib/status";
import { Badge, Button, cx, Dir, EmptyState, ErrorState, Mono, SkeletonRows } from "../ui/primitives";

// runstate.model.ENGINE_PHASE_TO_STAGE: an event's `phase` shown as the pipeline stage it belongs to
const PHASE_STAGE: Record<string, string> = {
  research: "Source acquisition", deterministic_harvest: "Deterministic harvest", document_sweep: "Document sweep",
  field_detection: "Tail recovery", field_recovery: "Tail recovery", finalization: "Finalization",
  finalization_repair: "Finalization", recovery_finalization: "Finalization",
};

export type EventCategory = "all" | "tools" | "model" | "evidence" | "fields" | "stages" | "problems";

export const CATEGORIES: { id: EventCategory; label: string }[] = [
  { id: "all", label: "All" }, { id: "stages", label: "Stages" }, { id: "tools", label: "Search & fetch" },
  { id: "model", label: "Model" }, { id: "evidence", label: "Evidence & candidates" }, { id: "fields", label: "Fields" },
  { id: "problems", label: "Problems" },
];

/** The category of an event, from its structured `kind` (never from parsing log text). */
export function categoryOf(kind: string): Exclude<EventCategory, "all"> {
  if (/(_failed|^error$|_error$|interrupted|_rejected$|blocked|exhausted|_invalid$|inconsistent|truncated)/.test(kind)) {
    return "problems";
  }
  if (kind.startsWith("tool_") || kind === "duplicate_work" || kind === "guessed_url" || kind === "site_map"
      || kind === "gov_registry" || kind === "il_version_pages") return "tools";
  if (kind.startsWith("model_") || kind.endsWith("_retry") || kind.startsWith("narration") || kind.startsWith("thinking")) {
    return "model";
  }
  if (kind === "evidence" || kind.startsWith("candidate") || kind.startsWith("grounded_candidates")
      || kind.startsWith("document_") || kind === "fact_reuse") return "evidence";
  if (kind.startsWith("field_") || kind.startsWith("cluster_")) return "fields";
  return "stages";
}

const CATEGORY_TONE: Record<Exclude<EventCategory, "all">, Tone> = {
  stages: "violet", tools: "info", model: "accent", evidence: "ok", fields: "neutral", problems: "danger",
};

function obj(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function parsedArgs(raw: unknown): Record<string, unknown> {
  if (obj(raw)) return obj(raw)!;
  if (typeof raw === "string") {
    try {
      return obj(JSON.parse(raw)) ?? {};
    } catch {
      return {};
    }
  }
  return {};
}

export interface EventSummary {
  field: string | null;
  source: string | null;
  url: string | null;
  text: string;
}

/** A short description built from the event's structured fields. */
export function describe(event: RunEvent): EventSummary {
  const kind = String(event.kind ?? "");
  const evidence = obj(event.evidence);
  const result = obj(event.result);
  const args = parsedArgs(event.arguments);
  const field = str(event.field) ?? str(evidence?.field) ?? null;
  const url = str(event.url) ?? str(evidence?.source_url) ?? str(result?.url) ?? str(result?.final_url) ?? str(args.url) ?? null;
  const doc = str(event.document_id) ?? str(evidence?.document_id) ?? str(result?.document_id) ?? null;
  const source = url ? domainOf(url) || url : doc;
  let text = "";
  switch (kind) {
    case "tool_call":
      text = `${event.name ?? "tool"}(${truncate(str(args.query) ?? str(args.url) ?? compactJson(args, 120), 120)})`;
      break;
    case "tool_result":
      text = `${event.name ?? "tool"} → ${str(result?.title) ?? str(result?.error) ?? (url ? "fetched" : "result")}`;
      break;
    case "evidence":
      text = `${field ?? "field"} = ${formatValue(evidence?.value, str(evidence?.unit))}`;
      break;
    case "candidates_harvested":
      text = `${Array.isArray(event.candidates) ? event.candidates.length : 0} candidate(s) harvested`;
      break;
    case "model_response": {
      const usage = obj(event.usage);
      text = [str(event.model), str(event.finish_reason) && `finish ${event.finish_reason}`,
              usage && typeof usage.total_tokens === "number" && `${usage.total_tokens} tokens`,
              typeof event.latency_ms === "number" && `${Math.round(event.latency_ms as number)} ms`]
        .filter(Boolean).join(" · ");
      break;
    }
    case "field_status":
      text = `${field ?? "field"}: ${event.status ?? event.state ?? ""}`;
      break;
    case "research_stopped":
      text = `stopped: ${event.reason ?? "—"}`;
      break;
    case "run_finished":
      text = `status ${event.status ?? "—"}`;
      break;
    default: {
      const message = str(event.error) ?? str(event.message) ?? str(event.reason) ?? str(event.note) ?? str(event.status);
      if (message) text = message;
      else {
        const scalars = Object.entries(event).filter(([k, v]) => !["line", "seq", "ts", "kind", "phase"].includes(k)
          && (typeof v === "number" || typeof v === "string" || typeof v === "boolean")).slice(0, 4);
        text = scalars.map(([k, v]) => `${humanize(k).toLowerCase()} ${formatValue(v)}`).join(" · ");
      }
    }
  }
  return { field, source, url, text: truncate(text, 300) };
}

const VISIBLE_STEP = 300;

export function EventTimeline({ stream, active }: { stream: EventStream; active: boolean }) {
  const [category, setCategory] = useState<EventCategory>("all");
  const [kind, setKind] = useState("");
  const [visible, setVisible] = useState(VISIBLE_STEP);

  const counts = useMemo(() => {
    const out: Record<string, number> = { all: stream.events.length };
    for (const e of stream.events) {
      const c = categoryOf(String(e.kind ?? ""));
      out[c] = (out[c] ?? 0) + 1;
    }
    return out;
  }, [stream.events]);
  const kinds = useMemo(() => [...new Set(stream.events.map((e) => String(e.kind ?? "")))].sort(), [stream.events]);
  const filtered = useMemo(() => stream.events.filter((e) => {
    const k = String(e.kind ?? "");
    return (category === "all" || categoryOf(k) === category) && (!kind || k === kind);
  }), [stream.events, category, kind]);
  // newest first; only the newest `visible` rows are rendered (very long runs stay responsive)
  const rows = useMemo(() => filtered.slice(-visible).reverse(), [filtered, visible]);

  if (!stream.events.length) {
    if (stream.error) return <ErrorState error={stream.error} title="Events could not be loaded" />;
    if (!stream.caughtUp) return <SkeletonRows rows={6} label="Loading events" />;
    return <EmptyState title={active ? "Waiting for the first event" : "This vehicle recorded no events"}>
      {active ? "The vehicle is queued or starting; events appear here as the engine writes them." : undefined}
    </EmptyState>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div role="group" aria-label="Event type" className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 [scrollbar-width:none]">
          {CATEGORIES.map((c) => (
            <button key={c.id} type="button" aria-pressed={category === c.id} onClick={() => setCategory(c.id)}
                    className={cx("shrink-0 rounded-lg px-2.5 py-1 text-xs transition-colors",
                                  category === c.id ? "bg-accent/15 text-accent-soft" : "text-ink-muted hover:bg-white/[0.06]")}>
              {c.label} <span className="text-ink-faint">{counts[c.id] ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <select aria-label="Event kind" className="input !w-auto !py-1.5 text-xs" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All kinds</option>
            {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          {active && <Badge tone="accent" pulse>Live</Badge>}
        </div>
      </div>
      {stream.error && <ErrorState error={stream.error} title="Event polling is retrying" />}
      <ol aria-label="Event timeline" className="relative space-y-1.5 border-l border-line pl-4">
        {rows.map((event) => {
          const k = String(event.kind ?? "event");
          const summary = describe(event);
          const stage = event.phase ? PHASE_STAGE[event.phase] ?? humanize(event.phase) : null;
          const cat = categoryOf(k);
          return (
            <li key={event.line} className="relative animate-fade-in rounded-xl bg-white/[0.02] px-3 py-2 hover:bg-white/[0.04]">
              <span className={cx("absolute -left-[21px] top-3.5 h-2 w-2 rounded-full ring-4 ring-bg",
                                  cat === "problems" ? "bg-danger" : cat === "evidence" ? "bg-ok" : cat === "model" ? "bg-accent"
                                    : cat === "tools" ? "bg-info" : "bg-violet")} />
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px]">
                <Mono className="tabular-nums text-ink-faint">{formatTime(str(event.ts))}</Mono>
                <Badge tone={CATEGORY_TONE[cat]} className="!py-0">{k}</Badge>
                {stage && <span className="text-ink-faint">{stage}</span>}
                {summary.field && <Mono className="text-accent-soft">{summary.field}</Mono>}
                {summary.source && <span className="truncate text-ink-faint" dir="ltr" title={summary.url ?? undefined}>{summary.source}</span>}
              </div>
              {summary.text && <Dir as="p" className="mt-0.5 break-words text-[13px] text-ink">{summary.text}</Dir>}
            </li>
          );
        })}
      </ol>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-faint">
        <span>
          {filtered.length.toLocaleString()} event{filtered.length === 1 ? "" : "s"} · cursor line {stream.cursor}
          {stream.dropped > 0 && ` · ${stream.dropped} oldest not kept in memory`}
        </span>
        {filtered.length > visible && (
          <Button size="sm" variant="ghost" onClick={() => setVisible(visible + VISIBLE_STEP)}>Show older events</Button>
        )}
      </div>
    </div>
  );
}

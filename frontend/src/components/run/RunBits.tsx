import { Link } from "react-router-dom";

import type { RunSummary } from "../../api/types";
import { formatDateTime, formatDuration, formatRelative } from "../../lib/format";
import { statusTone } from "../../lib/status";
import { Badge, Mono } from "../ui/primitives";

export function StatusBadge({ status, label, active }: { status: string; label?: string | null; active?: boolean }) {
  return <Badge tone={statusTone(status)} pulse={active}>{label || status}</Badge>;
}

export function profileLabel(profile: string | null | undefined, profiles?: { id: string; label: string }[]): string {
  if (!profile) return "—";
  return profiles?.find((p) => p.id === profile)?.label ?? profile;
}

/** One run in a list: label, status, profile, stage, timing, vehicle count. Cards on every width (no clipped table). */
export function RunRow({ run, profiles, now }: { run: RunSummary; profiles?: { id: string; label: string }[];
                                                 now?: number }) {
  return (
    <li>
      <Link to={`/runs/${encodeURIComponent(run.run_id)}`}
            className="group grid gap-2 rounded-card border border-line bg-white/[0.025] px-4 py-3 transition-all duration-150 hover:border-line-strong hover:bg-glass-hover sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span dir="auto" className="truncate text-sm font-medium text-ink [unicode-bidi:isolate]">{run.label}</span>
            <StatusBadge status={run.status} label={run.status_label} active={run.active} />
            {run.cancel_requested && run.active && <Badge tone="warn">Stopping</Badge>}
            {run.legacy && <Badge>Legacy</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-muted">
            <Mono className="text-[11px]">{run.run_id}</Mono>
            <span>{profileLabel(run.profile, profiles)}</span>
            {run.stage && <span>stage {run.stage}</span>}
            <span>{run.record_ids.length} vehicle{run.record_ids.length === 1 ? "" : "s"}</span>
            {run.resolved_fields_text && <span className="tabular-nums">{run.resolved_fields_text} fields</span>}
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs text-ink-muted sm:justify-end sm:text-right">
          <span title={formatDateTime(run.created_at ?? run.started_at)}>
            {formatRelative(run.created_at ?? run.started_at, now)}
          </span>
          <span className="tabular-nums text-ink">{formatDuration(run.elapsed_s)}</span>
        </div>
      </Link>
    </li>
  );
}

import { useEffect, useMemo, useRef } from "react";

import { api } from "../api/endpoints";
import type { RunDetail, RunProgress } from "../api/types";
import { useInterval } from "./useInterval";
import { useResource, type Resource } from "./useResource";

export const PROGRESS_MS = 2000;
export const DETAIL_ACTIVE_MS = 12000;

export interface RunMonitor {
  detail: Resource<RunDetail>;
  progress: Resource<RunProgress>;
  /** a worker executes the run or its durable status is still active */
  active: boolean;
}

function signature(progress: RunProgress | undefined): string {
  if (!progress) return "";
  return [progress.status, progress.active, ...progress.vehicles.map((v) => `${v.record_id}:${v.status}`)].join("|");
}

/**
 * The run page's single polling controller (the polling architecture PR #45 created):
 *   progress   every ~2 s while the run is active, never once it is terminal
 *   detail     at a lower frequency while active, and immediately after a meaningful status change
 * Events are tailed separately by useEventStream with the backend's line cursor.
 */
export function useRunMonitor(runId: string): RunMonitor {
  const detail = useResource<RunDetail>(`run:${runId}`, (signal) => api.run(runId, { signal }));
  const live = Boolean(detail.data && (detail.data.active || detail.data.executing));
  const progress = useResource<RunProgress>(`progress:${runId}`, (signal) => api.progress(runId, { signal }), {
    intervalMs: PROGRESS_MS, enabled: live,
  });
  // while progress is polled it is the freshest signal; once the detail says terminal, polling stops
  const active = live && progress.data ? progress.data.active || progress.data.executing : live;

  // a lower-frequency detail refresh while active (failure cards, notes, report appear with status changes)
  useInterval(() => void detail.refresh(), active ? DETAIL_ACTIVE_MS : null);

  const last = useRef<string>("");
  const sig = signature(progress.data);
  useEffect(() => {
    if (!sig) return;
    if (last.current && last.current !== sig) void detail.refresh();
    last.current = sig;
  }, [sig, detail.refresh]);         // detail.refresh is stable (useResource's useCallback)

  return useMemo(() => ({ detail, progress, active }), [detail, progress, active]);
}

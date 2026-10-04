import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { EventsPage, RunEvent } from "../api/types";

export const EVENT_POLL_MS = 1750;
export const EVENT_PAGE = 200;
const MAX_KEPT = 5000;

export interface EventStream {
  events: RunEvent[];
  cursor: number;
  caughtUp: boolean;
  error: ApiError | null;
  /** events dropped from memory (the oldest) because the run has very many */
  dropped: number;
}

type Fetch = (after: number) => Promise<EventsPage>;

/**
 * Tail one vehicle's events.jsonl with the backend's line cursor:
 *   cursor starts at 0 → request after=cursor → append → cursor = next_cursor
 *   more == true  → request the next page immediately
 *   caught up     → wait EVENT_POLL_MS (active run) or stop (terminal run)
 * The cursor is the backend's next_cursor, never the array length; history is never reloaded; a line already seen
 * is never appended twice.
 */
export function useEventStream(runId: string | null, recordId: string | undefined, active: boolean,
                               fetchPage?: Fetch): EventStream {
  const [stream, setStream] = useState<EventStream>({ events: [], cursor: 0, caughtUp: false, error: null,
                                                      dropped: 0 });
  const activeRef = useRef(active);
  activeRef.current = active;
  const wake = useRef<(() => void) | null>(null);

  // a run that becomes active again (finalization retry) resumes tailing
  useEffect(() => {
    if (active) wake.current?.();
  }, [active]);

  useEffect(() => {
    setStream({ events: [], cursor: 0, caughtUp: false, error: null, dropped: 0 });
    if (!runId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();
    const fetcher: Fetch = fetchPage
      ?? ((after) => api.events(runId, after, recordId, EVENT_PAGE, { signal: controller.signal }));
    let cursor = 0;
    let lastLine = 0;
    let failures = 0;

    const sleep = (ms: number) => new Promise<void>((resolve) => {
      wake.current = () => {
        window.clearTimeout(timer);
        resolve();
      };
      timer = window.setTimeout(resolve, ms);
    });

    const run = async () => {
      while (!cancelled) {
        let page: EventsPage;
        try {
          page = await fetcher(cursor);
          failures = 0;
        } catch (error) {
          if (cancelled || (error as { name?: string })?.name === "AbortError") return;
          failures += 1;
          const apiError = error instanceof ApiError ? error : new ApiError(0, "client_error", "Events failed.");
          setStream((s) => ({ ...s, error: apiError }));
          if (apiError.status === 401 || apiError.status === 404) return;
          await sleep(Math.min(15000, EVENT_POLL_MS * 2 ** Math.min(failures, 3)));
          continue;
        }
        if (cancelled) return;
        const fresh = page.events.filter((e) => typeof e.line === "number" && e.line > lastLine);
        if (fresh.length) lastLine = fresh[fresh.length - 1].line;
        cursor = Math.max(cursor, page.next_cursor);
        setStream((s) => {
          const merged = fresh.length ? s.events.concat(fresh) : s.events;
          const overflow = Math.max(0, merged.length - MAX_KEPT);
          return { events: overflow ? merged.slice(overflow) : merged, cursor, caughtUp: !page.more,
                   error: null, dropped: s.dropped + overflow };
        });
        if (page.more) continue;                          // already-available lines: fetch them now
        if (!activeRef.current) {
          // terminal and caught up: stop polling until the run becomes active again (finalization retry)
          await new Promise<void>((resolve) => { wake.current = resolve; });
          continue;
        }
        await sleep(EVENT_POLL_MS);
      }
    };
    void run();
    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timer);
      wake.current?.();
      wake.current = null;
    };
    // fetchPage is a test seam; runId / recordId define the stream
  }, [runId, recordId]);

  return stream;
}

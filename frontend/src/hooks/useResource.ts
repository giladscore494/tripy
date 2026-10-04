import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";

export interface Resource<T> {
  data: T | undefined;
  error: ApiError | null;
  /** true only until the first response for the current key (refreshes keep the previous data: no flicker) */
  loading: boolean;
  refresh: () => Promise<void>;
}

export interface ResourceOptions {
  /** poll interval in ms; null/0 = fetch once (and on refresh / key change) */
  intervalMs?: number | null;
  enabled?: boolean;
}

/**
 * The single polling primitive of the workspace: one in-flight request per resource, responses for a stale key are
 * dropped, the previous data stays visible during a refresh, and polling pauses while the tab is hidden. Pages and
 * the run monitor decide the intervals; components never start their own timers.
 */
export function useResource<T>(key: string | null, fetcher: (signal: AbortSignal) => Promise<T>,
                               { intervalMs = null, enabled = true }: ResourceOptions = {}): Resource<T> {
  const [state, setState] = useState<{ key: string | null; data: T | undefined; error: ApiError | null }>(
    { key: null, data: undefined, error: null });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const keyRef = useRef(key);
  keyRef.current = key;
  const inflight = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    const requested = keyRef.current;
    if (requested === null) return;
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    try {
      const data = await fetcherRef.current(controller.signal);
      if (keyRef.current === requested && !controller.signal.aborted) {
        setState({ key: requested, data, error: null });
      }
    } catch (error) {
      if (controller.signal.aborted || (error as { name?: string })?.name === "AbortError") return;
      if (keyRef.current !== requested) return;
      const apiError = error instanceof ApiError ? error
        : new ApiError(0, "client_error", "Something went wrong while loading this view.");
      setState((previous) => ({ key: requested, data: previous.key === requested ? previous.data : undefined,
                                error: apiError }));
    } finally {
      if (inflight.current === controller) inflight.current = null;
    }
  }, []);

  useEffect(() => {
    if (!enabled || key === null) return undefined;
    void load();
    return () => inflight.current?.abort();
  }, [key, enabled, load]);

  useEffect(() => {
    if (!enabled || key === null || !intervalMs) return undefined;
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (inflight.current) return;           // never stack requests on a slow server
      void load();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [key, enabled, intervalMs, load]);

  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && enabled && !current,
    refresh: load,
  };
}

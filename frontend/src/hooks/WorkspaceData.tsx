import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "../api/endpoints";
import type { ConfigStatus, RunList } from "../api/types";
import { useResource, type Resource } from "./useResource";

export const RUNS_ACTIVE_MS = 4000;
export const RUNS_IDLE_MS = 20000;
export const HEALTH_MS = 30000;
export const CONFIG_MS = 60000;

interface WorkspaceData {
  config: Resource<ConfigStatus>;
  runs: Resource<RunList>;
  health: Resource<{ status: string }>;
  activeRuns: RunList["runs"];
}

const Context = createContext<WorkspaceData | null>(null);

/** Shell-wide state polled ONCE for every page: backend health, configuration status and the run history (fast
 *  while any run is active, slow otherwise). Pages read it; they do not poll the same endpoints again. */
export function WorkspaceDataProvider({ children }: { children: ReactNode }) {
  const config = useResource("config", (signal) => api.configStatus({ signal }), { intervalMs: CONFIG_MS });
  const health = useResource("health", (signal) => api.health({ signal }), { intervalMs: HEALTH_MS });
  // polled fast while any run is active (the interval follows the latest response), slowly otherwise
  const [anyActive, setAnyActive] = useState(false);
  const runs = useResource("runs", (signal) => api.runs(undefined, { signal }),
                           { intervalMs: anyActive ? RUNS_ACTIVE_MS : RUNS_IDLE_MS });
  const activeRuns = useMemo(() => (runs.data?.runs ?? []).filter((r) => r.active), [runs.data]);
  useEffect(() => setAnyActive(activeRuns.length > 0), [activeRuns.length]);
  const value = useMemo(() => ({ config, runs, health, activeRuns }), [config, runs, health, activeRuns]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useWorkspace(): WorkspaceData {
  const value = useContext(Context);
  if (!value) throw new Error("useWorkspace outside WorkspaceDataProvider");
  return value;
}

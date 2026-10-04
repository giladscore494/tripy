// Visual tone of the backend's status vocabularies (runstate.model statuses, pipeline stage states, field states,
// series statuses). Labels always come from the backend when it provides one.

export type Tone = "accent" | "ok" | "warn" | "danger" | "info" | "neutral" | "violet";

// runstate.model.ACTIVE_STATUSES / TERMINAL_STATUSES
export const ACTIVE_STATUSES = ["QUEUED", "STARTING", "RESEARCHING", "HARVESTING", "SWEEPING", "RECOVERING",
  "FINALIZING"];
export const TERMINAL_STATUSES = ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"];

export function isActiveStatus(status: string | null | undefined): boolean {
  return ACTIVE_STATUSES.includes(String(status ?? "").toUpperCase());
}

export function statusTone(status: string | null | undefined): Tone {
  const s = String(status ?? "").toUpperCase();
  if (s === "COMPLETED") return "ok";
  if (s === "FAILED") return "danger";
  if (s === "CANCELLED" || s === "NOT_STARTED") return "neutral";
  if (s === "INTERRUPTED") return "warn";
  if (s === "PLANNED") return "neutral";
  if (s === "RUNNING" || ACTIVE_STATUSES.includes(s)) return "accent";
  return "neutral";
}

// runstate.pipeline stage states
export function stageTone(state: string | null | undefined): Tone {
  switch (state) {
    case "running": return "accent";
    case "done": return "ok";
    case "failed": return "danger";
    case "interrupted": return "warn";
    case "skipped": return "neutral";
    default: return "neutral";
  }
}

// field_recovery.current_evaluation states
export function fieldStateTone(state: string | null | undefined): Tone {
  switch (state) {
    case "ok": return "ok";
    case "not_applicable": return "neutral";
    case "conflicting": return "danger";
    case "foreign_market_only":
    case "variant_not_exact":
    case "weak_provenance": return "warn";
    case "missing":
    case "unresolved": return "info";
    default: return "neutral";
  }
}

export const STATUS_FILTERS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "COMPLETED", label: "Completed" },
  { id: "FAILED", label: "Failed" },
  { id: "INTERRUPTED", label: "Interrupted" },
  { id: "CANCELLED", label: "Cancelled" },
] as const;

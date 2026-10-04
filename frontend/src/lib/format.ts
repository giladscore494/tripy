// Formatting helpers (pure). Times are shown in the viewer's locale; technical ids are never transformed.

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit",
                                          minute: "2-digit", second: "2-digit" });
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatRelative(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return String(value);
  const seconds = Math.round((now - then) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 36) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function formatNumber(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(
    undefined, { maximumFractionDigits: 4 });
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export function formatCost(value: unknown): string {
  if (typeof value === "number") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  if (value && typeof value === "object") {
    const total = (value as Record<string, unknown>).total_usd ?? (value as Record<string, unknown>).usd;
    if (typeof total === "number") return `$${total.toFixed(total < 1 ? 4 : 2)}`;
  }
  return "—";
}

/** A value of any engine type as one readable line (objects compactly, never "[object Object]"). */
export function formatValue(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  let text: string;
  if (Array.isArray(value)) text = value.map((v) => formatValue(v)).join(", ");
  else if (typeof value === "object") text = compactJson(value);
  else if (typeof value === "number") text = formatNumber(value);
  else text = String(value);
  return unit ? `${text} ${unit}` : text;
}

export function compactJson(value: unknown, limit = 240): string {
  let text: string;
  try {
    text = JSON.stringify(value);
  } catch {
    text = String(value);
  }
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function truncate(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function humanize(key: string | null | undefined): string {
  if (!key) return "—";
  return key.replace(/_/g, " ").replace(/\s+/g, " ").trim().replace(/^./, (c) => c.toUpperCase());
}

export function domainOf(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return "";
  }
}

export function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

import { useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { createPortal } from "react-dom";

import type { ApiError } from "../../api/client";
import { textDirection } from "../../lib/bidi";
import type { Tone } from "../../lib/status";
import { IconAlert, IconChevron, IconClose, IconCopy, IconExternal, IconRefresh } from "./icons";
import { formatValue } from "../../lib/format";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// --- surfaces ---------------------------------------------------------------------------------------------------------

export function Panel({ children, className, title, kicker, actions, padded = true, id }: {
  children: ReactNode; className?: string; title?: ReactNode; kicker?: ReactNode; actions?: ReactNode;
  padded?: boolean; id?: string;
}) {
  return (
    <section id={id} className={cx("glass rounded-panel shadow-panel animate-fade-in", padded && "p-5 sm:p-6",
                                   className)}>
      {(title || kicker || actions) && (
        <header className={cx("mb-4 flex flex-wrap items-start justify-between gap-3", !padded && "px-5 pt-5 sm:px-6")}>
          <div className="min-w-0">
            {kicker && <p className="kicker mb-1">{kicker}</p>}
            {title && <h2 className="text-base font-semibold tracking-tight text-ink sm:text-lg">{title}</h2>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Card({ children, className, active, onClick }: {
  children: ReactNode; className?: string; active?: boolean; onClick?: () => void;
}) {
  const classes = cx("rounded-card border bg-glass p-4 transition-all duration-200",
                     active ? "border-accent/50 shadow-glow" : "border-line",
                     onClick && "cursor-pointer hover:bg-glass-hover hover:border-line-strong", className);
  if (onClick) {
    return <button type="button" onClick={onClick} className={cx(classes, "w-full text-left")}>{children}</button>;
  }
  return <div className={classes}>{children}</div>;
}

// --- controls ---------------------------------------------------------------------------------------------------------

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export function Button({ variant = "secondary", size = "md", className, children, busy, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: "sm" | "md"; busy?: boolean }) {
  const styles: Record<ButtonVariant, string> = {
    primary: "bg-accent text-bg-deep hover:bg-accent-soft shadow-[0_0_24px_-8px_rgb(var(--c-accent)/0.8)] font-semibold",
    secondary: "border border-line bg-glass text-ink hover:bg-glass-hover hover:border-line-strong",
    ghost: "text-ink-muted hover:text-ink hover:bg-white/[0.06]",
    danger: "border border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
  };
  return (
    <button type="button" {...props} disabled={props.disabled || busy} aria-busy={busy || undefined}
            className={cx("inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl transition-all duration-150",
                          "disabled:cursor-not-allowed disabled:opacity-45",
                          size === "sm" ? "px-3 py-1.5 text-xs" : "px-4 py-2.5 text-sm", styles[variant], className)}>
      {busy && <Spinner size={14} />}
      {children}
    </button>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span role="status" aria-label="Loading" style={{ width: size, height: size }}
          className="inline-block animate-spin rounded-full border-2 border-current border-t-transparent opacity-70" />
  );
}

const TONES: Record<Tone, string> = {
  accent: "border-accent/40 bg-accent/15 text-accent-soft",
  ok: "border-ok/35 bg-ok/10 text-ok",
  warn: "border-warn/35 bg-warn/10 text-warn",
  danger: "border-danger/40 bg-danger/10 text-danger",
  info: "border-info/35 bg-info/10 text-info",
  neutral: "border-line bg-white/[0.04] text-ink-muted",
  violet: "border-violet/50 bg-violet/20 text-ink",
};

export function Badge({ tone = "neutral", children, className, pulse, title }: {
  tone?: Tone; children: ReactNode; className?: string; pulse?: boolean; title?: string;
}) {
  return (
    <span title={title}
          className={cx("inline-flex max-w-full items-center gap-1.5 truncate rounded-full border px-2.5 py-0.5 text-[11.5px] font-medium",
                        TONES[tone], className)}>
      {pulse && <span className="h-1.5 w-1.5 shrink-0 animate-pulse-soft rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: {
  tabs: { id: T; label: ReactNode; count?: number | null }[]; value: T; onChange: (id: T) => void; label: string;
}) {
  return (
    <div role="tablist" aria-label={label}
         className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 [scrollbar-width:none]">
      {tabs.map((tab) => (
        <button key={tab.id} role="tab" type="button" aria-selected={tab.id === value} onClick={() => onChange(tab.id)}
                className={cx("shrink-0 rounded-xl px-3.5 py-2 text-sm transition-all duration-150",
                              tab.id === value ? "bg-accent/15 text-accent-soft shadow-glow"
                                : "text-ink-muted hover:bg-white/[0.06] hover:text-ink")}>
          {tab.label}
          {tab.count !== undefined && tab.count !== null && (
            <span className="ml-1.5 rounded-md bg-white/[0.08] px-1.5 py-px text-[11px] text-ink-muted">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ options, value, onChange, label }: {
  options: { id: T; label: ReactNode; hint?: string }[]; value: T; onChange: (id: T) => void; label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="grid gap-2 sm:auto-cols-fr sm:grid-flow-col">
      {options.map((option) => (
        <button key={option.id} type="button" role="radio" aria-checked={option.id === value}
                onClick={() => onChange(option.id)}
                className={cx("rounded-card border px-4 py-3 text-left transition-all duration-200",
                              option.id === value ? "border-accent/55 bg-accent/10 shadow-glow"
                                : "border-line bg-glass hover:bg-glass-hover")}>
          <span className="block text-sm font-semibold text-ink">{option.label}</span>
          {option.hint && <span className="mt-0.5 block text-xs text-ink-muted">{option.hint}</span>}
        </button>
      ))}
    </div>
  );
}

// --- text -------------------------------------------------------------------------------------------------------------

/** Text whose direction follows its own content (Hebrew labels, quotes, source text); the shell stays LTR. */
export function Dir({ children, className, as = "span", title }: {
  children: string | null | undefined; className?: string; as?: "span" | "p" | "div"; title?: string;
}) {
  const text = children ?? "";
  const Tag = as;
  return <Tag dir={textDirection(text)} className={cx("[unicode-bidi:isolate]", className)} title={title}>{text}</Tag>;
}

/** A technical identifier (run id, record id, URL, timestamp): always LTR, monospace, never translated. */
export function Mono({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return <span dir="ltr" title={title} className={cx("mono break-words text-ink-muted [unicode-bidi:isolate]", className)}>{children}</span>;
}

export function ExternalLink({ href, children, className }: { href: string | null | undefined; children?: ReactNode;
                                                               className?: string }) {
  if (!href) return <span className="text-ink-faint">—</span>;
  const safe = /^https?:\/\//i.test(href);
  if (!safe) return <Mono>{href}</Mono>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" dir="ltr"
       className={cx("inline-flex max-w-full items-center gap-1 text-accent-soft underline-offset-2 hover:underline [unicode-bidi:isolate]",
                     className)}>
      <span className="truncate">{children ?? href}</span>
      <IconExternal size={13} className="shrink-0" />
    </a>
  );
}

export function KeyValue({ items, columns = 2 }: { items: { label: ReactNode; value: ReactNode }[];
                                                  columns?: 1 | 2 | 3 | 4 }) {
  const cols = { 1: "", 2: "sm:grid-cols-2", 3: "sm:grid-cols-2 lg:grid-cols-3", 4: "sm:grid-cols-2 lg:grid-cols-4" };
  return (
    <dl className={cx("grid grid-cols-1 gap-x-6 gap-y-3", cols[columns])}>
      {items.map((item, index) => (
        <div key={index} className="min-w-0">
          <dt className="text-[11px] uppercase tracking-wider text-ink-faint">{item.label}</dt>
          <dd className="mt-0.5 min-w-0 break-words text-sm text-ink">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stat({ label, value, hint, tone }: { label: ReactNode; value: ReactNode; hint?: ReactNode;
                                                     tone?: Tone }) {
  const color = tone === "danger" ? "text-danger" : tone === "warn" ? "text-warn" : tone === "accent"
    ? "text-accent-soft" : "text-ink";
  return (
    <div className="rounded-card border border-line bg-white/[0.03] p-4">
      <p className="text-[11px] uppercase tracking-wider text-ink-faint">{label}</p>
      <p className={cx("mt-1 text-2xl font-semibold tracking-tight", color)}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}

export function ProgressBar({ value, max, label }: { value: number; max: number; label?: string }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="w-full" role="progressbar" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}
         aria-label={label}>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
        <div className="h-full rounded-full bg-gradient-to-r from-accent to-accent-soft transition-[width] duration-500"
             style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// --- states -----------------------------------------------------------------------------------------------------------

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cx("skeleton h-4", className)} />;
}

export function SkeletonRows({ rows = 5, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-3">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-9 w-9 shrink-0 rounded-xl" />
          <div className="flex-1 space-y-2">
            <Skeleton className={cx("h-3.5", i % 2 ? "w-2/3" : "w-1/2")} />
            <Skeleton className="h-3 w-1/3" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function SkeletonCards({ count = 5 }: { count?: number }) {
  return (
    <div role="status" aria-label="Loading" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
      {Array.from({ length: count }, (_, i) => <Skeleton key={i} className="h-28 rounded-card" />)}
    </div>
  );
}

export function EmptyState({ title, children, icon, action }: { title: string; children?: ReactNode;
                                                                icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-card border border-dashed border-line px-6 py-10 text-center">
      {icon && <div className="mb-3 rounded-2xl border border-line bg-glass p-3 text-accent-soft">{icon}</div>}
      <p className="text-sm font-semibold text-ink">{title}</p>
      {children && <div className="mt-1 max-w-md text-sm text-ink-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, title = "This view could not be loaded", onRetry }: {
  error: ApiError | null | undefined; title?: string; onRetry?: () => void;
}) {
  if (!error) return null;
  return (
    <div role="alert" className="flex flex-col gap-3 rounded-card border border-danger/30 bg-danger/[0.07] p-4 sm:flex-row sm:items-center">
      <IconAlert className="shrink-0 text-danger" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-ink">{title}</p>
        <p className="mt-0.5 break-words text-sm text-ink-muted">{error.message}</p>
        {error.code && error.code !== "http_error" && <Mono className="text-[11px]">{error.code}</Mono>}
      </div>
      {onRetry && <Button size="sm" onClick={onRetry}><IconRefresh size={14} />Retry</Button>}
    </div>
  );
}

export function Notice({ tone = "info", title, children, action }: { tone?: Tone; title?: ReactNode;
                                                                    children?: ReactNode; action?: ReactNode }) {
  const border = { accent: "border-accent/35 bg-accent/[0.07]", ok: "border-ok/30 bg-ok/[0.07]",
                   warn: "border-warn/35 bg-warn/[0.07]", danger: "border-danger/35 bg-danger/[0.07]",
                   info: "border-info/30 bg-info/[0.06]", neutral: "border-line bg-white/[0.03]",
                   violet: "border-violet/40 bg-violet/15" }[tone];
  return (
    <div role={tone === "danger" ? "alert" : "status"}
         className={cx("flex flex-col gap-3 rounded-card border p-4 sm:flex-row sm:items-center", border)}>
      <div className="min-w-0 flex-1">
        {title && <p className="text-sm font-semibold text-ink">{title}</p>}
        {children && <div className="mt-0.5 text-sm text-ink-muted">{children}</div>}
      </div>
      {action}
    </div>
  );
}

// --- disclosure / overlay ---------------------------------------------------------------------------------------------

export function Disclosure({ title, children, defaultOpen = false, className }: {
  title: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className={cx("rounded-card border border-line bg-white/[0.02]", className)}>
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
              className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium text-ink-muted hover:text-ink">
        <IconChevron size={16} className={cx("transition-transform duration-200", open && "rotate-90")} />
        {title}
      </button>
      {open && <div id={id} className="border-t border-line px-4 py-3">{children}</div>}
    </div>
  );
}

export function JsonBlock({ value, maxHeight = "28rem" }: { value: unknown; maxHeight?: string }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <pre dir="ltr" style={{ maxHeight }}
         className="mono overflow-auto whitespace-pre-wrap break-words rounded-xl bg-bg-deep/70 p-3 text-ink-muted">{text}</pre>
  );
}

export function Drawer({ open, onClose, title, children, wide }: {
  open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  // portalled to <body>: never affected by a parent's spacing, transform or overflow
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <button type="button" aria-label="Close" onClick={onClose}
              className="absolute inset-0 bg-bg-deep/70 backdrop-blur-sm animate-fade-in" />
      <div ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}
           className={cx("glass relative flex h-full w-full flex-col bg-bg-secondary/80 shadow-panel animate-fade-in outline-none",
                         wide ? "max-w-5xl" : "max-w-2xl")}>
        <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0 text-base font-semibold">{title}</div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close panel"><IconClose size={16} /></Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" aria-label={label} title={label}
            onClick={() => {
              void navigator.clipboard?.writeText(text).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              }).catch(() => undefined);
            }}
            className="inline-flex items-center rounded-md p-1 text-ink-faint hover:bg-white/[0.06] hover:text-ink">
      {copied ? <span className="text-[11px] text-accent-soft">Copied</span> : <IconCopy size={13} />}
    </button>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: ReactNode; hint?: ReactNode;
                                                            children: ReactNode; htmlFor?: string }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-xs font-medium text-ink-muted">{label}</label>
      {children}
      {hint && <p className="text-[11.5px] text-ink-faint">{hint}</p>}
    </div>
  );
}

/** A plain table of server rows (every cell formatted with formatValue, LTR ids kept readable). Shows `limit` rows
 *  and a "Show all" control beyond that; never computes anything. */
export function DataTable({ rows, columns, limit = 100, label, maxHeight = "max-h-[32rem]" }: {
  rows: Record<string, unknown>[]; columns?: string[]; limit?: number; label?: string; maxHeight?: string;
}) {
  const [all, setAll] = useState(false);
  const cols = columns ?? [...new Set(rows.flatMap((r) => Object.keys(r)))];
  if (!rows.length) return null;
  const shown = all ? rows : rows.slice(0, limit);
  return (
    <div className="space-y-1.5">
      <div className={cx("table-wrap", maxHeight)}>
        <table className="data-table" aria-label={label}>
          <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i}>{cols.map((c) => (
                <td key={c} className="max-w-[28rem] text-xs text-ink-muted"><span dir="auto" className="break-words">{formatValue(row[c])}</span></td>
              ))}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > limit && (
        <Button size="sm" variant="ghost" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${rows.length} rows`}</Button>
      )}
    </div>
  );
}

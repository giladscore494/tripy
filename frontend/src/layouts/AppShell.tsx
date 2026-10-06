import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import {
  IconClose, IconDashboard, IconList, IconLogout, IconMenu, IconPulse, IconSearch, IconServer, IconSliders, IconSpark,
  IconSplit,
} from "../components/ui/icons";
import { Badge, cx } from "../components/ui/primitives";
import { useWorkspace } from "../hooks/WorkspaceData";
import { formatDuration } from "../lib/format";

const NAV = [
  { to: "/", label: "Dashboard", icon: IconDashboard, end: true },
  { to: "/research/new", label: "New Research", icon: IconSpark },
  { to: "/runs", label: "Runs", icon: IconList },
  { to: "/series", label: "A/B Series", icon: IconSplit },
  { to: "/bakeoffs", label: "Search bake-off", icon: IconSearch },
  { to: "/diagnostics", label: "Diagnostics", icon: IconPulse },
  { to: "/data", label: "Data", icon: IconServer },
  { to: "/settings", label: "Settings", icon: IconSliders },
];

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <Link to="/" className="group flex items-center gap-3" aria-label="TRIPY dashboard">
      <span className="relative grid h-9 w-9 place-items-center rounded-xl border border-accent/40 bg-gradient-to-br from-accent/30 via-bg-secondary to-violet/40 shadow-glow">
        <span className="text-[15px] font-black tracking-tight text-ink">T</span>
      </span>
      {!compact && (
        <span className="leading-tight">
          <span className="block text-[15px] font-bold tracking-[0.18em] text-ink">TRIPY</span>
          <span className="block text-[11px] text-ink-muted">Research Intelligence Workspace</span>
        </span>
      )}
    </Link>
  );
}

function StatusChips() {
  const { health, config } = useWorkspace();
  const healthy = health.data?.status === "ok" && !health.error;
  const cfg = config.data;
  return (
    <div className="flex items-center gap-2">
      <Badge tone={health.loading ? "neutral" : healthy ? "ok" : "danger"} pulse={healthy}
             title="GET /health">{health.loading ? "Checking…" : healthy ? "Backend online" : "Backend unreachable"}</Badge>
      {cfg && (
        <Badge tone={cfg.configured ? "accent" : "warn"} className="hidden sm:inline-flex"
               title={cfg.blocking.length ? `Blocking: ${cfg.blocking.join(", ")}` : "No blocking check"}>
          {cfg.configured ? "Configured" : `${cfg.blocking.length} blocking`}
        </Badge>
      )}
      {cfg && <Badge tone="violet" className="hidden md:inline-flex">{cfg.environment}</Badge>}
    </div>
  );
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="space-y-1">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink key={to} to={to} end={end} onClick={onNavigate}
                 className={({ isActive }) => cx(
                   "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-all duration-150",
                   isActive ? "bg-accent/15 text-accent-soft shadow-glow"
                     : "text-ink-muted hover:bg-white/[0.06] hover:text-ink")}>
          <Icon size={18} />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

function ActiveRuns({ onNavigate }: { onNavigate?: () => void }) {
  const { activeRuns } = useWorkspace();
  if (!activeRuns.length) return null;
  return (
    <div className="mt-6">
      <p className="kicker mb-2 px-3">Active research</p>
      <ul className="space-y-1.5">
        {activeRuns.slice(0, 5).map((run) => (
          <li key={run.run_id}>
            <Link to={`/runs/${encodeURIComponent(run.run_id)}`} onClick={onNavigate}
                  className="block rounded-xl border border-accent/25 bg-accent/[0.06] px-3 py-2 transition-colors hover:bg-accent/[0.12]">
              <span className="flex items-center gap-2">
                <span className="h-1.5 w-1.5 shrink-0 animate-pulse-soft rounded-full bg-accent" />
                <span dir="auto" className="truncate text-xs font-medium text-ink">{run.label}</span>
              </span>
              <span className="mt-0.5 block truncate pl-3.5 text-[11px] text-ink-muted">
                {run.status_label} · {formatDuration(run.elapsed_s)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { logout, accessRequired } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    window.scrollTo?.({ top: 0 });
  }, [location.pathname]);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-line bg-bg/70 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-[1600px] items-center gap-3 px-4 sm:px-6">
          <button type="button" className="rounded-xl p-2 text-ink-muted hover:bg-white/[0.06] hover:text-ink lg:hidden"
                  aria-label="Open navigation" aria-expanded={open} aria-controls="mobile-nav" onClick={() => setOpen(true)}>
            <IconMenu />
          </button>
          <Brand />
          <div className="ml-auto flex items-center gap-2 sm:gap-3">
            <StatusChips />
            {accessRequired && (
              <button type="button" onClick={logout} title="Lock this session"
                      className="flex items-center gap-2 rounded-xl border border-line px-3 py-1.5 text-xs text-ink-muted transition-colors hover:bg-white/[0.06] hover:text-ink">
                <IconLogout size={15} />
                <span className="hidden sm:inline">Lock</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" id="mobile-nav">
          <button type="button" aria-label="Close navigation" className="absolute inset-0 bg-bg-deep/70 backdrop-blur-sm"
                  onClick={() => setOpen(false)} />
          <aside role="dialog" aria-modal="true" aria-label="Navigation"
                 className="glass absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-bg-secondary/90 p-4 animate-fade-in">
            <div className="mb-6 flex items-center justify-between">
              <Brand compact />
              <button type="button" aria-label="Close navigation" onClick={() => setOpen(false)}
                      className="rounded-xl p-2 text-ink-muted hover:bg-white/[0.06] hover:text-ink">
                <IconClose />
              </button>
            </div>
            <NavItems onNavigate={() => setOpen(false)} />
            <ActiveRuns onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="mx-auto flex max-w-[1600px] gap-6 px-4 sm:px-6">
        <aside className="sticky top-16 hidden h-[calc(100vh-4rem)] w-60 shrink-0 overflow-y-auto py-6 lg:block">
          <NavItems />
          <ActiveRuns />
        </aside>
        <main className="min-w-0 flex-1 py-6 pb-16">{children}</main>
      </div>
    </div>
  );
}

export function PageHeader({ title, kicker, children, actions }: {
  title: ReactNode; kicker?: ReactNode; children?: ReactNode; actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {kicker && <p className="kicker mb-1.5">{kicker}</p>}
        <h1 className="text-2xl font-semibold tracking-tight text-ink sm:text-[28px]">{title}</h1>
        {children && <div className="mt-1.5 text-sm text-ink-muted">{children}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

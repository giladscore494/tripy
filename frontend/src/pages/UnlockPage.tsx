import { useState, type FormEvent } from "react";

import { useAuth } from "../auth/AuthProvider";
import { IconAlert, IconLock, IconRefresh, IconServer } from "../components/ui/icons";
import { Button, Spinner } from "../components/ui/primitives";

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center px-4 py-12">
      <div className="w-full max-w-md animate-fade-in">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl border border-accent/40 bg-gradient-to-br from-accent/30 via-bg-secondary to-violet/40 shadow-glow">
            <span className="text-xl font-black text-ink">T</span>
          </div>
          <p className="text-lg font-bold tracking-[0.22em] text-ink">TRIPY</p>
          <p className="text-sm text-ink-muted">Research Intelligence Workspace</p>
        </div>
        <div className="glass rounded-panel p-6 shadow-panel sm:p-8">{children}</div>
      </div>
    </div>
  );
}

export function UnlockPage() {
  const { unlock, notice } = useAuth();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    const ok = await unlock(token);
    setBusy(false);
    if (!ok) setToken("");
  };

  return (
    <Frame>
      <form onSubmit={submit} className="space-y-5" autoComplete="off">
        <div className="flex items-center gap-3">
          <span className="rounded-xl border border-line bg-glass p-2.5 text-accent-soft"><IconLock /></span>
          <div>
            <h1 className="text-base font-semibold text-ink">Unlock workspace</h1>
            <p className="text-xs text-ink-muted">Enter the TRIPY access token for this deployment.</p>
          </div>
        </div>
        {notice && (
          <div role="alert" className="flex gap-2 rounded-xl border border-warn/35 bg-warn/[0.08] p-3 text-sm text-ink">
            <IconAlert size={17} className="mt-0.5 shrink-0 text-warn" />
            <span>{notice}</span>
          </div>
        )}
        <div className="space-y-1.5">
          <label htmlFor="access-token" className="block text-xs font-medium text-ink-muted">Access token</label>
          <input id="access-token" type="password" className="input" value={token} autoFocus spellCheck={false}
                 autoCapitalize="off" autoCorrect="off" onChange={(e) => setToken(e.target.value)}
                 placeholder="••••••••••••••••" />
          <p className="text-[11.5px] text-ink-faint">
            Kept only in this browser tab&apos;s session storage and sent as a Bearer header. Closing the tab forgets it.
          </p>
        </div>
        <Button type="submit" variant="primary" className="w-full" busy={busy} disabled={!token.trim()}>
          Unlock
        </Button>
      </form>
    </Frame>
  );
}

export function CheckingScreen() {
  return (
    <Frame>
      <div className="flex items-center justify-center gap-3 py-4 text-sm text-ink-muted">
        <Spinner /> Connecting to the TRIPY server…
      </div>
    </Frame>
  );
}

export function ServerProblemScreen({ kind, message, onRetry }: {
  kind: "misconfigured" | "unreachable"; message: string | null; onRetry: () => void;
}) {
  return (
    <Frame>
      <div className="space-y-4" role="alert">
        <div className="flex items-center gap-3">
          <span className="rounded-xl border border-danger/40 bg-danger/10 p-2.5 text-danger"><IconServer /></span>
          <h1 className="text-base font-semibold text-ink">
            {kind === "misconfigured" ? "Server access control is not configured" : "The TRIPY server is unreachable"}
          </h1>
        </div>
        <p className="text-sm text-ink-muted">
          {kind === "misconfigured"
            ? "This production server has no TRIPY_ACCESS_TOKEN, so it refuses every request. An operator must set the variable on the server; there is nothing to enter here."
            : "The workspace could not reach the API. Check that the backend is running."}
        </p>
        {message && <p className="rounded-xl bg-bg-deep/60 p-3 text-xs text-ink-muted">{message}</p>}
        <Button onClick={onRetry} className="w-full"><IconRefresh size={15} />Try again</Button>
      </div>
    </Frame>
  );
}

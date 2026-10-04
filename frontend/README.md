# TRIPY research workspace (frontend)

React 19 · TypeScript (strict) · Vite · Tailwind CSS 3 · React Router. No state library: server state lives in a few
polling hooks, UI state in components.

> **One process per `TRIPY_DATA_DIR`.** Run the FastAPI backend against a dedicated development data directory (or
> give Streamlit and FastAPI separate ones). Two processes on one data directory mark each other's active runs as
> interrupted.

```bash
TRIPY_DATA_DIR=../.tripy-data-api uvicorn src.api.app:app --host 127.0.0.1 --port 8000   # from the repo root
npm install
npm run dev          # http://localhost:5173 — /api and /health proxied to TRIPY_API_URL (default http://127.0.0.1:8000)
npm test             # Vitest + React Testing Library, HTTP mocked
npm run typecheck
npm run build        # → dist/
```

## Layout

```
src/
  api/        client.ts (the one HTTP client: Bearer header, error envelope, authenticated downloads)
              endpoints.ts (typed route functions) · types.ts (mirror of src/api/schemas.py)
  auth/       AuthProvider (unlock / 401 / server-misconfigured states) · tokenStore (sessionStorage only)
  hooks/      useResource (the single polling primitive) · useRunMonitor (progress 2 s, detail on change)
              useEventStream (line-cursor tailing) · WorkspaceData (health, config, run list — polled once for all pages)
  layouts/    AppShell (top bar, side nav, mobile drawer, active runs)
  pages/      Dashboard · NewResearch · Runs · Run · Series (list + detail) · Diagnostics · Settings · Unlock
  components/ ui/ (design-system primitives, icons) · run/ (pipeline, timeline, results, candidates, evidence,
              documents, Binding Replay, diagnostics, failure card, per-run settings form)
  styles/     tokens.css (every color / glass / radius token) · index.css (atmosphere, component classes)
  lib/        formatting, status tones, bidi, idempotency keys
  test/       Vitest suites, an HTTP mock, fixtures captured from the real backend
```

## Rules the code follows

- **Auth.** The token is only ever in `sessionStorage` (`tripy.access`) and the `Authorization` header; never in a
  URL, a log or `localStorage`. 401 → token cleared, unlock screen with an expiry notice.
  `access_control_not_configured` → a server-configuration screen (no login loop). Development without a token works
  when the API needs none.
- **Polling.** Only `useResource` / `useEventStream` / `useInterval` start timers; pages pick intervals. Active run:
  progress ~2 s, events ~1.75 s, detail ~12 s and right after a status change; terminal run: no polling. Hidden tabs
  skip ticks.
- **Events.** Cursor starts at 0, follows `next_cursor`, fetches `more` pages immediately, never uses array length,
  never reloads history, never appends a line twice, restarts at 0 per vehicle (`record_id`).
- **Submissions.** One idempotency key per deliberate submission, reused for network retries, cleared once resolved;
  a synchronous in-flight guard stops double clicks.
- **Data.** The backend computes every metric; the UI formats. Raw JSON only appears in technical expanders.
- **Design tokens.** Colors are defined once in `styles/tokens.css` and used through Tailwind theme names.
- **Bidi.** The shell is LTR; `<Dir>` renders Hebrew labels, quotes and source text in their own direction; `<Mono>`
  keeps ids, URLs and timestamps LTR.

Production serving (PR #3): FastAPI serves `dist/` and answers every non-`/api` path with `dist/index.html`, so
refreshing `/runs/<id>` or `/series/<id>` works.

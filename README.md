# TRIPY — AI Vehicle Research

TRIPY is the MILO GLM web-research benchmark (v1) packaged as a deployable research application: given the full
Level 1.5 record of a known vehicle variant and a strong set of web tools, how much useful, evidence-backed
information can a GLM model find, organize and return?

It is **not** part of the MILO production pipeline, does not protect the catalog and does not promote anything to
production.

```
Supabase Level 1.5 → benchmark loader → GLM primary research (source acquisition) → deterministic harvest
  → local-first document sweep → clustered tail recovery → durable checkpoint → GLM finalizer
  → structured result → Streamlit dashboard
```

## Deploy TRIPY to Railway

TRIPY deploys as **one Railway service** (Streamlit UI + background research runs in the same process) built
from the repository's `Dockerfile`, with one **Railway Volume** for durable state.

```
GitHub repo ──► Railway service (Dockerfile) ──► TRIPY Streamlit app ──► RunManager threads ──► research engine
                       │                                                                         │
                       └── Volume mounted at /data (TRIPY_DATA_DIR=/data)                        └─► GLM / web search
```

1. Create a Railway account at <https://railway.com>.
2. **New Project**.
3. **Deploy from GitHub repo**.
4. Select the TRIPY repository (and the branch to deploy). Railway detects `railway.json` and builds the
   `Dockerfile`; no source edits are needed.
5. Open the service → **Variables** and add the variables in [REQUIRED](#required): `GLM_API_KEY`, `GLM_MODEL` and
   `TRIPY_ACCESS_TOKEN` (generate it with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; never
   commit it). Optional ones are listed below.
6. Create a Volume: in the project canvas, **Create → Volume** (or right-click the service → *Attach volume*) and
   attach it to the TRIPY service.
7. Set the volume's **mount path** to `/data`.
8. In **Variables**, set `TRIPY_DATA_DIR=/data` (the Dockerfile already defaults to it; setting it explicitly makes
   the intent visible).
9. **Deploy** (Railway redeploys automatically after variable / volume changes).
10. Service → **Settings → Networking → Generate Domain** to get a public URL — **only after** `GLM_API_KEY`,
    `GLM_MODEL`, `TRIPY_ACCESS_TOKEN` and `TRIPY_DATA_DIR=/data` are configured. A Railway public domain exposes the
    service to the internet; `TRIPY_ACCESS_TOKEN` is what protects the dashboard and your GLM / search budget.

What Railway uses (all in the repo):

| Item | Value |
| --- | --- |
| Builder | `Dockerfile` (`railway.json` → `build.builder = DOCKERFILE`) |
| Start command | `sh scripts/start.sh` → `streamlit run app.py --server.address=0.0.0.0 --server.port=$PORT --server.headless=true …` |
| Start command with `TRIPY_MCP_TOKEN` | the same flags with `streamlit run tripy_server.py` (Streamlit's `st.App` serving the same `app.py` plus `/mcp/<token>`) |
| Port | Railway's injected `$PORT` (8501 when unset) |
| Health check | `GET /_stcore/health` (Streamlit's built-in endpoint, returns `ok`) |
| Volume mount path | `/data` |
| Replicas | **1** (a Volume attaches to one deployment; research runs execute inside the web process) |

On startup `python -m src.startup_check` logs the resolved paths and which variables are present (never their
values). The app itself shows a **System** panel (GLM, Search, Persistent storage, Level 1.5 data). If
`GLM_API_KEY` / `GLM_MODEL` are missing, it says exactly which ones and disables *Start research*; if no volume is
attached on Railway it warns that runs are stored on ephemeral disk.

**Access control (production).** The production dashboard fails closed: without `TRIPY_ACCESS_TOKEN` it shows only
*"Production access control is not configured."*; with it, every browser session first sees a minimal lock screen
(access token + *Unlock*) and nothing else — no research target, *Start research*, run history, results, diagnostics,
retry / restart or settings — until the token is entered. The check is constant-time and the token is opaque
(compared exactly as configured). The session keeps only an `authenticated` flag, never the token; *Logout* (sidebar)
locks the session again without touching runs or history. After five wrong tokens a session waits 30 s before the
next attempt. A browser refresh starts a new Streamlit session, so the token is asked for again. The token is never
read from the URL or a cookie, and it is redacted from logs, run state, diagnostics and error details like the other
secrets. The `/_stcore/health` check is not gated. Outside production (local development) the gate is open.

### Read-only MCP for Claude (optional)

One variable turns on a **read-only** MCP server on the same service and port: runs, live events, results,
diagnostics, binding replay, candidates, fetched documents and the server log (12 tools; nothing can start, cancel,
delete or write). Without the variable it does not exist at all (no route, no process, the plain start command).

1. Railway → service → **Variables** → add `TRIPY_MCP_TOKEN` = a long random value (`openssl rand -hex 32`).
   Redeploy.
2. claude.ai → **Settings → Connectors → Add custom connector** → URL
   `https://tripy-production.up.railway.app/mcp/<the value>` → no auth.
3. To revoke: change or delete the variable, then redeploy.

The URL path is the password: keep it secret. Any other `/mcp/...` path answers 404. The token is never logged; every
tool call writes one audit line (tool, ids, size, ms) to the server log (`/data/logs/tripy.log`, rotating 5 × 5 MB).

**MCP לקריאה בלבד עבור Claude (אופציונלי).** משתנה אחד מפעיל שרת MCP לקריאה בלבד באותו שירות ובאותו פורט: ריצות,
אירועים חיים, תוצאות, דיאגנוסטיקה, binding replay, מועמדים, מסמכים שנשלפו ולוג השרת. אף כלי לא מתחיל, מבטל, מוחק או
כותב דבר. בלי המשתנה ה-MCP לא קיים בכלל.

1. ב-Railway: השירות ← **Variables** ← להוסיף `TRIPY_MCP_TOKEN` עם ערך אקראי ארוך (`openssl rand -hex 32`), ואז
   Redeploy.
2. ב-claude.ai: **Settings ← Connectors ← Add custom connector**, כתובת
   `https://tripy-production.up.railway.app/mcp/<הערך>`, בלי אימות (no auth).
3. לביטול הגישה: לשנות או למחוק את המשתנה, ואז Redeploy.

הנתיב בכתובת הוא הסיסמה ויש לשמור עליו בסוד. כל נתיב אחר תחת `/mcp/` מחזיר 404.

**Secrets never go into the UI in production.** `TRIPY_ENV=production` (set by the Dockerfile, also implied by
Railway's own variables) removes the development-only API-key box; keys come from Railway variables only.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate        # Python 3.11+
pip install -r requirements.txt
cp .env.example .env                                  # fill GLM_API_KEY and GLM_MODEL (DATABASE_URL optional)
set -a; . ./.env; set +a
streamlit run app.py                                  # http://localhost:8501, hot reload on
```

State goes to `./.tripy-data/` (git-ignored) unless `TRIPY_DATA_DIR` is set. To keep using an existing `runs/`
folder, set `MILO_RUNS_DIR=runs` (the cache then stays at `runs/_cache`, as before). In development you may type an
API key in *Advanced settings* instead of exporting it (`TRIPY_ALLOW_UI_API_KEY` controls this).

Production-like run, exactly as Railway starts it:

```bash
docker build -t tripy .
docker run -p 8501:8501 -e GLM_API_KEY=... -e GLM_MODEL=glm-5.3-flash -v "$PWD/.tripy-data:/data" tripy
```

Offline end-to-end check without credentials: `python scripts/fake_glm_server.py --port 8765` and start the app with
`GLM_BASE_URL=http://127.0.0.1:8765/api/paas/v4 GLM_API_KEY=fake GLM_MODEL=glm-5.3-flash NO_PROXY=127.0.0.1`
(`--fail finalizer` exercises the failure and retry UI).

## Configuration

Every supported variable, with safe placeholders, is in [`.env.example`](.env.example). Values come from the
environment first, then Streamlit secrets (same names).

### REQUIRED

| Variable | Purpose |
| --- | --- |
| `GLM_API_KEY` | Z.ai / GLM API key (secret). Also used by the default `glm` web search backend. |
| `GLM_MODEL` | Research model id (e.g. `glm-5.3-flash`), sent unchanged as the API `model`. |
| `TRIPY_ACCESS_TOKEN` | **Production:** the private dashboard access secret (a long random string). Without it the production dashboard refuses to open. Optional in local development (not enforced). |

### Deployment

| Variable | Purpose |
| --- | --- |
| `TRIPY_DATA_DIR` | Root of all durable state. Railway: `/data` (the Volume mount path). Local default `./.tripy-data`. |
| `TRIPY_ENV` | `production` disables development conveniences (set by the Dockerfile). |
| `PORT` | Injected by Railway; do not set it there. |

### OPTIONAL

| Variable | Purpose |
| --- | --- |
| `GLM_FINALIZER_MODEL` | Model id for the compact finalization call only; empty = `GLM_MODEL`. |
| `SEARCH_BACKEND` | `glm` (GLM `web_search` API, default) or `duckduckgo` (keyless HTML search). |
| `DATABASE_URL` (alias `SUPABASE_DB_URL`) | Postgres URL for the Level 1.5 source (secret); without it the bundled snapshot is used. |
| `TARGET_MARKET` | Target market (default `IL`). |
| `TRIPY_MAX_ACTIVE_RUNS` | Research runs that may execute at once in this server (default **1**). |
| `TRIPY_SHUTDOWN_GRACE_S` | Seconds active runs get to persist an interrupted result when the server stops (default 20; keep below Railway's `drainingSeconds`, 30 in `railway.json`). |
| `TRIPY_ALLOW_UI_API_KEY` | Allow typing an API key in the UI (default: development yes, production no). |
| `TRIPY_LOG_LEVEL` | Server log level on stdout (default `INFO`). |
| `MILO_RUNS_DIR`, `MILO_CACHE_DIR` | Legacy explicit storage overrides (win over `TRIPY_DATA_DIR`). |

### ADVANCED TUNING

Defaults are the tested research configuration; the research behaviour behind each is documented in the sections
below.

| Setting | Purpose |
| --- | --- |
| `GLM_CHAT_MAX_ATTEMPTS` | Total chat/completions HTTP attempts per request (default **2**; 1 = no retry). |
| `GLM_SEARCH_MAX_ATTEMPTS` | Total web_search HTTP attempts per request (default **3**). |
| `GLM_CHAT_TIMEOUT_S` | Read timeout per attempt in seconds (default 240). |
| `PRIMARY_RESEARCH_MAX_TURNS` (alias `AGENT_MAX_STEPS`) | Primary research (source acquisition) turn ceiling (default **6**). See [Primary research is source acquisition](#primary-research-is-source-acquisition). |
| `PRIMARY_RESEARCH_NO_ARTIFACT_STOP` | Stop primary research after N consecutive turns that acquired nothing new (default **2**; 0 = off), once the minimum acquisition base is met. |
| `PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS`, `PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE`, `PRIMARY_RESEARCH_HARD_MAX_TURNS` | Fail-safe minimum acquisition base (defaults **3** useful documents AND **50**% in-scope candidate coverage; both 0 = off) and the hard ceiling an under-acquired run may extend to (default **12**). See [Minimum acquisition base](#minimum-acquisition-base-fail-safe-stop). |
| `PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS`, `PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD` | Optional acquisition-sufficiency transition (both default **0** = off; e.g. `3` and `60` or `0.6`). Scheduling only. |
| `SWEEP_MODE`, `ADJUDICATION_MAX_U_ITEMS`, `ADJUDICATION_MAX_A_FIELDS`, `ADJUDICATION_MAX_A_CANDIDATES`, `ADJUDICATION_MAX_M_FIELDS`, `ADJUDICATION_MAX_M_SNIPPETS`, `ADJUDICATION_U_MAX_TOKENS`, `ADJUDICATION_A_MAX_TOKENS`, `ADJUDICATION_M_MAX_TOKENS` | `adjudication` (default): the document sweep only asks the model to JUDGE pre-checked candidates in small no-tool JSON packets (defaults 40 U items; A 6 fields / 18 candidates; M 6 fields / 12 snippets; max_tokens 4000 / 6000 / 4000: glm-5.3 always reasons and reasoning shares this budget; a reply cut at the cap is retried once with double the cap, at most 12000). `legacy`: the tool-loop sweep below. See [Candidate adjudication sweep](#candidate-adjudication-sweep-default). |
| `DOCUMENT_SWEEP_MAX_PACKET_CHARS`, `DOCUMENT_SWEEP_MAX_FIELDS`, `DOCUMENT_SWEEP_MAX_CANDIDATES`, `DOCUMENT_SWEEP_CANDIDATES_PER_FIELD` | `SWEEP_MODE=legacy` only (the chunk limits; the per-field count also sizes the pre-sweep snippets): adaptive document sweep: one call while the packet fits (defaults 28000 chars, 12 fields, 16 candidates), else deterministic chunks by `recovery_cluster`; candidates per field in the packet (default 3; storage keeps all). |
| `GLM_<PHASE>_THINKING`, `GLM_<PHASE>_MAX_TOKENS`, `GLM_<PHASE>_TEMPERATURE`, `GLM_<PHASE>_TIMEOUT_S`, `GLM_<PHASE>_MAX_ATTEMPTS`, `GLM_DOCUMENT_SWEEP_MODEL`, `GLM_RECOVERY_MODEL` | Optional per-phase overrides, PHASE = `RESEARCH`, `DOCUMENT_SWEEP`, `RECOVERY`, `FINALIZER`; empty = inherit the global setting, except `GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS`, whose default is `1` (no identical retry of a timed-out sweep packet). See [Phase-specific model settings](#phase-specific-model-settings). |
| `ACQUISITION_DOCUMENT_CARD` | `off` (default) / `on` (also `true`/`false`/`1`/`0`). Contract research only: successful `fetch_url` / `fetch_pdf` results carry a server-computed `document_card` (scheduling metadata, never evidence) and the research prompt gets one paragraph about it. See [Run profiles and A/B series](#run-profiles-and-ab-series). |
| `ACQUISITION_MODE` | `contract` (default): primary research is source acquisition only (search / fetch tools only, acquisition prompt, progress = new usable / target-market documents and new candidates, an under-acquired extension beyond the normal ceiling ends after an extension turn with no search / fetch or two extension turns without a new usable document, always finalized from the bundle). `legacy`: the previous behaviour exactly. |
| `ENRICHMENT_FIELDS` / `ENRICHMENT_SCHEMA_PATH` | Requested enrichment fields (default: `data/enrichment_fields.json`). |
| `IDENTITY_VOCABULARY_PATH`, `SOURCE_RULES_PATH` | Alternative identity vocabulary / source-authority rule files (defaults in `data/`). |
| `FIELD_RECOVERY_ENABLED`, `FIELD_RECOVERY_MAX_ATTEMPTS`, `FIELD_RECOVERY_MAX_STEPS`, `FIELD_RECOVERY_MAX_TOTAL_STEPS` | Targeted field retries (defaults `true`, `2`, `4`, `24` turns per vehicle; `0` = no cap). See [Targeted field recovery](#targeted-field-recovery-legacy-per-field-mode). |
| `RECOVERY_MODE`, `CLUSTER_MAX_ATTEMPTS`, `CLUSTER_BASE_TURNS`, `CLUSTER_MAX_TURNS`, `CLUSTER_SEARCH_BUDGET` | Tail recovery mode (`reacquire` default, `cluster` or `legacy`) and the cluster attempt limits (defaults `2` attempts, `2` base turns, ceiling `4`, `4` billable searches per attempt; `reacquire` uses `CLUSTER_SEARCH_BUDGET` per episode). See [Targeted re-acquisition](#targeted-re-acquisition-default) and [Clustered tail recovery](#clustered-tail-recovery). |
| `SITE_MAP`, `GROUNDED_CANDIDATES` | `on` (default) / `off`. The importer site map for contract acquisition and re-acquisition, and the grounded-candidate step before adjudication. See [Candidate yield](#candidate-yield-structural-harvest-site-map-grounded-candidates). |
| `RESEARCH_MEMORY_ENABLED`, `FACT_REUSE_MAX_AGE_DAYS`, `NEGATIVE_ROUTE_MAX_AGE_DAYS`, `NEGATIVE_ROUTE_BLOCKING` | Cross-run memory: verified fact reuse, negative routes, recovery yield (default on, 365 / 30 days). See [Verified fact reuse](#verified-fact-reuse-negative-research-memory-and-the-feedback-dataset). |
| `DISABLED_TOOLS` | Comma-separated tools never offered to the model (tools whose runtime capability is missing, such as `render_page` without Playwright, are left out automatically). |
| `LAYERED_HARVEST_ENABLED`, `DOCUMENT_SWEEP_MAX_TURNS` | Deterministic candidate harvest + model document sweep (defaults `true`, `2` = adaptive: a 2nd turn only after a turn-1 cached-document inspection returned content; absolute max 2). See [Layered field harvesting](#layered-field-harvesting). |
| `INCLUDE_LEVEL3` | Level 3 open research (default **off**; UI checkbox / `--level3`). It is opt-in so it cannot take research turns from the Level 2 benchmark. |
| `BATCH_MAX_WORKERS`, `GLM_CHAT_MAX_INFLIGHT`, `GLM_CHAT_MAX_INFLIGHT_BY_MODEL`, `GLM_SEARCH_MAX_INFLIGHT`, `GLM_UNKNOWN_MODEL_MAX_INFLIGHT` | Vehicle workers and in-flight request limits (defaults 50; Flash 48/50, FlashX 18/20, GLM-5.3 5/5; search 5; unknown model 1). See [Concurrent batches](#concurrent-batches). |
| `AGENT_NO_NEW_RESEARCH_TURNS` | Finalize after N consecutive turns with no new research artifact (default 2; 0 = off). |
| `AGENT_MAX_TOOL_OUTPUT_CHARS`, `AGENT_KEEP_RECENT_TOOL_RESULTS`, `AGENT_COMPACT_TOOL_OUTPUT_CHARS`, `AGENT_FINALIZER_BUNDLE_MAX_CHARS` | Context limits (see [Research and finalization](#research-and-finalization)). |
| `TOOL_PREVIEW_CHARS`, `TOOL_MAX_TEXT_CHARS`, `TOOL_MAX_LINKS`, `TOOL_MAX_TABLE_ROWS`, `TOOL_MAX_TABLES` | Tool output caps. The full document always stays in the cache. |
| `GLM_BASE_URL` | Default `https://api.z.ai/api/paas/v4`; use `https://open.bigmodel.cn/api/paas/v4` for bigmodel. |
| `GLM_CHAT_PATH` | Chat completions endpoint, relative to the base URL or a full URL (default `chat/completions`). |
| `GLM_SEARCH_PATH` | Web search endpoint, relative to the base URL or a full URL (default `web_search`). |
| `GLM_SEARCH_ENGINE` | Engine name for the standalone search API (default `search-prime`). |
| `GLM_REASONING_EFFORT` | `low` / `medium` / `high` / `max`, sent as the top-level `reasoning_effort` field. Empty = each phase's `GLM_<PHASE>_REASONING_EFFORT`, else the code defaults: research `high`, document sweep / recovery / finalizer `low`. |
| `GLM_THINKING` | Empty = provider default (nothing sent); `enabled` sends `{"thinking": {"type": "enabled"}}`. glm-5.3 models always think and reject `disabled` (HTTP 400, code 1210), so `disabled` is never sent: it maps to no thinking object + reasoning effort `low` (unless an effort is set explicitly), logged once per run as `thinking_disabled_mapped`. A 400 with code 1210 is retried once without a thinking object and with effort `low` (`reasoning_retry`; not counted against max attempts). |
| `GLM_EXTRA_BODY` | JSON merged into every chat request. A `thinking` object of type `disabled` in it is stripped (same mapping as `GLM_THINKING=disabled`). |
| `FINAL_ASSEMBLY` | `deterministic` (default): the final output values are assembled in code from the field states and admitted evidence; a model writes at most `summary` / `research_trace`. `llm`: the finalizer model writes the output (previous behaviour). See [Deterministic Final Assembly](#deterministic-final-assembly). |
| `GLM_PRICE_INPUT_PER_MTOK` / `GLM_PRICE_OUTPUT_PER_MTOK` / `GLM_PRICE_WEB_SEARCH_PER_CALL` | Optional overrides of the built-in price defaults (see [Cost](#cost)). |

`render_page` needs Playwright (`pip install playwright && playwright install chromium`); the Docker image installs
it with Chromium's headless shell (`playwright install --with-deps --only-shell chromium`).
Without it the tool reports `render_unavailable` and the model can use `fetch_url`.

**Render fallback (PR #43).** An HTML `fetch_url` of an official / importer domain that returns 2xx with fewer than 500
visible characters (a JavaScript shell or a bot challenge) is re-fetched once with `render_page` (25 s budget, cached like
a fetch; event `rendered_fallback`). The code decides, never the model. If the rendered page is still empty or a
challenge page, the domain is `unreadable` for the run (event `domain_unreadable`, counter `acq_unreadable_domains`)
and later fetches of it return `domain_unreadable` without a request. No stealth, no anti-bot evasion.

**Bot / challenge shells and render diagnosis (PR #44).** A response with HTTP 247 / 403 / 429 / 503 and a body under
1,000 bytes (audi.co.il and championmotors.co.il answer 247 with a ~584-byte Radware shell), or a short page with a
known challenge marker (Radware / perfdrive, Cloudflare "Just a moment", captcha), marks its domain `unreadable` on the
FIRST such response (event `domain_unreadable` with the reason `http_<status>_shell` / `challenge_marker`, counters
`acq_unreadable_domains`, `acq_challenge_shells`); it is never rendered and no later fetch of the domain is made. At boot
`src/startup_check.py` logs one `playwright boot:` line (Playwright version, Chromium executable, a real headless launch
running a line of JavaScript, ms); every `render_page` call logs `render_page url=… status=… text_chars=… ms=…
outcome=…` to the server log. `tests/test_pr44_playbook.py` renders a local JavaScript page with a real Chromium (CI
installs the headless shell as the Dockerfile does; the test is skipped on a host without a browser).

## Runs, persistence and reconnects

**Before:** the research batch ran on the Streamlit script thread; the page blocked while it rendered. A browser
refresh, a closed tab or a dropped websocket ended that script run, which cancelled the research (persisted as
`interrupted`). Run discovery depended on the page that started the run.

**Now:**

```
browser session(s) ──read──► runs/<run_id>/run_state.json  (lifecycle: QUEUED … COMPLETED/FAILED/…)
        │                    runs/<run_id>/<record_id>/events.jsonl, result.json  (the engine's own artifacts)
        │ start / stop / retry
        ▼
RunManager (src/jobs/manager.py, one per server process) ──► daemon thread per run
        └── research_one / run_batch / finalize_existing_run   (the unchanged research engine)
```

- **A run is a durable object with a stable `run_id`** (the batch id = its folder). `run_state.json` (written
  atomically) holds the lifecycle status, the current pipeline stage, the owning process, a heartbeat, every
  execution attempt (research, finalization retries), a user-safe error summary and the end-of-run report.
- **Progress is reconstructed from the engine's own `events.jsonl`** (`src/runstate/pipeline.py`, read
  incrementally), with the same state machine the live dashboard always used. Refreshing, reopening a tab or
  opening the app on another device shows the same run, phase and counters. `st.session_state` is only a UI cache
  (the form-submission nonce); the URL carries `?run=<run_id>`.
- **Statuses:** `QUEUED`, `STARTING`, `RESEARCHING` (source acquisition), `HARVESTING`, `SWEEPING`, `RECOVERING`
  (tail recovery), `FINALIZING`, `COMPLETED`, `FAILED`, `CANCELLED` (stopped by a user), `INTERRUPTED` (the server
  stopped while it was active).
- **No duplicate execution:** a run executes only on the thread `start` created; a submission is idempotent
  (nonce), a second run for a vehicle with an active run is refused, at most `TRIPY_MAX_ACTIVE_RUNS` run at once,
  and a finalization retry is refused while the run is active. Streamlit reruns only re-read state.
- **Restarts:** a run whose owning process is gone (redeploy, crash) is marked `INTERRUPTED` by the next process
  at startup, using the engine's own `interrupted` / `finalization_pending` results. On a normal stop (SIGTERM),
  active runs are asked to stop at their next safe point and persist their partial result within
  `TRIPY_SHUTDOWN_GRACE_S`.
- **Retry from checkpoint:** a vehicle whose finalization failed, whose finalizer was interrupted
  (`finalization_pending`), or that was interrupted / failed after acquiring sources can be finalized from its
  preserved research with one click. This is the engine's existing `finalize_existing_run` (the CLI's
  `--finalize-existing`): only the finalizer call, with the run's own saved configuration; no search, fetch,
  harvest, sweep or recovery is repeated, and history is preserved. **Resuming in the middle of Document Sweep or
  Tail Recovery is not offered:** those phases keep working state in memory (tool session, novelty tracking, retry
  queue) that the engine does not checkpoint, so a mid-phase resume could not reproduce an uninterrupted run.
  *Restart research* starts a new run of the same target.
- **Storage abstraction:** `src/storage/paths.py` derives every durable path from `TRIPY_DATA_DIR`;
  `src/runstate/repository.py` defines a `RunRepository` interface with a file-backed implementation, so a database
  (and later a separate API / worker tier) can replace it without touching the research engine. Run folders written
  before `run_state.json` existed, or by the CLI, are listed read-only from their files.

**Limits (single-process design):** a research run lives inside the server process. A browser refresh, closed tab,
websocket drop or Streamlit rerun never interrupts it, but a redeploy, restart or crash of the Railway service does:
the run is then shown as `INTERRUPTED`, everything it acquired stays on the volume, and it can be finalized from the
preserved research or restarted. Run one replica only.

## Run profiles and A/B series

Environment variables are the source of DEFAULTS; the configuration of a run is chosen in the UI per run, so an
experiment never needs an env edit or a redeploy (and a stale env value, e.g. `DOCUMENT_SWEEP_MAX_FIELDS=30` left on a
deployment, cannot leak into a profiled run). The **Run profile** selector sits next to *Start research*
(`src/run_profiles.py`):

| Profile | acquisition_mode | document card |
|---|---|---|
| Production (default) | contract | off |
| Benchmark: Baseline | legacy (site map off: it is part of the Treatment) | off |
| Benchmark: Treatment | contract | off |
| Benchmark: Treatment + card | contract | on |
| Custom | the Advanced settings as edited (their defaults come from env) | |

Every named profile also runs: sweep mode `adjudication`, sweep 12 fields / 16 candidates per chunk, final assembly
`deterministic`, reasoning effort research `high` / document sweep `low` / recovery `low` / finalizer `low`, no
thinking object (`GLM_EXTRA_BODY` / the extra request JSON is ignored entirely), and one HTTP attempt per sweep and
recovery request. The importer site map is part of the Treatment: it only acts in contract acquisition and reacquire
recovery, so Baseline has it off and `run_configuration()` records `site_map_used`. A single run of an A/B arm
(Baseline, Treatment, Treatment + card) started outside a series runs with research memory and negative-route blocking
off (no fact reuse or route verdicts from earlier runs); series runs keep their private per-run memory and Production
keeps memory on (`research_memory` / `negative_route_blocking` in `run_configuration()`). A named profile pins every setting listed below under *env overrides* to its code default
(also research turn ceiling 6, hard ceiling 12, minimum base 3 documents / 50 %, sweep packet 28000 chars, 2 sweep
turns, 3 candidates per field, the `ADJUDICATION_*` limits, the `CLUSTER_*` recovery budgets, the recovery turn cap and
the research / recovery HTTP attempts); everything else (models, timeouts, other phases' thinking / max_tokens) still
comes from Advanced settings / env. *Production* with no env set is exactly the
code defaults. The research model is never part of a profile.

**Advanced settings** has the per-run controls the Custom profile uses (acquisition mode, document card, a
*Reasoning effort* select per phase — research, document sweep, recovery, finalizer: provider default / low / medium /
high / max, defaulting to env, else the code defaults —, document sweep max attempts 1-3 / max fields / max candidates
per chunk). The *Extra request JSON* field warns when it contains `thinking.type = disabled` (stripped). Its phase
settings are merged per phase and per key over the env phase settings (another phase's env setting such as
`GLM_RECOVERY_THINKING` survives). It also lists every env variable that differs from its code default as
`VAR = env value (code default X)` for `ACQUISITION_MODE`, `ACQUISITION_DOCUMENT_CARD`, `DOCUMENT_SWEEP_MAX_FIELDS`,
`DOCUMENT_SWEEP_MAX_CANDIDATES`, `DOCUMENT_SWEEP_MAX_PACKET_CHARS`, `DOCUMENT_SWEEP_MAX_TURNS`,
`DOCUMENT_SWEEP_CANDIDATES_PER_FIELD`, `SWEEP_MODE`, `FINAL_ASSEMBLY`, `GLM_REASONING_EFFORT`,
`GLM_<PHASE>_REASONING_EFFORT` (research, document sweep, recovery, finalizer), `GLM_THINKING`,
`GLM_DOCUMENT_SWEEP_THINKING`, `GLM_EXTRA_BODY` (whenever it is a non-empty object),
`GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS`, `GLM_RESEARCH_MAX_ATTEMPTS`, `GLM_RECOVERY_MAX_ATTEMPTS`, the `ADJUDICATION_*`
limits, `PRIMARY_RESEARCH_MAX_TURNS`, `PRIMARY_RESEARCH_HARD_MAX_TURNS`, `PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS`,
`PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE`, `SITE_MAP`, `GROUNDED_CANDIDATES`, `RECOVERY_MODE`,
`CLUSTER_MAX_ATTEMPTS`, `CLUSTER_BASE_TURNS`, `CLUSTER_MAX_TURNS`, `CLUSTER_SEARCH_BUDGET`,
`FIELD_RECOVERY_MAX_TOTAL_STEPS` (the same list is logged as `env_overrides` in `run_started` and result.json).

Every run records its effective configuration: `run_profile`, `acquisition_mode`, `acquisition_document_card`,
`research_prompt_hash` (of the research system prompt actually sent) and `env_overrides` in `run_started` /
result.json, the full AgentConfig in batch.json (so a finalization retry uses the same configuration), and
`run_profile` / `document_card` in `diagnostics.run_configuration()` and `config_key()` (`by_config` separates
Treatment from Treatment + card; `env_overrides` is informational and not part of the key).

**Benchmark A/B** (a target mode next to One vehicle / Manufacturer / All 50): pick vehicles, *Runs per arm* (1-5,
default 3) and the arms (Baseline and Treatment checked by default). One click creates a SERIES: for each repeat, for
each arm, one run over the selected vehicles with that arm's profile, arms interleaved (B, T, B, T, ...) so provider
drift hits both arms equally. The runs execute strictly one after another; a vehicle in another active run makes the
series wait. Sequencing lives in the job layer (`RunManager.start_series`, state in
`runs/_series/<series_id>/series.json`), so it survives a page refresh; *Cancel series* stops the running run and
every planned one. When the series ends, `diagnostics.write_benchmark` runs over exactly the series' run_ids into
that folder (`benchmark.json`, `per_vehicle.csv/.jsonl`, `parser_gaps.jsonl`), offered under *Benchmark diagnostics*.
**Restart policy:** a series cannot outlive its server process. After a restart it is marked INTERRUPTED (unstarted
runs NOT_STARTED, the interrupted run reconciled like any run) and its benchmark is written over the runs it
finished; it is never resumed automatically, because a restart is usually a redeploy and resuming would mix code /
prompt versions inside one comparison. Start a new series instead.

**Document card** (`Benchmark: Treatment + card`, or `ACQUISITION_DOCUMENT_CARD=on` / Custom): in contract research a
successful `fetch_url` / `fetch_pdf` result carries
`{"note": "server-computed scheduling metadata, not evidence", variant_match, binding_level, market,
target_market_document, source_authority, usable, clusters: {recovery_cluster: in-scope fields with a harvested
candidate}, candidate_fields_in_scope}` (in scope = the scoped-coverage rule: any market for `market_sensitivity:
low`, else a target-market document). It is built after the harvester ran for the document; a failure logs
`document_card_failed` and returns the result without a card; a replay outside research drops it.

## Acquisition and document-sweep diagnostics

Observational telemetry for benchmarking where performance is lost: search / source acquisition, source selection,
deterministic extraction, document-sweep reasoning, latency / timeouts or orchestration. **It never changes research
behaviour:** no model escalation, no routing, no threshold changes. The engine only adds fields to two existing events
(`primary_research_turn.state_before/state_after`; `document_sweep_started.packet_document_ids /
candidates_per_field / estimated_input_tokens`); everything else is rebuilt from `events.jsonl` by
`src/diagnostics.py`.

**Where it is stored** (durable, under `TRIPY_DATA_DIR`, i.e. the Railway Volume):

```
runs/<run_id>/<record_id>/diagnostics.json    schema tripy-diagnostics/1: acquisition {summary, turns[]},
                                               document_sweep {summary, calls[]}, summary_text, configured models
runs/<run_id>/<record_id>/diagnostics.jsonl   one line per acquisition turn and per sweep call
runs/<run_id>/diagnostics/                    benchmark.json, per_vehicle.jsonl / .csv, parser_gaps.jsonl for the run
runs/_series/<series_id>/                     series.json + the same benchmark files over exactly the series' runs
benchmarks/<UTC stamp>/                       python -m src.diagnostics [--runs-dir …] [run_id …] [--rebuild]
```

**Parser gaps** (`src/parser_gaps.py`, every mode, observational only): right after the deterministic harvest each
run logs one `parser_gaps` event: for every usable cached document (not bound to another variant) and every applicable
field with a dictionary entry, a row `{field, document_id, source_url, source_authority, market, matched_alias, offset,
snippet}` when the field's label occurs in the document (`document_inspection`) but the cached harvest has no
candidate for it from that document (at most 200 rows per run, `truncated` counts the rest; zero model calls, zero
network; a failure logs `parser_gaps_failed` and the run continues). Diagnostics join it with the sweep: per field
`{label_hits_no_value, recovered_by_sweep}` (`candidate_missed_by_deterministic_harvest` on one of that field's gap
documents), per-vehicle `parser_gap_rows / parser_gap_fields / parser_gap_recovered_by_sweep`, and the benchmark's
`parser_gaps_by_field` backlog (rows, runs with a gap, recovered by the sweep, top-3 aliases; most widespread first)
plus `parser_gaps.jsonl` (one row per line with `run_id`, `record_id`).

They are written on every exit path of a vehicle run (also interrupted ones) and can be rebuilt from `events.jsonl`
at any time (`--rebuild`). The compact summary is also logged to stdout. Secrets are redacted before writing.

- **Acquisition turn:** configured model vs provider-reported (`resolved_model`; null when the provider returns
  none), latency, tokens, attempts / retries / timeouts; search queries, result count, result URLs / domains with
  source class, official and target-market flags, whether a result was later fetched (`selected`), repeated queries
  / URLs, billable search requests; fetches attempted / succeeded / failed by category (`http_forbidden`,
  `http_not_found`, `http_rate_limited`, `http_server_error`, `timeout`, `connection`, `empty_content`, …); the
  acquisition state before and after the turn (useful, official and target-market documents, candidates, candidate
  fields, scoped coverage, admitted evidence) with deltas; the acquisition artifacts, no-artifact streak, base-gate
  status, a stop the gate deferred, extension beyond the normal budget and the stop reason, exactly as the existing
  policy recorded them. The final turn that answers without tools is listed but not measured by the policy.
- **Document-sweep call** (one packet / chunk, up to the configured model turns): models, latency, timeout,
  retries, tokens; packet fields, documents, characters, estimated tokens and candidates per field; per field the
  state before / after, whether this call resolved it, candidates, admitted evidence before, whether local
  inspection located it, evidence admitted / rejected (with rejection reasons) and malformed tool output; malformed
  final reply; success.
- **`DETERMINISTIC_HARVEST_MISS`** is assigned to a sweep evidence item only when the field was resolved by that
  call, the evidence was admitted and bound to the target, its document was in the run before the sweep, and the
  deterministic harvest produced **no** candidate for that field from that document. A candidate with another value
  is `DETERMINISTIC_VALUE_MISMATCH` (precision / normalization, not recall), the same value `PROMOTED_CANDIDATE`,
  anything unprovable `UNCLASSIFIED`. The pre-existing `document_sweep_deterministic_misses_found` (misses and
  mismatches together) is reported alongside.
- **UI:** *Technical details* shows concise Source acquisition and Document sweep blocks and a *Detailed
  diagnostics* expander (summary, per-turn table, per-call table, per-field table, raw JSON). The sidebar's
  *Benchmark diagnostics* aggregates chosen runs and downloads `benchmark.json` / `per_vehicle.csv`.
- **Benchmark aggregate:** acquisition turns (mean / median), searches, useful / official / target-market
  documents, scoped coverage after each turn, coverage gain per turn and per search, no-artifact turns, repeated
  searches, hard-max rate, latency; sweep calls, fields entering / resolved, resolution rate, harvest misses
  recovered, evidence rejection rate, tokens, latency, timeout rate, resolved fields per model call; plus every
  vehicle's row.
- **End to end** (also for old runs: rebuilt from `events.jsonl`): recovery (model calls, billable searches,
  fetches, attempts, fields resolved), the final field states (`current_evaluation()` over the final events: ok,
  conflicting, unresolved / missing, foreign_market_only, variant_not_exact, not_applicable, plus the ok fields),
  totals per phase (model calls, input / output tokens), cost (the run's own `result.json` cost), wall time, the
  first turn that met the minimum acquisition base and the research tokens spent until then, research
  `tool_blocked` calls, and `official_urls_discovered`. Interrupted / incomplete runs are flagged and reported
  under `interrupted`, never averaged into `end_to_end`. `by_config` repeats the statistics per configuration
  (acquisition mode, research / sweep model, sweep thinking, sweep max attempts, sweep limits, the reasoning effort
  of every phase, final assembly), so a legacy and a contract arm compare from one `benchmark.json`. Totals also sum
  provider-reported reasoning tokens per phase (`usage.completion_tokens_details.reasoning_tokens`, null when not
  reported).
- **Official documents:** newer runs record official documents that were *fetched* (`official_documents`, UI
  "Official docs (fetched)") separately from official URLs merely *discovered* in search results
  (`official_urls_discovered`, UI "Official URLs (discovered)"); an older run's single number keeps its old label.

## Z.ai API: confirmed and unverified

Confirmed against the official Z.ai documentation (2026-10-01) and used as defaults:

| Item | Value |
| --- | --- |
| Base URL | `https://api.z.ai/api/paas/v4` |
| Chat endpoint | `POST /chat/completions` |
| Standalone search endpoint | `POST /web_search` |
| Standalone search engine | `search-prime` |
| Search request fields | `search_engine`, `search_query`, `count` (1–50), `search_domain_filter`, `search_recency_filter` |
| Search response | `search_result[]` with `title`, `content`, `link`, `media`, `icon`, `refer`, `publish_date` (all kept) |
| Tools | OpenAI-style function tools; `tool_choice: "auto"`; `function.arguments` is a JSON-format string |
| Tool results | Sent back as `role: "tool"` messages with `tool_call_id` |
| Reasoning | Responses expose `reasoning_content` (logged per model turn) |
| Usage | `prompt_tokens`, `completion_tokens`, `total_tokens`, and cached-token details (`prompt_tokens_details.cached_tokens` is summed) |

Still unverified until the first real run:

- The thinking request shape `{"thinking": {"type": "enabled"}}`. Confirmed in production: glm-5.3 models reject
  `{"type": "disabled"}` with HTTP 400 code 1210 ("always engages in thinking ... please use low, high, or max"), so
  it is never sent; the control is the top-level `reasoning_effort`.
- The `search_recency_filter` value sent by default (`noLimit`).
- That the cached-token detail is reported as `prompt_tokens_details.cached_tokens`.
- Error bodies, status codes and rate-limit behaviour. Retries cover timeouts, connection errors and
  500/502/503/504 within the configured attempt limits, and every raw error is stored. An HTTP 429 (PR #43) halves the
  shared, process-wide concurrency of that model's pool, waits out `Retry-After` (else an exponential backoff with
  jitter) and is retried without spending an attempt while 300 s of rate-limit waiting remain; the limit recovers by
  one slot every 15 s once 30 s passed without a 429 (defaults in `src/concurrency.py`). Counted per run as
  `rate_limited_retries` / `rate_limited_failures` (`api_stats`, the benchmark row).
- Whether a response always echoes `tool_calls` exactly as we send them back. Assistant messages are echoed with
  `content` and `tool_calls` only.

**Not used: Z.ai Web Reader.** The experiment tests our own fetch / render / extract / PDF / document tools, so
page reading is never delegated back to Z.ai.

## Research and finalization

The GLM-5.3 baseline (batch `20261001T185509Z-glm-5.3-one`, vehicle #44) showed that research works but
re-sending the whole 30-step conversation for the final answer does not: the final call timed out
repeatedly and no result was saved. Each vehicle run now has two explicit phases:

1. **Research phase** (`GLM_MODEL`). Source acquisition: tool-calling turns up to
   `PRIMARY_RESEARCH_MAX_TURNS` (default 6), ended earlier by the deterministic no-artifact stop (see
   [Primary research is source acquisition](#primary-research-is-source-acquisition)). Fetch tools store the full
   document in the cache and return only a `document_id`, metadata and a short preview. Older tool results
   in the conversation are compacted to stubs that keep `document_id`, URL, title, query and result URLs.
   Repeated searches, re-fetches and identical document queries are noticed and pointed out to the
   model. Research stops with a recorded `stop_reason`: `model_finished`, `max_steps`, `no_new_artifact`
   (`PRIMARY_RESEARCH_NO_ARTIFACT_STOP` turns in a row that acquired nothing), `acquisition_sufficient` (optional),
   `no_new_research` (`AGENT_NO_NEW_RESEARCH_TURNS` turns with no new document, evidence, source or document
   query), `user_cancelled`, `api_failure` or `research_exception`.
2. **Finalization phase** (`GLM_FINALIZER_MODEL`, default `GLM_MODEL`). If the research model did not
   already return valid JSON, ONE no-tools call receives a compact research bundle (`src/bundle.py`):
   the Level 1.5 identity, the targets, every stored evidence item (with market/variant), candidate facts
   grouped by field, fields with several stored values (no winner picked), conflicts the model noted,
   document metadata, the document excerpts the model actually read, a concise action list, targets with
   no evidence and the last model notes. It is capped at `AGENT_FINALIZER_BUNDLE_MAX_CHARS`. The raw
   conversation is never sent. The exact request is saved as `finalizer_request.json`.

Statuses: `completed`, `max_steps_finalized`, `no_new_research_finalized` (also after `no_new_artifact`),
`acquisition_sufficient_finalized`, `completed_unparsed`,
`finalization_failed`, `research_failed`, `interrupted`, `recovered_finalized`, `finalization_pending`
(the durable checkpoint, see below), plus `incomplete` for runs reconstructed from events.

### Layered field harvesting

The full per-vehicle pipeline is now:

```
PRIMARY RESEARCH (source acquisition)      model turns, tools; deterministic no-artifact stop
DETERMINISTIC HARVEST                      0 model calls: every document -> candidates for ALL fields
LOCAL-FIRST PRE-SWEEP                      0 model calls: candidate routing, batch inspection of cached documents for
                                           open fields without candidates, current_evaluation(), settled fields removed
ADAPTIVE DOCUMENT SWEEP                    1 call per chunk (1 chunk unless the packet exceeds its limits; chunks by
                                           recovery_cluster); a 2nd turn per chunk ONLY to read turn-1 cached
                                           inspections; cached tools only
current_evaluation()                       the one shared field evaluator
TARGETED WEB RECOVERY                      only fields still unresolved, breadth-first
current_evaluation()
PRE-FINALIZATION CHECKPOINT                result.json status=finalization_pending
FINALIZER
```

- **Candidates are not evidence.** `src/candidate_harvest.py` reads every document a run touches (in any
  phase, including documents fetched during recovery) once, and lists label/value candidates for every
  requested field that has dictionary metadata. A candidate carries `value`, `raw_value`, `unit`,
  `document_id`, `source_url`, `quote`, `matched_alias`, `extraction_method`, `market_hint`,
  `variant_hint`, `position`, `table_index`/`row_index` and `parser_confidence` (how sure the parser is
  of the label/value pairing, never that the value belongs to the requested variant). Candidates never
  change a field state, never resolve a conflict and are never majority-voted: only `store_evidence`
  does that.
- **Strategies**: structured key/value (HTML tables, `<dl>`, PDF tables, JSON-LD / page state), alias +
  nearby value + unit within one clause, known textual forms (`9-speed automatic`, `255/45 R20`,
  `10%-80% in 33 minutes`, `5 years / 150,000 km`), and equipment booleans only when the feature is
  syntactically tied to a value or equipment context (a bare menu mention is not a candidate).
  Reversed-Hebrew PDF lines are repaired only when detection is deterministic (a word starting with a
  final letter form, or a known Hebrew label appearing only after reversal).
- **Field dictionary**: `data/enrichment_fields.json` holds, per field, `display_name_he`, Hebrew and
  English aliases, units and unit variants, exact conversion rules (`Wh/km ÷ 10`, `m × 1000`, ...),
  positive / negative context terms, exclusion and ambiguity rules, enum values, patterns and broad
  plausibility ranges (parser sanity only). Shared vocabularies (affirmative / negative cell values,
  axle and warranty-type words, currencies) live in `harvest_vocabulary`. Python provides generic
  matchers only (`numeric`, `boolean`, `enum`, `gearbox`, `tire_size`, `charging_time`,
  `charging_window`, `warranty`, `price`, `text`); adding or changing an alias is a JSON edit.
- **Candidate cache**: `documents/<id>/derived_field_candidates_<schema_hash>.json` in the shared cache.
  Another vehicle (or worker) reusing the document and dictionary reuses the harvest; it is computed
  once even under concurrent requests.
- **Document sweep** (`src/document_sweep.py`): compact packets for the OPEN fields only (identity, current
  states, each field's best-routed candidates with quotes and hints, `local_snippets` for fields without
  candidates, `fields_not_found_locally`, the open fields' stored evidence, cached-document identity with
  authority / market / binding). See [Local-first adaptive document sweep](#local-first-adaptive-document-sweep).
  Only `inspect_document_for_fields`, `find_in_document`, `extract_tables`, `extract_html`, `get_structured_data`,
  `get_cached_document`, `store_evidence` and `report_field_status` are offered; any other call is
  refused without being executed (`tool_blocked`), so the stage makes zero searches and zero fetches.
  Evidence the sweep stores for a document/value with no matching candidate is logged as
  `candidate_missed_by_deterministic_harvest` (feedback for the dictionary). The sweep runs only when
  field recovery is enabled, at least one field is unresolved and the run has cached documents.
- **Recovery packets** now include the field's `deterministic_candidates`.
- Metrics (observational, never accuracy): `documents_harvested`, `candidate_count_total`,
  `candidate_fields_total`, `candidate_field_coverage_pct`, `candidate_cache_hits/misses`,
  `document_sweep_model_calls`, `document_sweep_fields_promoted_to_evidence`,
  `document_sweep_fields_resolved`, `document_sweep_deterministic_misses_found`,
  `candidate_precision_reviewed` (share of presented candidates promoted), `fields_unresolved_before_harvest`,
  `fields_unresolved_after_harvest_review`, `fields_entering_web_recovery`, and model calls per stage
  (`primary_model_calls`, `document_sweep_model_calls`, `field_recovery_model_calls`,
  `finalizer_model_calls`) next to `search_api_calls`.
- **Adaptive turn budget** (absolute maximum 2). A sweep that can promote or reject the candidates it was
  given ends after ONE model turn. Only when turn 1 called a cached-document inspection tool
  (`find_in_document`, `extract_tables`, `extract_html`, `get_structured_data`, `get_cached_document`)
  that returned content (hits, tables, data or text; not an error or zero hits) does the model get ONE
  follow-up turn to read those results and store evidence (`document_sweep_follow_up` event). This is
  what lets the sweep store a fact the parser missed: a tool result is only visible on the next turn.
  `DOCUMENT_SWEEP_MAX_TURNS=1` never grants the follow-up.
- `LAYERED_HARVEST_ENABLED=false` restores the previous pipeline; `DOCUMENT_SWEEP_MAX_TURNS=0` keeps the
  zero-cost harvest but skips the sweep.

### Primary research is source acquisition

A real one-vehicle GLM-5.3-Flash run (Toyota COROLLA 2024 BUSINESS EDI) spent 12 research turns, 6 searches and 6
documents, much of it as a model-driven Ctrl+F over documents it already had (`find_in_document("Maximum torque")`,
`("Torque")`, `("Fuel tank")`, `("Kerb Weight")`, `("Apple CarPlay")`, `("Tyres")`, ...), and only then reached a
document sweep of 37 fields / 66 candidates whose single model call hit a 240 s ReadTimeout. Primary research now
acquires sources; deterministic code reads them (`src/acquisition.py`):

- The research prompt asks for a compact, high-value source set (official importer, official manufacturer, official
  technical PDFs, official media, then publishers, aggregators, marketplaces / communities; target-market sources
  first for price, licence fee, trim and warranty) and states that every fetched document is harvested for ALL
  fields by code. Field-by-field `find_in_document` over a document already fetched is not the strategy; one
  `inspect_document_for_fields` call checks many fields at once. Search results in the research phase carry
  `acquisition_priority` (`priority_technical`, `priority_commercial`, `source_class`, `market`): a deterministic,
  inspectable order, never an allowlist and never an admission rule.
- **Acquisition artifacts** of a turn: a new usable document (2xx with content, by URL, not server-side bound to
  another variant), a new official source (retrieved, or returned by a search), a new target-market document, a new
  candidate for an open applicable field from a usable document, newly admitted evidence (not `different` /
  `unbound`) or a better best binding. NOT artifacts: re-reading a cached document, a repeated (or replayed) search,
  403 / 404 / 429 and other failed calls, commentary, rejected evidence, candidates of another variant.
- **No-artifact stop**: `PRIMARY_RESEARCH_NO_ARTIFACT_STOP` (default 2) consecutive turns without an artifact end
  research (`stop_reason=no_new_artifact`) before the `PRIMARY_RESEARCH_MAX_TURNS` ceiling (default 6), once the
  minimum acquisition base is met. The model is told after each empty turn how many such turns remain.
- **Minimum acquisition base (fail-safe stop)**: see below. Every scheduler stop above needs it.
- **Acquisition sufficiency** (optional, off by default): at least `PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS` useful
  documents AND at least `PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD` % of the applicable fields covered (a
  candidate from a useful document, or already settled) end research (`acquisition_sufficient`).
- All of it is scheduling: nothing here creates evidence or changes a field state, binding, market or conflict.
- Telemetry (`result.json` `primary_research`, metrics `primary_research_*`): turns, model calls, stop reason
  (`model_finished`, `max_turns`, `no_new_artifact`, `acquisition_sufficient`, `no_new_research`, `user_cancelled`,
  `error`), documents added, useful / target-market / other-variant documents, official sources, candidate fields,
  candidate coverage, no-artifact turns, and the artifacts of every turn (`primary_research_turn` events).

### Minimum acquisition base (fail-safe stop)

A scheduling optimization must not cost reliable coverage because a research model does not follow the new prompt.
The offline orchestration benchmark showed it: a model that keeps interleaving per-field Ctrl+F turns (as the real
GLM run did) was stopped after two empty turns with 2 of 6 documents, and trustworthy fields fell from 15 to 11. So a
no-artifact streak is not enough to stop on its own:

```
if a scheduler stop fires (no-artifact streak, acquisition sufficiency, AGENT_NO_NEW_RESEARCH_TURNS, or the normal
PRIMARY_RESEARCH_MAX_TURNS ceiling):
    if minimum acquisition base met:  stop
    else:                             log primary_research_stop_deferred, tell the model the source set is still thin
                                      and to acquire NEW sources, continue (up to PRIMARY_RESEARCH_HARD_MAX_TURNS)
```

The base (`acquisition.minimum_base`) is met when the run holds at least `PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS` (3)
useful documents AND at least `PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE` (50) % of the applicable fields have a
candidate from a source in their market scope (any useful document for a `market_sensitivity: low` field, a
target-market document otherwise) or are already ok. In-scope coverage, not raw candidate coverage: in the benchmark
raw candidate fields plateau (26 of 37) while trustworthy fields still wait for an Israeli source for the
market-sensitive dimensions. It reads documents, candidates and the schema only: never model confidence, a vote,
historical yield, an expected answer or Golden Set truth, and it never changes evidence, a field state, a binding,
N/A or a conflict. A run that acquires normally stops exactly as before (6 turns); only an under-acquired run may go
beyond the normal ceiling, and it stops as soon as the base is met (or at the hard ceiling,
`stop_reason=hard_max_turns_under_acquired`). `model_finished` (the model's own final answer) is not a scheduler stop:
in `legacy` it is never held back. In `contract` an early `{"done": true}` cannot bypass the base: before or at the
normal ceiling with the base unmet, the FIRST "done" is deferred once (`primary_research_stop_deferred`,
`wanted_stop="model_finished"`) and the next request carries a user `[operational note]` with the base numbers and the
missing source categories; a second consecutive "done", a "done" on an extension turn (the futility rule's signal) or
the hard ceiling stop research at once. The deferred turn counts as a research turn and toward the ceiling, runs no
`after_turn` (the no-artifact streak is unchanged), and is counted as `primary_research.done_deferred_count`
(diagnostics `acquisition.summary.done_deferred_count`, per-vehicle row `acq_done_deferred`).

Defaults come from the benchmark's sensitivity sweep (policy that ignores the new prompt / follows it): 3 documents
with 40-45 % let the regression back (13 trustworthy fields); 50 % is the smallest threshold that keeps 15 with 10
research calls; 55-60 % also keep 15 but run to the hard ceiling (12 calls) and add a turn when the prompt is followed;
the document count alone (3 / 0 %) does not protect. The fixture sits close to the 50 % edge (49 % -> 54 % when the
Israeli source arrives), so a live run should re-check it.

Telemetry: `primary_research.minimum_acquisition_met`, `minimum_acquisition` (documents, in-scope coverage,
thresholds), `stop_deferred_count`, `stops_deferred`, `under_acquired_turns`, `extended_turns`,
`scoped_coverage_pct`; metrics `primary_research_minimum_acquisition_met`, `primary_research_stop_deferred_count`,
`primary_research_under_acquired_turns`, `primary_research_extended_turns`, `primary_research_scoped_coverage_pct`;
events `primary_research_stop_deferred` and `primary_research_turn.minimum_acquisition_met`.

### Deterministic batch document inspection

`src/document_inspection.py` (tool `inspect_document_for_fields(document_id, fields)`) inspects ONE cached document
for MANY fields in one local operation: the field dictionary's aliases (the same compiled rules as the harvest)
locate each field's labels; overlapping windows of a field merge into one snippet (the label's line and the next
one, bounded by `context_chars`); every snippet keeps `offset` / `end` / `label_offset` in the cached text
(`text[offset:end] == snippet`). The field's harvested candidates come with it (quote, table / row / page and, where
the quote occurs verbatim, its offset). It makes zero model, API or network calls, creates no evidence, decides no
variant or market and never calls `current_evaluation()`. Exact repeats are replayed like the other read-only tools.

### Local-first adaptive document sweep

Before any paid sweep call (`run_document_sweep` in `src/agent.py`):

1. every document is already harvested for all fields (each phase's session hook, then the harvest step);
2. candidates are **routed** per field (`routing_score` in `src/document_sweep.py`): official authority, server-side
   binding of the source document (another variant ranks last), target market (more for market-sensitive fields),
   same-row / same-cell pairing, parser confidence, alias specificity, an official PDF, the target trim named next
   to it, a differing year hint. Distinct values come first, so competing values are both in front of the model.
   It answers only "which candidate should the model inspect first?"; agreement between sources, occurrences, model
   confidence and historical yield are never used, and nothing about evidence, state, binding, market or conflict
   changes;
3. open fields WITHOUT candidates get a deterministic batch inspection of every usable cached document (event
   `document_inspection`, 0 model calls): label snippets go into the packet as `local_snippets`; fields with no
   label anywhere are listed as `fields_not_found_locally` (do not search the cache again for them);
4. `current_evaluation()` runs again and settled fields are removed (`document_sweep_plan.removed_settled`);
5. the packet carries the best `DOCUMENT_SWEEP_CANDIDATES_PER_FIELD` (default 3) candidates per field
   (`candidates_not_shown` counts the rest; storage keeps every candidate and the local tools reach them);
6. while the packet fits `DOCUMENT_SWEEP_MAX_PACKET_CHARS` / `_MAX_FIELDS` / `_MAX_CANDIDATES` (28000 / 12 / 16) it
   is ONE call; otherwise `plan_chunks` packs the schema's `recovery_cluster`s, in schema order, into as few chunks as
   fit (a cluster that alone exceeds the limits is split by fields). Every open field is in exactly one chunk; fields
   settled by an earlier chunk are dropped before the next one. A `GLMError` still ends the sweep (retry semantics
   unchanged) and recovery still runs.

Telemetry (`result.json` `document_sweep`, metrics `document_sweep_*`): calls, chunks, packet chars, estimated input
tokens ((system prompt + packet) / 4, before the call), fields, candidates, documents, latency (wall and model),
timeouts, input / output tokens and per chunk: index, clusters, fields, candidates, packet chars, calls, latency,
tokens; plus the pre-sweep inspection summary. The timeout itself is not raised by default: the fix is a smaller
packet, not a longer wait.

### Candidate adjudication sweep (default)

`SWEEP_MODE=adjudication` (`src/adjudication.py`). The legacy sweep made the model re-type every valid candidate as a
full `store_evidence` call inside a 2-turn tool loop; it is output-bound (production chunks of 30 fields hit the 240 s
read timeout) and the re-typing itself caused admission rejections. Now code does the mechanical work and the model
only judges:

- **Usable documents only.** Candidates harvested from a non-2xx, empty or failed document are never swept,
  presented, counted as fresh or triaged in recovery (`tail_planner.usable_candidate_matrix`, both sweep modes); the
  harvest totals stay unfiltered and `candidates_from_unusable_documents` counts what was dropped.
- **Grouping.** Per open field: routed candidates, grouped by value, at most 2 per distinct value, packet-local ids.
- **Admission dry run.** `admit()` on the request the candidate itself makes (value, unit, document, quote); nothing is
  stored. A mechanical rejection (`field_label_not_in_quote`, `value_not_stated`, `quote_not_in_source`) gets ONE
  widened verbatim quote of the same place (a table candidate's header line + row, else the quote's line, else ±160
  characters) and one more dry run. Still rejected: never shown, logged once in `adjudication_not_admissible`, and
  treated like a rejected key by recovery (never fresh again).
- **Classes.** U = one admissible value bound `exact` and no harvester hint (`trim_mentioned`, `variant_hints`,
  `year_hint_differs`, ambiguity-rule hints); A = any other field with an admissible candidate; M = nothing admissible
  but the pre-sweep inspection located label snippets; anything else goes straight to recovery.
- **Packets** per recovery cluster (schema order), NO tools, JSON only: U `{"decisions": [{id, accept, reason}]}`,
  A `{"fields": [{field, accept: [ids], reject: [{id, reason}]}]}` (each candidate with ≤700 chars of context),
  M `{"fields": [{field, value, unit, snippet, span}]}`. One repair turn for an unparseable reply. Unknown ids,
  out-of-range spans and fields not in the packet are ignored and logged (`adjudication_invalid_decision`).
- **Storage** through the normal tool path: accepted candidates (their own value / unit / document / admissible
  quote; `variant_match: exact` only for reason `ok`) and M statements (`snippet[start:end]`, verified verbatim in the
  cached document) become synthetic `store_evidence` calls in `ToolSession.execute`. Admission decides.
- **Failures.** `candidates_presented` (`source: adjudication`) is logged only for a packet whose call returned and
  parsed. A failed packet (timeout, API error, unparseable after repair) presents nothing; its fields stay open and
  their candidates fresh for recovery's local pass; the next packet still runs.
- The phase stays `document_sweep` (usage, pricing, `GLM_DOCUMENT_SWEEP_*` thinking / timeout / max attempts). The
  `DOCUMENT_SWEEP_*` chunk limits do not apply; `DOCUMENT_SWEEP_MAX_TURNS=0` still skips the sweep. The summary keeps
  the legacy keys and adds `sweep_mode`, `adjudication` {classes, packets per class, decisions, accepted, admitted,
  rejected_by_admission, dry_run stats} and per-packet latency / tokens in `document_sweep_chunk_details`.

### Phase-specific model settings

`src/phase_settings.py`: `research`, `document_sweep`, `recovery` and `finalizer` may each set `model`,
`reasoning_effort`, `thinking`, `max_tokens`, `temperature`, `timeout_s` (read timeout per HTTP attempt) and
`max_attempts` through `AgentConfig.phase_settings` or `GLM_<PHASE>_*` (see the settings table). Unset values inherit
the global configuration, then the built-in phase defaults (`PHASE_DEFAULTS`): reasoning effort research `high`,
document sweep / recovery / finalizer `low`; max attempts 1 for document sweep and recovery (a timed-out request is
not re-sent identically). The research model stays `GLM_MODEL` and the
finalizer model `GLM_FINALIZER_MODEL`; a sweep / recovery phase on its own model is priced with that model's prices
(`cost_details.<phase>_tokens_usd`). The effective settings of every phase are recorded in
`glm_config.phase_settings`.

### Evidence admission, variant binding and source authority

The first real Toyota Corolla Touring Sports 1.8 Hybrid run stored a cargo volume that belongs to the 2.0
Hybrid as `variant_match=exact`, a gear count of 1 inferred from "e-CVT", label-only booleans as `true`, and a
model note that blamed a value on the wrong source; every one of its 34 evidence items claimed `exact`. Evidence
is therefore admitted by deterministic checks, and identity is computed by the server:

```
store_evidence request
  → provenance      the document is in the store (document_id, or a URL this run / the cache retrieved);
                    a search snippet or memory is never a source                    → source_not_retrieved
  → applicability   the field applies to this propulsion                            → field_not_applicable_for_vehicle
  → quote           occurs in the document (normalized; whole numbers stay whole, so "150 mm" is not in
                    "4,150 mm"; "…" joins fragments in order within one short passage; a quote the parser
                    cut from the same document also counts); with no quote, a deterministic candidate of
                    the same document/field/value supplies it                       → quote_missing / _too_short / _not_in_source
  → entailment      ONE fragment states the value FOR THIS FIELD: the field's own dictionary parse, or the
                    number literally (written numbers such as "שלוש" only with the field's unit) or by a
                    schema conversion (146.0 ס"מ → 1460 mm), with the field's label in the value's clause
                    or the parser's own pairing of field and value in this document, and never a number
                    the quote's parse gives to another field. Booleans need the feature's label with its
                    availability right after it (יש / אין / ✓ / standard / not available), a negation right
                    before it, or "includes …"; never another feature's "yes"     → unsupported_inference /
                                                                                      value_not_in_quote / value_not_stated /
                                                                                      field_label_not_in_quote /
                                                                                      value_belongs_to_other_field
  → semantics       unit (a USD price stays USD), plausible range, the field's semantic exclusions for this
                    propulsion in the value's own words (its clause up to the next number, its bracket group)
                    in the quote AND in its source line, and in the section heading above it ("Electric
                    motor" / "Max. torque 185 Nm" is not a hybrid's engine torque)  → unit_* / implausible_value /
                                                                                      semantic_mismatch
  → identity        binding level + variant_match (src/document_binding.py), market from the source
  → authority       source_authority (src/source_authority.py, data/source_rules.json)
  → typing / time   typed_value, condition only if quoted, valid_as_of only from the source
  → EvidenceStore   (the store itself refuses any payload that did not pass admission)
```

**Server-side variant binding.** The target identity comes from the Level 1.5 record (manufacturer, model
family, model year, body, propulsion, engine displacement in cc, power, drivetrain, model code, trim, market).
Each document gets a profile: its identity zone (title, URL, H1 headings or the first lines, keeping only the
list segments that name the target model family) decides; the full text only confirms, so a navigation menu
listing hybrids or SUVs never vetoes a page (another displacement / power / drivetrain in the full text only
leaves the dimension unresolved). Each fact is bound through its own context, most specific first: the value's own
words, the table column header of the value, the quote fragment that states it, its source line (other numbers'
bracket groups removed; a value found only in page source keeps the surrounding source), the section heading above
it; the quote's other "…" fragments can only veto (years inside a fact are never read as model years). The trim counts only when the identity zone names it; another trim vetoes `exact`
for trim-sensitive fields (price, equipment): a column header with words beyond technical terms and the target
trim's own words ("Premium", "Business Plus"), a fact naming another trim ("the Premium version"), or the target
trim named to exclude it ("not available on Business"). Levels: `unknown < model_family < generation < body_powertrain < exact_technical_variant
< exact_market_trim`. An explicit contradiction vetoes exact binding (another displacement, body, propulsion,
drivetrain, or the system power of a BEV / combustion car; a longer model family such as "Corolla Cross" for a
"Corolla"), even when manufacturer, model, body and year all match. A multi-variant page (1.8 AND 2.0 Hybrid)
is never exact by itself: the fact's context must name the target's technical variant. `variant_match` becomes
`different` (veto, or the model's own "different"), `unbound` (the source does not even name the model family),
`exact` (level ≥ the field's `binding_requirement`) or `unclear`. The evaluator treats `different` and
`unbound` as not about the target, and `unclear` as not target-safe: a value is usable only when its
server-computed `binding_level` reaches the field's `binding_requirement` (an `exact_market_trim` field whose
evidence binds only at `exact_technical_variant` stays `variant_not_exact` and retry-eligible). Only `exact`
target-market evidence ends a recovery attempt early. Server scope is authoritative: these two states are decided from the server-computed
`variant_match` and market BEFORE any model declaration, so a model's `found` (or a `conflict_resolved` citing an
out-of-scope item) never turns another variant's or another market's evidence into target evidence. The model's
`market` is a claim too: when the source itself does not establish a market (domain, regional site, path locale,
Hebrew text), the evidence market is `unknown`, whichever market the model named; the claim is kept as
`model_market_claim`. Vocabulary (model families with Hebrew names, body / propulsion / drivetrain terms) lives in
`data/identity_vocabulary.json`.

`binding-v2` makes `exact_market_trim` reachable when the government trim does not identify itself in prose, with two
deterministic, fail-closed rules, each recorded as `binding_basis` on the evidence item: **`qualified_trim_phrase`**:
a trim made only of generic words (`MAX`, `PRO`, `BASE EDITION`) matches only as the family followed by its words
(`g6 max`, `g6-max`, `xpeng g6 max`, `G6 בגרסת MAX`), never the word alone ("max power"); **`single_trim_catalog`**:
a fact already bound at `exact_technical_variant` (no veto, no mixed dimension) from a target-market official source
(or a target-market document whose identity zone names the family) that names no other trim binds at
`exact_market_trim` only when `data/catalog_trim_index.json` lists exactly ONE government trim for the target's
technical variant in its model year (also none in a neighbouring power bucket within the 3 % tolerance; a missing
key or an entry marked `"complete": false` never applies). The index is built by `scripts/build_trim_index.py`
(`DATABASE_URL`, else the data.gov.il catalog; workflow `build-trim-index`). A multi-variant table's value also gets a
`column_identity` layer (the column header plus that column's power / drivetrain / battery / ... cells), so the
column's own power decides instead of the document's mixed status; a value in two columns gets none. A
`variant_not_exact` field reports what stopped it as `binding_gap:*` info (diagnostics `binding_gap_counts`,
`binding_gap_trim`, `binding_gap_technical`), and re-acquisition skips a field that is open only on binding although
it has admitted official target-market evidence (`reacquire_skipped_binding_gap`; one search at most when the gap is
the trim and no official target-market document names it).

**Source authority** is `government | official_manufacturer | official_importer | official_media | aggregator |
marketplace | publisher | unknown`, from `data/source_rules.json` (government suffixes, per-brand slugs and press
domains, aggregator / marketplace / publisher lists). Official is relative to the target brand. Authority is not
identity: an official page about another variant is still `different`.

**Market** comes from the source: domain TLD, regional site (`toyota-europe.com` → EU), path locale
(`/en-gb/`), else Hebrew text → IL. When the source says nothing, a model claim of ANOTHER market is kept as
such, while a claim of the target market is recorded as `unknown` (unverified). A UK official fact stays `UK`.

**Schema semantics.** Every field has a `semantic_definition` (shown to the models), a `binding_requirement`,
and where it matters `semantic_exclusions` (e.g. hybrid torque: engine torque only; cargo: seats up, not folded)
and `not_applicable_when` (`gear_count` when exact, target-market evidence says the gearbox is CVT / e-CVT: no
discrete stepped gear count; never over the field's own target evidence). Plug-in-only fields (`electric_range_km`, `electric_range_standard`, AC/DC charging time,
window and power, `energy_consumption_kwh_100km`) apply to `plug_in` and `battery_electric` only: a regular
hybrid does not plug in, so they leave its requested fields, its coverage denominator (37 instead of 45), its
recovery queue and its dashboard. `climate_zones` reads "בקרת אקלים: מפוצלת" as 2 only through an explicit
schema pattern.

**Typed values and time.** Each evidence item keeps `value` as stored plus `typed_value` (`scalar`, `range` with
`min`/`max`/`unit`/`condition`, `boolean`, `enum`, `text`, `compound`). A condition is kept only when the quote
states it. Time-sensitive fields (`list_price`, `registration_licence_fee`, warranty fields) carry `valid_as_of`
from a date the source states (in the quote or its own source lines), else the document's own publication metadata
(JSON-LD / meta tags / PDF metadata), else `temporal_status: undated`; the fetch time is kept separately as
`observed_at` and never used as validity. The finalizer is asked to report such values with their date.

**Consistency checks** (`src/consistency_checks.py`): CO2 vs fuel consumption (government WLTP CO2), curb vs gross
mass, rim vs tire size, usable vs gross battery, dimensions vs length, fields vs propulsion. Each says
`consistent | suspicious | not_checkable`; results are in `result.json` (`consistency_checks`), the finalizer
bundle and the UI, and each evidence item of the bundle gets a `sanity_status`. Nothing is ever replaced.

Evidence items in `result.json` carry `admission_status`, `admission_checks`, `entailment`, `binding_level`,
`binding_veto`, `binding_dimensions`, `variant_match`, `model_variant_claim`, `market`, `market_basis`,
`model_market_claim`, `source_authority`, `authority_basis`, `typed_value`, `condition`, `valid_as_of` /
`temporal_status`, `source_date` and `observed_at`. Rejected requests are `evidence_rejected` events. The finalizer
bundle drops model notes (a note once misattributed a value to another source) and adds `consistency_checks` and
`evidence_admission` counts. Metrics: `evidence_rejected`, `evidence_variant_exact` / `_unclear` / `_different`,
`evidence_unbound`, `evidence_official_source`, `evidence_aggregator_source`, `consistency_checks_suspicious`.
Runs written before admission load unchanged (their evidence keeps the model's own `variant_match`).

### Israeli source playbook (PR #44)

- **Government registry layer (P1).** `scripts/build_gov_registry_index.py` aggregates the data.gov.il registry of
  private and commercial vehicles (licence-plate level, paged CKAN reads with a pause between pages) into
  `data/gov_registry_index.json`: per `tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur`, the front / rear tyre size
  distribution (majority, share, N). It verifies the resource's columns first and writes nothing when `zmig_kidmi` /
  `zmig_ahori` (or a key column) are missing; the `build-gov-registry-index` workflow (manual) runs it and opens a pull
  request. At the start of a run `src/gov_registry.py` admits the target's entry as government evidence
  (`source_authority` `government_registry`, market IL, `exact_market_trim` through the government model code):
  `tire_size_front` / `tire_size_rear` when the majority share is >= 0.8 and N >= 20, `rim_diameter_in` from that size's
  R number; a share under 0.8 gives only `alternative_tire_sizes` (the two most common sizes); N < 20 gives nothing. No
  network and no database at runtime; the shipped index is an empty `"complete": false` placeholder until the workflow
  fills it.
- **Israeli version pages (P2).** Before the agentic loop an IL-market run resolves the target's single-version page
  on the sites of `data/source_rules.json` → `il_version_sites` (cartube.co.il, icar.co.il, auto.co.il): at most 3
  site-restricted searches from the data file's templates, version links of the site's model / price-list pages,
  at most 6 fetches, robots.txt respected, at most one request per second per domain, the shared cache (counters
  `acq_il_version_searches`, `acq_il_version_fetches`, `acq_il_version_pages_found`; event `il_version_pages`). A page is
  accepted (`src/il_version_pages.version_page_verdict`) only when it describes exactly one version and its year,
  displacement (±0.06 l), drivetrain, propulsion, body and power (±3 %; for a hybrid, or a page without a power, the
  catalog's only entry for manufacturer / family / year / propulsion / drivetrain) match; every value on it binds
  `exact_technical_variant` (`binding_basis` `il_version_page`), its market trim only when the page names the trim.
- **Torque rpm (P3).** A field with `rpm_guard` (torque_nm) never reads an engine speed: a number followed by an rpm
  term (also through a range) or in a row whose label names rpm without a torque unit is rejected; "קג"מ" converts to
  whole Nm.
- **H1 relaxed (P4).** Fields with `variant_invariance: "engine"` (gearbox_type, gear_count, length / width / height /
  wheelbase) of a hybrid target are not held by `system_power_unmapped` when the document's displacement is the
  target's; a single-version Israeli page binds a hybrid only with the catalog's single entry, and one that states
  another power is itself `system_power_unmapped`. Relative references (H2) and stale publications (H3) still cap.
- Proof gate: `python scripts/pr44_proof_gate.py` (fixtures + goldens), `--runs-dir /data/runs --run <id>` (replay) and
  `--runs-dir /data/runs --fresh <id>` (ok fields per vehicle with their source URLs).

### Durable pre-finalization checkpoint

`result.json`, `batch.json` and `input.json` are written through one atomic primitive
(`src/storage/atomic.py`): serialize completely, write a temporary file in the same folder, flush and
`fsync`, `os.replace` onto the destination, `fsync` the folder; on any failure the temporary file is removed
and the previous file stays intact. A crash mid-write therefore leaves the old or the new file, never a
truncated one. The shared document cache uses the same primitive without `fsync`.

**Fail closed:** the finalizer request is made only after the checkpoint below was persisted. If writing
it fails, the run logs `result_write_failed` (stage `checkpoint`) and `finalization_not_started`, makes no
finalizer call, and ends `finalization_failed` with `finalization.status = not_started` (still eligible for
`--finalize-existing` once the disk problem is fixed).

Right before the paid finalizer request, `result.json` is written with `status: finalization_pending`,
`partial: true`, `output: null`, the evidence, field recovery, candidate summary, current field states
and research bundle (`finalization_checkpoint_written` event, then `finalization_started`). If the
process dies during the finalizer request, the run reloads as `finalization_pending` (never the generic
`incomplete`) and the UI says so in Hebrew. `--finalize-existing` (or the recovery helper) then makes
exactly one finalizer call: no research, harvest, sweep, recovery or search is repeated.

**Every started run leaves a `result.json`**, including research failures, finalizer timeouts and
Ctrl+C. It holds the status, the errors, all tool calls, evidence, documents, counters, usage per
phase, API attempt statistics, the partial research bundle and the last model content, and never any
fabricated structured fields. Ctrl+C makes no further model call. The CLI saves the partial result
and exits with code 130.

### Deterministic Final Assembly

With `FINAL_ASSEMBLY=deterministic` (the default) no model decides a final value (`src/final_assembly.py`). The
output keeps the finalizer's shape (so the UI, result.json and benchmark readers keep working) and is built from the
current field states (`current_evaluation()`) and the admitted evidence items only; candidates, excerpts, model notes,
the research reply and any model output are never a source of values. Per field state:

| State | Output entry |
|---|---|
| `ok` | `value` / `unit` / `market` / `valid_as_of` from the evidence that carries the value; `provenance` `israel_direct` (target-market evidence) or `foreign_direct` (a portable foreign item, its `portability_basis` in `notes`); `evidence_ids` = those items |
| `conflicting` | `value: null`, every distinct admitted value in `alternatives`, the field in `conflicts` with its `conflict_class` |
| `foreign_market_only` / `variant_not_exact` / `weak_provenance` | `value: null`, the admitted items as `alternatives`, `provenance: unresolved`, a note naming the state |
| `not_applicable` | `value: null`, `provenance: unresolved`, note `not_applicable` |
| `missing` / `unresolved` | `value: null`, `provenance: unresolved` |

The items that carry an `ok` value are the field's admitted items in the evaluator's server-side target scope
(`in_server_scope`: binding reaches the field's requirement, target market or portable), narrowed to the evidence ids
of a backed `conflict_resolved` declaration when the evaluator accepted one. If they disagree in value (and the
evaluator's conflict classification did not already establish `unit_equivalent` or a trim-bound
`scalar_inside_range`), the run logs `final_assembly_inconsistent` and emits the field as `conflicting`: a value is
never picked by majority or order. `provenance_summary` is computed from the entries; `variant_identity.government`
comes from the Level 1.5 record and `local_commercial_name` only from an `ok` `local_trim_name` field
(`mapping_basis: explicit_source`), else null with `inference`.

After assembly, ONE small no-tool narration call (phase `finalization`, the finalizer's reasoning effort,
`max_tokens` <= 2000, one retry with double the cap if the reply is cut) may write `summary` and `research_trace` from a
digest of field-state counts, ok / open field names and source domains (no values). Only those two keys are taken from
its reply, and only if every number they state also occurs in the digest; otherwise the narration is discarded
(`narration_rejected` with the offending tokens). If it fails, times out or is rejected the code-written summary stays
and the status is unchanged. The durable `finalization_pending` checkpoint is still written
before it. `result.json` records `final_assembly` and `output_source` (`code`; `model` with `FINAL_ASSEMBLY=llm`, which
keeps the previous finalizer exactly). `--finalize-existing` and the UI's finalize retry use the same assembly: the mode
the run recorded in `run_started`, never the current environment. In
`benchmark.compute_metrics`, `coverage_pct` / `target_filled` count only output values whose `evidence_ids` all exist in
the run's evidence; `filled_by_output` / `coverage_pct_by_output` keep the old count. The diagnostics' final-state
counts stay the primary metric.

### Candidate yield: structural harvest, site map, grounded candidates

The bottleneck after the adjudication sweep is candidate YIELD: on record 38626 (Toyota COROLLA 2024 BUSINESS EDI)
contract runs reached 24-27 of 37 fields with any candidate. Four additions raise yield; all of them produce
CANDIDATES or routing hints only, and only `admit()` turns anything into evidence:

- **Structural HTML pairs** (`src/structure_harvest.py`, method `dom_pair`, routed like a table row): `<dl>`,
  two-column elements (`li > span + span`, card rows), "label: value" inside one element; repeated sibling groups or a
  dictionary alias; nav / footer / header / consent / hidden elements skipped (also ARIA roles navigation / menu /
  banner / contentinfo and class / id words nav, menu, breadcrumb, footer, header, cookie, consent); quotes
  "label | value" checked against the text admission reads; at most 400 pairs per page; pages over 3 MB skipped and 2 s
  per page (`harvest_capped`). **Borderless PDF tables**: a pdfplumber text-strategy pass
  (`source: pdf_table_text`) on pages without ruled tables or with >= 3 aliases (first 60 pages, 5 s per document). **Unit anchors** (`unit_anchor`):
  number + unit with exactly one field's alias within the clause / 60 chars (existing candidates always win).
  `HARVESTER_VERSION` is `harvest-v5` (candidates of multi-variant tables carry `column_identity`), so older cached candidates are recomputed.
- **Importer site map** (`SITE_MAP`, `src/site_map.py`): robots.txt / sitemap indexes / `.xml.gz` with the standard
  library (depth 3, 50 files, 20,000 URLs per domain, 7-day per-domain cache, robots `Disallow` respected); the top 15
  ranked URLs go into the first contract-acquisition message (20 s bound; a failure logs `site_map_failed`). The bound
  holds while bodies stream in (a slow server is cut), each file is capped at 15 MB / 50 MB decompressed, the
  per-domain single-flight wait times out at the remaining deadline, and a crawl the deadline cut short is cached as
  `partial: true` for 1 hour only.
- **Grounded candidates** (`GROUNDED_CANDIDATES`, `src/grounded.py`): for open fields with no admissible candidate after
  the adjudication dry run, one no-tool call per top document (max 3, 15 fields, 24,000 chars of numbered blocks); the
  model returns block offsets, code cuts the quote, checks it against the document and dry-runs admission; admissible
  ones are adjudicated as class A. A grounded candidate keeps `extraction_method: grounded_llm` everywhere: a field with
  any admissible grounded candidate is always class A (re-acquisition included), and coverage / triage count them apart
  (`grounded_candidate_count`; `candidate_count_total` stays the deterministic harvest's).

### Targeted re-acquisition (default)

`RECOVERY_MODE=reacquire` replaces the tool-using recovery agent with the two primitives the rest of the engine uses.
Per recovery cluster, breadth-first, one pass: a short acquisition episode with the contract tool surface (2 turns,
`CLUSTER_SEARCH_BUDGET` billable searches, 3 fetches; the research reasoning settings, booked as recovery; site-map
URLs ranked for the cluster, already fetched URLs, negative routes), the automatic harvest of its new documents,
grounded candidates on them, and adjudication of the cluster's unseen candidates. The recovery model never receives
`store_evidence` or a cached-document tool. Two consecutive API failures stop recovery; one failed cluster never stops
the others.

### Clustered tail recovery

After the document sweep, the fields that are still open (the "tail") are recovered in **clusters**, not one
field at a time (`RECOVERY_MODE=cluster`, the default before PR #31; `RECOVERY_MODE=legacy` keeps the per-field retries
described in the next section). Field detection, the evaluator and every PR #18 rule are shared by both modes.

```
current_evaluation() → tail triage → recovery clusters → breadth-first cluster attempts → finalizer
                         (scheduling)   (schema)            local-first · adaptive turns · search budget
```

- **Triage** (`src/tail_planner.py`) puts each open field in a scheduling category, never a field state:
  `candidate_rich_local` (harvested candidates no model has seen yet), `conflicting`, `foreign_only`,
  `low_yield` (an earlier web attempt for it found nothing new), `policy_blocked` (no recovery attempts in the
  schema, or own evidence contradicting a schema rule) and `true_missing`.
- **Clusters** come from the schema's `recovery_cluster` (`technical_spec`, `performance`, `charging_ev`,
  `equipment`, `multimedia`, `tires_wheels`, `commercial`, `warranty`). One attempt asks for the best source
  for the whole cluster: one official spec page usually answers a dozen technical fields.
- **Every new document benefits every open field before the next paid turn.** A fetched document is
  harvested for ALL applicable fields, every field is re-evaluated with `current_evaluation()`, every cluster
  is pruned, and the model's next turn is told which fields are now resolved and which new candidates
  appeared. Candidates are still promoted only through `store_evidence` and the admission gate (the
  reliability rules allow no automatic promotion).
- **Failure isolation.** An API error (timeout, 5xx, ...) in a cluster attempt ends that attempt only: it is logged
  (`field_recovery_failed`), the attempt is marked `failed`, its candidates stay fresh and the next cluster still runs.
  Only two consecutive failed attempts stop all recovery (`stopped: api_failure`, `api_failure_stop: true`, a
  provider-outage guard). Recovery requests make one HTTP attempt by default (`GLM_RECOVERY_MAX_ATTEMPTS`), so a
  timed-out request is not re-sent identically. The summary reports `failed_attempts` and `api_failure_stop`.
- **Local-first.** While a cluster has candidates no model has seen, it first gets one pass with cached-document
  tools only (the packet shows those unseen candidates first), and every round runs such local passes before any
  web attempt. The local pass does not use up the cluster's web attempts. With no unseen local material that
  model pass is skipped and web research starts directly. A candidate counts as "seen" only once a model call
  that carried it RETURNED (`candidates_presented` / `cluster_candidates_announced` events): a sweep chunk or
  cluster call that failed (e.g. a read timeout) leaves its candidates fresh, so their cluster keeps its local pass
  (`failed_chunk_candidates_kept_fresh` in the sweep summary).
- **Ranking, not truth.** Cached documents are ranked per cluster by `source_yield_score` (open fields with
  candidates, binding level, authority, target market, tables, evidence already yielded, identity, parser
  confidence). Already-paid search results that were not fetched are passed as `search_hints` (title, snippet,
  URL), so fetching a strong result replaces a new search. Hints are routing metadata, never evidence.
- **Adaptive turns.** An attempt starts with `CLUSTER_BASE_TURNS` (2) and gets another turn, up to
  `CLUSTER_MAX_TURNS` (never above 4), only after a turn with real novelty: a new official document, a new
  candidate for an open field, newly admitted evidence, a better binding, a better field state or a narrower
  conflict. An error page (403/404), a repeated query, a cached re-read, a rejected store, evidence bound to
  another variant or commentary is not novelty. When the
  base turns end without novelty the attempt stops (`no_novelty_stops`); a web attempt that found nothing marks
  its fields `low_yield`, and the cluster is not retried for them.
- **Billable search budget per attempt** (`CLUSTER_SEARCH_BUDGET`, 4) counts the provider calls a search makes
  (`search_official_domains` makes one per uncached domain); cache hits are free; a search that would exceed
  the budget is refused before execution.
- **Breadth-first over clusters**: every cluster gets attempt 1 before any cluster gets attempt 2
  (`CLUSTER_MAX_ATTEMPTS`, 2), and `FIELD_RECOVERY_MAX_TOTAL_STEPS` still caps all recovery turns of a vehicle.
- **Conflicts are classified before research** (`src/conflict_normalizer.py`): `unit_equivalent`,
  `scalar_inside_range`, `market_difference`, `variant_scope_difference`, `internal_source_inconsistency`,
  `true_conflict`. Only the first two can clear a conflict, and `scalar_inside_range` only when the scalar is
  bound to the exact market trim (179,990 inside 179,990-183,990 for the Business trim); otherwise the field
  stays `conflicting`. A conflicting field gets a compact resolver packet (identity, the field's meaning, each
  competing item with quote, source, binding and authority, recent queries), not the research bundle.
- **Market portability**: `market_sensitivity` (high | medium | low) and `portability_scope` (none |
  exact_technical_variant) per field. A foreign item counts for the target market only when the policy allows
  it, it binds exactly at that level, an official source states the value, all such foreign items agree and
  no target-market evidence contradicts it (any contradiction vetoes). The item keeps its market; the bundle
  shows `portable_to_target_market`, `portability_basis` and `portability_policy`. Price, fees, warranty and
  trim equipment are never portable, and neither are height, ground clearance, weight or boot volume.
- **Unknown market is not the target market**: `market = unknown` means the server could not establish the
  source market. Such an item is never target-scope by itself (state `foreign_market_only`, info
  `market_not_established`). Only a field with an explicit `unknown_market_policy: portable` (the
  low-sensitivity technical fields) lets it count, and only through the same portability verdict as an official
  foreign fact (verdict `market_established: false`; the market stays `unknown`). High-sensitivity and
  `portability_scope: none` fields never accept it.
- **Tools the host cannot run are never offered**: `render_page` is left out of every tool schema (and the
  research prompt) when Playwright is missing; `DISABLED_TOOLS` switches tools off by configuration.

Metrics (`result.json` `field_recovery`, the benchmark tab): `tail_fields_at_start / _resolved / _remaining`,
`tail_model_calls`, `tail_search_calls`, `cluster_attempts`, `fields_resolved_by_cluster`, `no_novelty_stops`,
`budget_extensions`, `conflicts_normalized_without_search`, `portable_facts_accepted / _rejected`,
`fields_resolved_per_tail_turn / _search`, `tail_cost_usd` and `cost_per_tail_field_resolved` (when pricing is
known). Legacy recovery reports the same tail metrics.

Offline benchmark (`python tests/fixtures/corolla_tail.py`): the Corolla Touring Sports 1.8 Hybrid tail
(height conflict inside one aggregator page, no ground-clearance source, fuel tank only on an official EU
page, a price range) played by the same deterministic policy model in both modes:

| | legacy | cluster |
| --- | --- | --- |
| tail fields at start (of 14 requested) | 11 | 11 |
| fields ok after recovery | 10 | 10 |
| recovery model turns | 24 (cap reached) | 9 |
| recovery searches | 2 | 2 |
| documents fetched in recovery | 2 | 2 |
| fields resolved per turn | 0.29 | 0.78 |

Both modes leave height conflicting, ground clearance unresolved and curb weight / boot volume foreign-only:
coverage is not bought by loosening truth.

### Verified fact reuse, negative research memory and the feedback dataset

Related variants (the same technical family in one batch, or a re-run) get cheaper through a cross-run memory under
`<cache>/memory/` (`src/research_memory.py`, `RESEARCH_MEMORY_ENABLED`, default on). One file per fact and one per
run, each written atomically, so 50 concurrent workers never share an append target.

- **Verified facts.** At the end of a run, an admitted fact is recorded only when its own run settled the field
  (final state `ok`, no conflict, the item among the field's evidence), the field's schema `reuse_scope`
  (`none | exact_market_trim | exact_technical_variant | body_powertrain`) allows it, the item bound exactly at that
  level, and the target identity at that level is complete (a key built from missing parts would collide). The
  technical-variant key includes the full government model code (letter suffixes included) and the transmission, so
  battery size, manual vs automatic and the driven axle at equal power are kept apart. The market-trim key uses every
  word of the trim, so "GR SPORT" is not "SPORT". A later run that ends the field unsettled while holding a reused
  fact marks that record disputed, and no variant reuses it again. The
  record carries its scope key, source, quote, binding, authority and a schema identity (the field's meaning,
  units and policy plus the admission / binding / harvester versions); stale, expired or colliding records are
  ignored. Price, fees, warranty, equipment, multimedia, tyres and every time-sensitive field are never reused;
  curb weight, height and ground clearance only within the exact market trim.
- **Reuse is re-admission, not trust.** At the start of a run, matching facts are re-admitted against the NEW
  target with the same gate as `store_evidence`. The document must be in the shared cache, the quote must state
  the value, and the server-side binding to the new target must again be exact at the reuse level. A fact from a
  page about the 1.8 Hybrid therefore never reaches a 2.0 Hybrid. A reused item keeps its original source and
  `reused_from`. The same fact on twenty variants is one source, never corroboration. The research prompt lists
  the fields already supported so they are not researched again.
- **Negative routes.** A web recovery turn without novelty records its working routes as "no new material" for the
  fields still open, per identity scope and field identity. The routes are search queries attributed to the field
  whose label matches most specifically, plus fetched URLs. Errors and 429s are never recorded, and time-sensitive
  fields keep no negative memory. A route is not recorded when the call itself failed (an HTTP error status, or every
  domain of a domain search erroring; a domain whose own provider search errored is left out). A route is a
  PROVIDER operation (`src/research_memory.route_identity`), never looser than what it stands for: a search is
  its backend and engine, its query (apart from case and spacing), its domain restriction and its effective result
  count (after the tool's default and clamp), the same identity the search cache keys on
  (`src/tools/search.planned_searches` / `search_key`). So "Corolla ground clearance" on `toyota.co.uk`, on
  `toyota.co.il` and with no domain are three routes, 8 and 20 results are two, and GLM and DuckDuckGo are two; a
  `search_official_domains` call is one route per domain it searches (5 results each). A fetch is its mechanism
  (`fetch_url`, `fetch_pdf` and `render_page` are different routes: a browser render can expose what a fetch did
  not) and its URL, plus the effective `wait_ms` of a render. Later runs see the routes as
  `known_unproductive_routes` (only those this session could repeat), and the runtime refuses, before execution, a
  request whose provider routes were ALL recorded for the open fields it serves (`NEGATIVE_ROUTE_BLOCKING`): a
  failed UK search never suppresses an IL one, a 20-result search or another provider always runs, and a domain
  search with any domain not tried before runs. Records of an older route format, or missing any identity part,
  refuse nothing. Only the route is refused: the cluster's research still runs, and any new query or page is executed even when other
  routes for the same field failed before. Failed routes are never evidence that a value does not exist. This is
  scheduling only: it never creates evidence, never changes a field state, never marks a field not_applicable and
  never resolves a conflict. The routes expire after `NEGATIVE_ROUTE_MAX_AGE_DAYS` (30).
- **Historical recovery yield** (by cluster / field, manufacturer, propulsion and source family: attempts, turns,
  searches, documents, resolutions, by-product resolutions, tokens) only orders clusters within a round, and only
  with at least 5 samples. It is never a confidence in any value.

**Training feedback** (`src/training_feedback.py`, deterministic, no API or model call). Every run writes
`<run>/training_feedback.jsonl` with labelled examples. The labels:

- `candidate_accepted` / `candidate_rejected`;
- `candidate_false_positive`: only with an explicit contradiction, i.e. the gate refused the value and the same
  document states another value, or the value belongs to another field or quantity. Not being promoted is never a
  label;
- `deterministic_miss`: admitted evidence with no parser candidate for that field, value and document;
- `evidence_admission_rejected`;
- `variant_binding_rejected`;
- `conflict_example`;
- `portability_accepted` / `portability_rejected`.

Each example has the vehicle identity, field, values, unit, a cut quote, document, source and its authority,
market, binding, matched alias, extraction method, parser confidence, reason code, pattern, schema hash,
harvester version, phase and time.

`python -m src.cli --export-feedback [--feedback-dir DIR]` aggregates all runs deterministically (old runs are
derived from their events) into `training_feedback.jsonl`, `training_feedback.csv` and
`training_feedback_summary.json`. The summary counts misses, false positives, admission and binding rejections by
field, source family and pattern, which shows the parser or admission rule to fix next.

Offline cold/warm benchmark (`python tests/fixtures/corolla_family.py`). It uses one shared cache and the same
policy model as the tail benchmark:

| | A 1.8 Business (cold) | A again (re-run) | A2 1.8 Premium (warm) | B 2.0 Business (warm cache) | A2 without memory |
| --- | --- | --- | --- | --- | --- |
| model calls | 13 | 8 | 8 | 16 | 16 |
| tokens (fixed 1,100 per fake call) | 14,300 | 8,800 | 8,800 | 17,600 | 17,600 |
| recovery turns | 9 | 4 | 4 | 12 | 12 |
| searches | 2 | 1 | 0 | 3 | 2 |
| verified facts reused | 0 | 9 | 8 | 0 | 0 |
| failed routes shown to the model | 0 | 1 | 0 | 0 | 0 |
| equivalent routes refused | 0 | 0 | 0 | 0 | 0 |
| fields ok (of 14) | 11 | 11 | 11 | 10 | 9 |

A2 reuses the technical facts (tank, battery, torque, performance, dimensions) but not price, warranty, height or
curb weight. It also does not reuse the boot volume, because A left that field foreign-market-only. B reuses nothing,
and its boot stays 581 L, never A's 596 L. The re-run's saved search comes from the model following
`known_unproductive_routes` (the policy model in the benchmark does). Had it repeated the failed query, the runtime
would have refused that one request and let every other route run (tested separately). The scale metrics are
`verified_fact_cache_hits`, `negative_route_cache_hits` (equivalent routes refused), `negative_route_fields_shown`,
search / document / candidate cache hits,
`verified_facts_recorded` and `training_feedback_examples`.

### Targeted field recovery (legacy per-field mode)

Between research and finalization, the requested enrichment fields that primary research did not obtain
get focused retries:

```
PRIMARY RESEARCH → FAILED REQUESTED-FIELD DETECTION → TARGETED FIELD RETRIES → UPDATED EVENTS / EVIDENCE
                 → COMPACT RESEARCH BUNDLE → FINALIZER
```

**The requested fields drive everything.** At run start the exact list is resolved and saved as
`requested_fields` (in `run_started` and `result.json`). It comes from `data/enrichment_fields.json`, or
from `ENRICHMENT_FIELDS` / `--fields` (any names, including ones the schema doesn't know), or from
`ENRICHMENT_SCHEMA_PATH`. A spec has a `name`, and optionally a `description`, `group`, `unit`,
`applies_to` (Level 1.5 `propulsion_normalized` values) and `recovery_attempts`. The engine
(`src/field_recovery.py`) contains no field names, and a test enforces that. A new field gets the same
recovery with no code change.

**Detection uses the model's own research state.** It is not a fact check. A field is **not** retried when
the research produced a candidate value backed by a `store_evidence` record, or when the field is
`not_applicable` (by the schema's `applies_to`, by `report_field_status`, or in the model's own answer).
A field **is** retried when:

- `missing`: there is no candidate value and no evidence record;
- `weak_provenance`: a value exists only in the model's answer, with no evidence record;
- `unresolved`: the model said unresolved;
- `foreign_market_only`: no candidate is in the server-side target scope, and the ones that may apply to the
  variant come from a known other market;
- `variant_not_exact`: every candidate is about another variant (`variant_match=different`, by the server-side
  binding veto or the model) or from a source that does not name the model (`unbound`);
- `conflicting`: the model reported an unresolved conflict.

Several stored values for one field are recorded as info only. Nothing is removed, changed or ranked.

**Each failed field gets its own focused task.** It is a fresh conversation with a dedicated
field-recovery prompt and a compact packet:

- `vehicle_identity` and the `requested_field` spec;
- `failure_reason`;
- `existing_candidates` / `existing_evidence`;
- `relevant_documents` (the cache, documents behind the field's evidence first);
- `previous_queries_for_this_field`;
- earlier attempts.

The primary conversation is never sent. The task can use every tool, exploiting cached documents first.
It stores evidence into the same store and ends with a JSON status for the field. The field is then
re-evaluated. Scheduling is **breadth-first**: round 1 gives every queued field its first attempt
(A1, B1, C1, ...); only then does round 2 revisit the fields still unresolved (A2, C2, ...), so a second
attempt never starves a field that has not had its first. Packets also carry the field's
`deterministic_candidates`. Coverage metrics: `recovery_fields_given_first_attempt`,
`recovery_fields_never_attempted`, `recovery_second_attempts_started`, `recovery_unique_fields_touched`,
`recovery_unique_fields_resolved`, `recovery_resolution_per_turn`, `fields_never_attempted_due_to_budget`,
and `attempt_order`.

Settings:

- `FIELD_RECOVERY_ENABLED=true`.
- `FIELD_RECOVERY_MAX_ATTEMPTS=2` per field. A spec's `recovery_attempts` overrides it; 0 means never.
- `FIELD_RECOVERY_MAX_STEPS=4` turns per attempt.
- `FIELD_RECOVERY_MAX_TOTAL_STEPS=24` is a hard cap on recovery model turns per vehicle, across all fields,
  attempts and turns (0 = no cap). At the cap no further recovery turn or attempt starts. Evidence and
  unresolved or conflicting states are kept, and the run continues to the compact finalizer; it does not
  fail.
- Results report `field_recovery_turn_budget`, `field_recovery_turns_used`,
  `field_recovery_turns_remaining`, `fields_not_attempted_due_to_budget`, `field_cut_short_by_budget` and
  `stopped = "max_total_steps"`.

The CLI `--dry-run` prints `field_recovery_worst_case_model_turns`, which is bounded by the cap.

**Same-scope conflicts.** A field is `conflicting` (and retried) when:

- at least two candidates come from the target market;
- none of them is marked `variant_match=different`;
- their values are materially different (numbers compared as numbers, so `"1,066 Nm"` equals `1066`;
  anything else compared as normalized text);
- the model has not explicitly resolved them.

An explicit resolution is a `conflict_resolved` declaration (`report_field_status` or a retry reply)
newer than the field's latest evidence. A plain `found` does not resolve a conflict.

The following are info only, not conflicts:

- an IL candidate against another market's value;
- a candidate explicitly marked as another variant.

Code never picks a value and never removes a candidate. A conflicting field's retry packet carries both
candidates and a conflict-resolution task: trim, model year, drivetrain, boost or performance mode,
nominal vs peak, unit conversion, source wording and manufacturer documentation. A conflict still
unresolved after the last attempt, or when the cap is reached, reaches the finalizer as `conflicting`,
with every candidate.

The finalizer runs after the retries. Its bundle carries the requested fields, every field's final state,
the retry replies and the research model's own primary JSON. If no field needed a retry and the research
model already returned valid JSON, no finalizer call is made.

Results record `field_recovery`: the queue, attempts, states before and after, and recovered vs still
failed fields. Usage is booked separately as `usage_field_recovery`. The UI has a **Field recovery** tab,
and Benchmark shows `fields_failed_primary`, `fields_retried`, `fields_recovered`, `fields_still_failed`,
`field_retry_attempts` and `field_recovery_model_calls`. `--finalize-existing` never retries fields (it
makes no new research).

### Exact repeats, novelty and the dynamic retry queue

**Exact repeats are answered before dispatch.** Every read-only call gets a canonical signature
(`src/context.py: call_signature`) built from its normalized arguments, with defaults filled in:

| Tool | Signature |
| --- | --- |
| `search_web` | whitespace-normalized query, domain, `max_results` |
| `search_official_domains` | query and sorted domains |
| `fetch_url` / `fetch_pdf` / `render_page` | tool and URL without fragment (render also `wait_ms`) |
| `find_in_document` | document, query (case-insensitive, like the tool), scope, `context_chars`, `max_hits` |
| `extract_html` | document, offset, `max_chars` |
| `get_cached_document` | key, offset, `max_chars` |
| `extract_tables` | document, `max_tables`, `max_rows`, `start_table` |
| `get_structured_data` | document, `max_chars` |

If the same signature already completed successfully in this vehicle run (in any phase), `ToolSession`
does not dispatch. It returns the earlier result to the model as a normal `role=tool` response, marked
`reused_from_step` with an operational note, and logs a `tool_reused` event instead of
`tool_call`/`tool_result`. A replay makes no HTTP request, no extraction, no billable search, no new
document and no novelty.

Only identical operations are replayed: a different query, document, offset or option always runs.
Failed calls are never replayed, and `store_evidence` / `report_field_status` are never deduplicated.
This applies within one vehicle run; the document cache still handles reuse across runs.

**Novelty means new material, not a new operation.**

- A zero-hit `find_in_document`, empty tables or structured data, or reloading a document the run
  already knows is a new operation but not new material.
- A turn of only such calls, or only replays, is idle, so `AGENT_NO_NEW_RESEARCH_TURNS` ends unproductive
  loops (including a recovery attempt that keeps re-emitting the same call).
- New material is content not exposed before: new hit offsets, new table indexes, first structured data
  per document, or uncovered text spans.

**Field recovery memory and dynamic queue**

- Each retry packet includes `already_attempted_operations`: up to 40 compact entries (tool, document,
  query or offset, phase, field/attempt, outcome such as `no_hits`, `hits`/`hit_count` or `loaded`, never
  contents). It covers primary research and earlier attempts.
- The retry prompt says not to repeat those operations, and allows by-product `store_evidence` for other
  requested fields clearly stated in the same source.
- After every attempt, all requested fields are re-evaluated. A queued field that another field's
  recovery resolved is skipped without its own model call (`field_recovery_queue_resolved_indirectly`).

**Live feed**

- Recovery turns read `🧠 field_recovery · <field> · attempt 1/2 · turn 2/4 · tokens N`.
- Tool rows read `🔧 <field> · attempt 1 · step 39`.
- Replays read `↺ reused result of step N (not executed again)`.
- `model_response` events carry `field`, `attempt`, `turn` and `turn_budget`, while `phase` stays
  `field_recovery`.

**Metrics:**

- Suppressed repeats: `duplicate_calls_suppressed`, plus the `duplicate_searches_suppressed`,
  `duplicate_fetches_suppressed` and `duplicate_inspections_suppressed` breakdown.
- Recovery outcomes: `fields_resolved_directly_by_recovery`, `fields_resolved_indirectly_by_other_recovery`,
  `recovery_operations_with_new_material` and `recovery_operations_without_new_material`.
- `tool_calls` now counts real executions only.

### Idempotent evidence and early recovery exit

**Evidence facts are stored once.** `store_evidence` is never deduplicated by the read-only replay
mechanism; it has its own fact identity (`src/tools/evidence.py: evidence_fact_key`). Two writes are the
same fact when all of these match:

- the normalized field;
- the value (`205`, `"205"` and `"205.0"` are the same; `"1,066"` equals `1066`; anything else is compared
  as normalized text, so `"205 kWh"` stays different from `205`);
- the unit;
- the source (`document_id`, or else the URL without its fragment);
- the market, variant and `variant_match` (server-computed for admitted evidence) and the condition.

Quote and note are not part of the identity.

A repeat creates no new item. It returns `{"evidence_id": <original>, "stored": false, "reused": true}`,
logs `evidence_reused`, and keeps a different quote or note only as `supplementary` metadata of the
original item. It is not new evidence, so it never resets the idle streak and never looks like
independent corroboration; the finalizer bundle holds the fact once. Metric:
`duplicate_evidence_suppressed`.

**Early success exit.** Within a recovery attempt, after every tool turn the target field is
re-evaluated from the stored evidence. If it is `ok`, the attempt ends at once and no further model turn
is sent just to say "found" (`field_recovery_early_resolved`; metric
`field_recovery_turns_saved_by_early_resolution`).

Every other state continues the attempt, and the final-reply path is unchanged: `conflicting`,
`foreign_market_only`, `variant_not_exact`, `weak_provenance`, `missing`, `unresolved`, and
`not_applicable` without an explicit declaration. The all-fields re-evaluation still runs after the
attempt, so by-product evidence still resolves other queued fields.

### Prior relevant excerpts (retry working memory)

Each retry is a fresh conversation, so it also gets `prior_relevant_excerpts`: a bounded set of excerpts
already exposed earlier in this vehicle run (`src/excerpts.py`). Without them, attempt #2 knows from
`already_attempted_operations` that a document was read, but not what it said.

**Sources** (successful tool results in `events.jsonl`; the cache is never read here, and zero-hit or
failed calls contribute nothing):

- `find_in_document` hit snippets, with query and offset;
- the `extract_html` / `get_cached_document` text slice that was returned, windowed around the first
  field-related term;
- `extract_tables`: only matching rows plus the header, at most 8 rows and 60 chars per cell;
- `get_structured_data`: matching flattened `path: value` leaves, at most 12 lines.

**Relevance** is generic and schema-driven. A score combines:

- +100 when the excerpt came from this field's own recovery;
- +50 when it came from a document behind this field's evidence;
- +10 per overlap (up to 5) with the field's name, description, group and unit, its earlier queries and
  its evidence values.

Only excerpts scoring above 0 are eligible, so unrelated material is never included.

**Deduplication** is deterministic. In the same document, text spans overlapping by at least half of the
smaller span count as one; so does text contained in another excerpt's text. The more focused excerpt
wins (hit snippet, then table, structured data, HTML slice, cached slice).

**Caps:** `FIELD_RECOVERY_PRIOR_EXCERPTS_MAX_ITEMS=8`, `FIELD_RECOVERY_PRIOR_EXCERPTS_MAX_CHARS=8000` and
`FIELD_RECOVERY_PRIOR_EXCERPT_MAX_CHARS=1500`.

Excerpts are context, never evidence: no evidence record is created from them and no model call selects
them. The finalizer bundle is unchanged; its own excerpt section stays the single excerpt store.

Each attempt records `packet_chars`, `prior_excerpt_items`, `prior_excerpt_chars` and
`document_rereads_after_prior_excerpt`. Metrics: `recovery_prior_excerpt_items`,
`recovery_prior_excerpt_chars`, `recovery_attempts_with_prior_excerpts`,
`recovery_document_rereads_after_prior_excerpt`. The Field recovery tab shows each attempt's excerpts.

### Interrupted runs and current field state

**A Streamlit stop or rerun (or Ctrl+C) is a script-control interruption, not an error.** The run:

1. stops issuing model and tool calls (no finalizer);
2. mutes further UI callbacks, without swallowing anything;
3. persists everything already completed, including a partial bundle rebuilt from events;
4. re-raises the original exception.

The result keeps `status: "interrupted"` with `error: null`, and adds `partial`, `interrupted`,
`interrupted_phase`, `interruption_type` and `interruption_message`. The UI shows "Run interrupted during
Field Recovery. All completed research/evidence was preserved. The result below is partial." Real API,
tool and runtime failures stay under Errors.

**Current field states are recomputed from all events.** The bundle's `field_states` run
`evaluate_fields()` over the requested specs, every evidence event, every `field_status` declaration and
the primary output. It is the same evaluator live recovery uses, so it never relies on a stale snapshot.
Evidence stored mid-attempt, as a by-product, or after the last `field_recovery_finished` always counts.
Attempt history (`state_before`/`state_after`, replies) is kept separately. The recovery summary gains
`current_states`, which are authoritative.

**Three distinct lists:**

- `targets_without_stored_evidence`: requested applicable fields with zero evidence records (strict,
  no interpretation).
- `unresolved_targets`: current state is not `ok` or `not_applicable`. Evidence may exist, for example
  `cargo_volume_l — foreign_market_only` or `torque_nm — conflicting`.
- `level3_topics_without_evidence`: Level 3 topics, listed apart from Level 2 fields.

Invariant: a requested field with any evidence record never appears in
`targets_without_stored_evidence`. There is no automatic resume; the UI offers *Finalize from preserved research* (see
[Runs, persistence and reconnects](#runs-persistence-and-reconnects)).

**Declaration freshness.** Every field-status declaration counts only while no evidence for that field
was stored after it. This covers `found`, `conflict_resolved`, `not_applicable`, `unresolved`,
`conflicting`, `foreign_market_only`, `variant_not_exact`, `weak_provenance`, and the primary JSON's
provenance. A stale declaration is ignored and noted as `stale_declaration:<status>`. With unknown
ordering, the declaration still holds.

**Evidence-backed conflict resolution.** `conflict_resolved` closes a same-scope conflict only if:

- its `evidence_ids` are non-empty;
- every id is stored evidence for this same field;
- at least one cited item was stored at or after the moment the conflict became active.

`report_field_status` refuses a `conflict_resolved` without such ids. A recovery reply without them
leaves the field `conflicting`. Code never decides which value is true.

**Early exit is stricter than "usable".** A recovery attempt ends early only when the field is `ok` and
at least one target-market evidence item has `variant_match` `exact` (computed server-side) or no
`variant_match` at all (legacy evidence). `unclear`, `unknown`, `different` and `unbound` never end an attempt early,
and never make a field usable either.

Metrics:

- `early_resolution_count`: the number of early exits.
- `turn_budget_skipped_by_early_resolution`: the sum of turn budget left when exiting.
- `turns_saved_by_early_resolution`: deprecated alias of the count.

**Budget.** `field_cut_short_by_budget` names a field only when one of its attempts had started and was
stopped by the global cap. If attempt 1 completed and attempt 2 never started, only
`stopped = "max_total_steps"` reports it.

**One evaluator.** `field_recovery.current_evaluation()` (`evaluate_fields` over all events) is used by
live recovery, the research bundle and run reconstruction. Reconstructed recovery summaries are history
only, and their current states come from that evaluator. Supplementary quote/note values carried by
`evidence_reused` events are folded back into the canonical evidence item on reconstruction.

### Retries, timeouts and cost

`GLM_CHAT_MAX_ATTEMPTS` and `GLM_SEARCH_MAX_ATTEMPTS` count **total** HTTP attempts: 2 means one
try plus at most one retry. Each attempt is logged as its own `api_call` or `api_error` event, with
`attempt`, `max_attempts`, `request_kind`, `timeout`, `usage_unknown` and `will_retry`.

A read timeout or dropped connection has an unknown billing outcome: the provider may have done the
work. Results therefore report `api_attempts`, `chat_attempts`, `search_attempts`, `timeout_count`
and `unknown_usage_attempts`. Cost is computed only from the usage returned by successful responses.
When some attempts have unknown usage, the UI says: *Recorded API cost from returned usage; provider
billing may be higher for timed-out requests.*

### Incomplete runs in the UI

For each `record_id` in `batch.json`, the UI loads `result.json`. If that file is missing but
`input.json` or `events.jsonl` exist, it rebuilds the run from events (`src/storage/run_loader.py`):

- status, timings and the last successful step;
- tool calls, evidence and touched documents (from the cache, the run's `documents/` copy or the
  logged `document` events);
- usage confirmed by `model_response` events, API attempts and timeouts;
- raw model responses with their reasoning, and the partial research bundle.

Such runs carry the banner "This run did not produce a final result.json. The research trace below was
recovered from events.jsonl." Results shows "Not produced" plus the partial research, and Benchmark
still counts their work. Nothing on disk is modified.

### Finalize an existing run without repeating research

```bash
python -m src.cli --finalize-existing --batch-id 20261001T185509Z-glm-5.3-one --record-id 101122 --dry-run
python -m src.cli --finalize-existing --batch-id 20261001T185509Z-glm-5.3-one --record-id 101122
```

The dry run prints the bundle size and calls nothing. The real run builds the bundle from the saved
events and cache, then makes only the finalization call, using `GLM_FINALIZER_MODEL`, else `GLM_MODEL`
(with `FINAL_ASSEMBLY=deterministic`, the default, the output is assembled in code exactly as in a live run and that
call is only the narration). There is no search and no fetch. History is preserved:

- events are appended to `events.jsonl`, with sequence numbers continuing;
- an existing `result.json` is first copied to `result.pre-recovery-<stamp>.json`;
- the request and bundle are saved under `recovery/<stamp>/`;
- the new `result.json` is marked `recovered: true`.

A run that already has structured output is only re-finalized with `--force`.

### Market and variant provenance

The prompts ask the model to (the runtime then computes market, binding and authority itself; see
[Evidence admission](#evidence-admission-variant-binding-and-source-authority)):

- match the exact government variant and treat trim-name mappings as inferences;
- keep the market of every value;
- for this Israeli target, prefer Israeli-market values when markets differ, keeping foreign values
  as alternatives and conflicts;
- avoid carrying trim-specific values across trims;
- keep EV energy consumption (`energy_consumption_kwh_100km`) separate from fuel l/100km;
- cite real evidence records (`e1`, ...) rather than document ids.

The output has a `provenance_summary` (Israeli-market values, foreign-market values, inferred variant
mappings, conflicts, unresolved fields) and per-field `market`, `provenance` and `alternatives`.
`data/variant_notes.json` holds operator context for specific variants (for #44: the likely local name
"Core Performance AWD", marked as an inference). It is shown to the model as context and never checked
in code. The Benchmark tab only counts these observations, for example cited ids that are not stored
evidence.

## Run provenance

Every run stores the complete effective configuration, without the API key, in `batch.json`, in the
`run_started` event and in `result.json` (`glm_config`, `effective_config`):

- the research model and finalizer model;
- base URL, chat path, search path and search engine;
- attempt limits and timeout;
- thinking setting, `max_tokens`, temperature, `tool_choice` and the full extra request body;
- search backend, agent/context limits, tool caps, prompt and bundle versions;
- the timestamp it was recorded.

`result.json` adds the `stop_reason`, `research_steps`, `api_stats`, `usage_research`,
`usage_finalizer`, `finalization` (`finalizer_input_chars`, tokens, latency, model) and
`research_tracking` (duplicate searches, fetches and inspections).

Each GLM HTTP attempt is logged as `api_call` (endpoint, status, latency) or `api_error`. An `api_error` keeps
the status, the raw response body (up to 64 KiB) and the request-id and rate-limit response headers. A failed
run stores the final error in `result.json` as `api_error`. Documents a run touched are copied into
`runs/<batch>/<record_id>/documents/`, so the run is self-contained even after the shared cache changes.

## Cost

Observational only. Defaults (official Z.ai pricing as provided on 2026-10-01, in `src/pricing.py`):

| Model | Input / 1M tokens | Output / 1M tokens |
| --- | --- | --- |
| `glm-5.3` | $1.40 | $4.40 |
| `glm-5.3-flash` | $0.15 | $0.50 |
| `glm-5.3-flashx` | $0.37 | $1.25 |
| Web Search | $0.01 per call | |

- **Overrides:** values can be overridden by environment variable or in the sidebar.
- **Unknown model:** a model id with no default reports token cost as n/a.
- **Search calls:** only billable GLM `web_search` calls count. Cache hits don't, and
  `search_official_domains` counts one call per domain.
- **Cached tokens:** these are billed at the full input price, because no cached-input price was supplied.
- **Reproducibility:** the prices used are stored with each run, and the Benchmark tab always uses them.

## One-vehicle run: vehicle #44

Benchmark vehicle #44: XPeng G6, 2026, MAX, model code NSGHA, upstream_record_id `101122`, 486 hp, AWD.
The CLI runs exactly one vehicle and stops.

```bash
export GLM_API_KEY="..."            # never commit it
export GLM_MODEL="glm-5.3-flash"
python -m src.cli --dry-run         # prints vehicle, Level 1.5 source, effective config and pricing; calls nothing
python -m src.cli                   # researches 101122 once, prints a summary, then exits
```

Optional experiment: research with Flash and finalize with GLM-5.3, with no code change.

```bash
export GLM_MODEL="glm-5.3-flash"
export GLM_FINALIZER_MODEL="glm-5.3"
```

In the UI, choose **One vehicle**; #44 is preselected. The run lands in `runs/<batch>/101122/`:

- `input.json`: the Level 1.5 payload.
- `events.jsonl`: every model turn (with `reasoning_content` and latency), tool call and full tool result
  (including complete search results), document, evidence item, `api_call` and `api_error`.
- `documents/`: the fetched documents.
- `result.json`: final (or partial) result, evidence, tool calls, token usage per phase, API attempts and
  timeouts, `search_api_calls`, latency, cost, `glm_config` and any raw `api_error`. It is written on
  every exit path.
- `finalizer_request.json`: the exact compact messages sent to the finalizer.

## Level 1.5 data

The authority is the spec's Appendix D query (`src/db.py:LEVEL15_SQL`): `v.*` from
`public.catalog_variants_current` plus `public.catalog_variant_equipment(...)`,
ordered by the fixed benchmark order. That view is granted to a read-only database
role rather than the REST roles, so the loader connects with `DATABASE_URL` and
runs in a read-only transaction.

Without `DATABASE_URL`, `data/benchmark_v1_level15_snapshot.json` is used: the same
query's output for the 50 IDs, captured on 2026-10-01. Every batch records which
source it used. If `DATABASE_URL` is set and the query fails, the run stops. It never
falls back to the snapshot silently.

The model receives the whole row (`raw_row`) plus a grouped view: identity, engine &
drivetrain, structure, environment, and safety with all 19 assist systems and the 5
installation sources.

## Benchmark v1

`data/benchmark_v1_ids.json` holds the 50 fixed variants in canonical order.
Toyota has 8; Audi, BMW, Mercedes, Hyundai, Cadillac and XPeng have 7 each. Years
run 2017–2026 and cover ICE, hybrid, PHEV and BEV, private and commercial, 4X2 and
4X4. Nothing is sampled at run time. Run one vehicle, one manufacturer or all 50.
Vehicles run sequentially against one shared document/search cache, so reuse across
vehicles is measured honestly.

## Tools

| Tool | Role |
| --- | --- |
| `search_web` | General search; results in backend order, no reliability ranking. |
| `search_official_domains` | Same, biased to manufacturer/importer domains (defaults per manufacturer, or any list). |
| `fetch_url` | Downloads a URL; stores status, headers, content-type, final URL and body (detects PDFs). |
| `fetch_pdf` | Downloads a PDF; stores bytes, extracted text (pdfplumber) and metadata. |
| `render_page` | Headless Chromium render for JS/SPA pages; returns text and links. Offered only when Playwright is installed. |
| `extract_html` | Headings, lists, links and paged readable text. |
| `extract_tables` | HTML tables, `<dl>` spec grids and PDF tables as rows/columns. |
| `find_in_document` | Focused search in visible text, raw source or embedded structured data. |
| `get_structured_data` | JSON-LD, `__NEXT_DATA__`, `application/json`, window state objects, meta tags, microdata. |
| `get_cached_document` | Re-reads a document already downloaded by any run. |
| `store_evidence` | Stores field, value, source, quote after deterministic admission (retrieved source, quote in it, value stated); computes binding, market, authority, typed value, validity date; returns an evidence id or the rejection reasons. |

## Output

The output is one JSON object with `summary`, `variant_identity`, `fields`
(`{name: {value, unit, market, provenance, alternatives, notes, evidence_ids}}`), `conflicts`,
`provenance_summary`, `additional_findings`, `level3` and `research_trace`. By default it is assembled in code
(see [Deterministic Final Assembly](#deterministic-final-assembly); each field entry also carries its `state`).
With `FINAL_ASSEMBLY=llm` the finalizer model writes it: there is no forced found/not-found enum, nulls, free text,
multiple values and unrequested fields are all kept and shown, and if the final reply is not JSON, the finalizer gets
one repair request (still compact context), and the raw text is kept either way.

## Concurrent batches

`run_batch` runs the selected vehicles on a thread pool (`BATCH_MAX_WORKERS`, default 50, capped at the
number of vehicles) and returns results in benchmark order; `max_workers=1` keeps the historical
sequential loop. A failing vehicle becomes an `error` result and never cancels the others. A Stop /
Ctrl+C cancels the batch: queued vehicles never start, running ones stop at their next safe point
(before a model call, before a tool call, or while queued for a slot) and persist an `interrupted`
partial result in their own folder; there is no automatic re-run of whole vehicles.

Provider limits are guarded per HTTP attempt by a shared `ConcurrencyController` (`src/concurrency.py`):

| pool | provider limit | default operational limit |
|---|---:|---:|
| `glm-5.3-flash` | 50 | 48 |
| `glm-5.3-flashx` | 20 | 18 |
| `glm-5.3` | 5 | 5 |
| any other model id | unknown | `GLM_UNKNOWN_MODEL_MAX_INFLIGHT` (1) |
| Search-Prime | 5 | `GLM_SEARCH_MAX_INFLIGHT` (5) |

A slot is acquired right before `session.post` and released in `finally` when the response or error
is back: never held during a retry sleep, tool execution, page fetch, PDF parsing or evaluation. The
pool is chosen by the ACTUAL model of the request, so a `glm-5.3` finalizer draws from its own 5-slot
pool while research uses Flash. `GLM_CHAT_MAX_INFLIGHT` / `GLM_CHAT_MAX_INFLIGHT_BY_MODEL` and the UI can
change the operational limits, never above the provider limit.

Each vehicle worker gets its own `GLMClient` and HTTP session (`vehicle_client_factory`); only the
immutable settings and the controller are shared, so one vehicle's API events can never reach another
vehicle's `events.jsonl` (a client already attached to a running vehicle refuses a second one). The
shared document cache is single-flight per key (one network call per URL or search query, the other
workers wait and reuse it, different keys run concurrently) and writes files atomically.

`batch.json` records `concurrency` (workers, operational and provider limits) and, at the end,
`concurrency_observed`: `peak_vehicle_workers`, `peak_chat_inflight_by_model`, `peak_search_inflight`,
`chat_queue_wait_count/_ms`, `search_queue_wait_count/_ms` and the cross-vehicle single-flight reuses.

## UI

The dashboard (`app.py`, `src/ui/dashboard.py`) is restrained and works on a phone (columns stack, long text wraps,
no horizontal page scroll):

- **Research target** — scope (one vehicle, a manufacturer, the whole Benchmark v1 sample) and *Start research*.
- **System** — GLM (configured / reachable: an unauthenticated request to the base URL, no key sent), Search,
  Persistent storage (writable, Railway Volume detected) and the Level 1.5 source. Problems are precise
  ("Missing GLM_API_KEY") and block *Start research*.
- **Active run** — the real pipeline with phase status instead of a spinner or invented percentages:

  ```
  ✓ Source acquisition        1m 02s
  ✓ Deterministic harvest     0s
  ● Document sweep            Running · 14s
  ○ Tail recovery             Waiting
  ○ Finalization              Waiting
  ```

  with what is happening now ("Model is working", "Searching the web: …") and the counters the run actually
  recorded: sources, target-market sources, candidates, candidate fields, resolved fields (evidence-backed state,
  not a correctness score), research turns, searches. *Stop run* stops at the next safe point. The panel refreshes
  itself every 2 s while the run is active.
- **Current run** (right column) — run id, status, phase, model, start time, elapsed, sources, candidates, resolved
  fields, search calls, model calls.
- **Result** — summary and a field table (value, unit, market, evidence state, provenance, evidence count),
  conflicts and additional findings. *Run report* shows the observability summary: total and per-phase durations
  (acquisition, harvest, sweep, recovery, finalizer), search calls, model calls by phase, fetched / official /
  target-market documents, candidates and candidate fields, admitted and rejected evidence, resolved fields, stop
  reason and recorded cost. It is also stored in `run_state.json`.
- **Failures** — a clean card: where it stopped, why in plain words (provider timeout, rate limit, credentials,
  provider error, search failure, malformed response, storage, server restart, user stop), what was preserved, and
  the safe actions (*Retry finalization* / *Finalize from preserved research*, *Restart research*). Technical
  details (redacted) are in an expander; stack traces only go to the server log.
- **Run history** (sidebar) — every run with target, status, duration, resolved fields and run id; selecting one
  restores its result and diagnostics.
- **Technical details** — while running: the Hebrew field-progress table and the raw activity log; afterwards the
  previous per-vehicle diagnostics (human view, JSON, partial research, evidence, tool calls, documents, model
  responses, Level 1.5 input, config & cost, API attempts, events, field recovery, the Hebrew candidate matrix),
  the Documents, Results and Benchmark views, and the raw run state.
- **Advanced settings** (sidebar) — the previous sidebar knobs (models, endpoints, attempts, concurrency, research
  budget, recovery, Level 3, temperature / thinking / max_tokens / extra body, pricing, Level 1.5 source), applied to
  the next run. Defaults come from the environment.

## Files

All paths are under `TRIPY_DATA_DIR` (`/data` on Railway): `runs/` below, the shared cache at `cache/` (shown
here with its historical `runs/_cache` name, still used when `MILO_RUNS_DIR` is set).

```
runs/<batch>/run_state.json            run lifecycle: status, stage, owner/heartbeat, jobs, error summary, final report
runs/<batch>/batch.json                configuration (model, prompt version, tools, data source, concurrency)
runs/<batch>/<record_id>/input.json    Level 1.5 payload sent to GLM
runs/<batch>/<record_id>/events.jsonl  every model turn, tool call/result, document and evidence
runs/<batch>/<record_id>/result.json   final or partial output, evidence, tool calls, usage, cost, glm_config, api_error
runs/<batch>/<record_id>/finalizer_request.json   exact compact messages sent to the finalizer
runs/<batch>/<record_id>/recovery/<stamp>/        --finalize-existing request and bundle
runs/<batch>/<record_id>/documents/    copies of every document the run touched
runs/<batch>/<record_id>/training_feedback.jsonl   labelled engineering feedback of the run
runs/_cache/                           shared documents + search results (+ derived tables / candidates)
runs/_cache/memory/                    verified facts (one file per fact), negative routes and recovery yield (per run)
runs/_feedback/                        --export-feedback: training_feedback.jsonl / .csv / _summary.json
runs/<batch>/<record_id>/diagnostics.json / .jsonl   acquisition + document-sweep diagnostics (src/diagnostics.py)
runs/<batch>/diagnostics/              benchmark.json, per_vehicle.jsonl / .csv of the batch
```

## Tests

```bash
pip install pytest
python -m pytest
```

Tests use fake HTTP sessions and a scripted GLM client and never touch the network. They include:

- a synthetic fixture of the GLM-5.3 baseline failure (`tests/fixtures/baseline_runs`, regenerate with
  `python tests/fixtures/make_baseline_fixture.py`): 30 research steps, then repeated finalize-call
  timeouts and no `result.json`;
- Streamlit `AppTest` smoke tests (including a parallel live-dashboard batch);
- concurrency (per-model and Search-Prime pools, per-attempt slots, hook isolation, failure isolation,
  cancellation), cache single flight, the field dictionary (all 45 fields, positive and false-positive
  phrases), deterministic harvesting, the layered pipeline (Cadillac LYRIQ-style fixture in
  `tests/fixtures/cadillac_lyriq.py`), the finalization checkpoint and the Hebrew dashboard state;
- the reliability foundation (`tests/test_reliability_foundation.py`, Corolla fixture in
  `tests/fixtures/corolla_touring.py`): 2.0-vs-1.8 cargo contamination, server binding over model claims,
  e-CVT gear count, boolean statements, notes, typed ranges, propulsion applicability, source authority,
  valid_as_of and the consistency checks;
- the contract-mode early-"done" guard and the document card (`tests/test_acquisition_guard.py`), parser-gap
  telemetry (`tests/test_parser_gaps.py`), run profiles, env overrides, saved-config round trips and the A/B series
  launcher (`tests/test_run_profiles.py`);
- clustered tail recovery (`tests/test_tail_recovery.py`, offline benchmark in `tests/fixtures/corolla_tail.py`):
  conflict classes, market portability and its veto, triage, local-first, novelty-based turn extensions,
  search budgets in provider calls, breadth-first clusters and the legacy-vs-cluster comparison;
- verified fact reuse and feedback (`tests/test_reuse_feedback.py`, cold/warm benchmark in
  `tests/fixtures/corolla_family.py`): scope keys, stale / colliding records, cross-powertrain re-admission,
  negative memory never touching field state, concurrent memory writes and exports, the required Corolla
  training cases and strict labels;
- orchestration (`tests/test_orchestration.py`, offline benchmark in `tests/fixtures/corolla_orchestration.py`,
  record 38626 shaped after the real GLM-5.3-Flash run): the no-artifact stop and what does / does not reset it,
  acquisition sufficiency and source priority, batch inspection (several fields, offsets, merged snippets, no
  evidence, no state, no model), settled fields removed, one sweep vs deterministic cluster chunks (nothing dropped),
  the presentation limit vs storage, routing without votes, recovery still breadth-first after the sweep, no paid
  stage when nothing is open, and per-phase settings. `python tests/fixtures/corolla_orchestration.py` prints the
  report; the same file runs unchanged against an older checkout for a before/after comparison.
- the Railway migration (`tests/test_railway_runstate.py`, `tests/test_dashboard_app.py`): persistent path
  configuration and storage detection, run-state serialization and the repository, pipeline phase transitions,
  reconnect (incremental event replay equals a fresh replay), background execution, duplicate-run prevention,
  cancellation, shutdown, orphaned-run restoration after a restart, finalization retry from the checkpoint, failure
  explanations, configuration validation and redaction, the Railway start configuration, `.env.example`
  completeness, and AppTest renders of the dashboard from durable state.

`.github/workflows/tests.yml` runs them on every push and pull request (no secrets, no deployment).

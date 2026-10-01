# MILO — GLM Web Research Benchmark v1

A small, isolated Streamlit demo that measures one thing: given the full Level 1.5
record of a known vehicle variant and a strong set of web tools, how much useful
additional information can a GLM model find, organize and return?

It is **not** part of the MILO pipeline, does not protect the catalog and does not
promote anything to production.

```
Supabase Level 1.5 → benchmark loader → GLM agent loop → research tools → evidence store → structured result → Streamlit UI
```

## No epistemic gate in code

- No verifier, no domain allowlist, no confidence threshold, no rejection of
  unofficial sources, no conflict resolution in code, no deterministic variant
  matching after the model.
- `search_official_domains` is a convenience bias, not a gate.
- `store_evidence` is a journal, not a judge.
- Only technical validation exists: parseable JSON / tool arguments, `http(s)`
  URLs, timeouts and a response size cap.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill GLM_API_KEY (and optionally GLM_MODEL, DATABASE_URL)
set -a; . ./.env; set +a
streamlit run app.py
```

Settings can also come from Streamlit secrets (same names) or the sidebar. The
GLM defaults follow the official Z.ai documentation (see [Z.ai API](#zai-api-confirmed-and-unverified)),
and every one of them stays configurable.

| Setting | Purpose |
| --- | --- |
| `GLM_API_KEY` | Required. |
| `GLM_MODEL` | Any GLM chat model id. Also editable in the UI, so different GLM models can be compared. |
| `GLM_BASE_URL` | Default `https://api.z.ai/api/paas/v4`; use `https://open.bigmodel.cn/api/paas/v4` for bigmodel. |
| `GLM_CHAT_PATH` | Chat completions endpoint, relative to the base URL or a full URL (default `chat/completions`). |
| `GLM_SEARCH_PATH` | Web search endpoint, relative to the base URL or a full URL (default `web_search`). |
| `SEARCH_BACKEND` | `glm` (GLM `web_search` API) or `duckduckgo` (keyless HTML search). |
| `GLM_SEARCH_ENGINE` | Engine name for the standalone search API (default `search-prime`). |
| `GLM_THINKING` | Empty = provider default (nothing sent); `enabled` / `disabled` sends `{"thinking": {"type": ...}}`. |
| `GLM_EXTRA_BODY` | JSON merged into every chat request. |
| `GLM_PRICE_INPUT_PER_MTOK` / `GLM_PRICE_OUTPUT_PER_MTOK` / `GLM_PRICE_WEB_SEARCH_PER_CALL` | Optional overrides of the built-in price defaults (see [Cost](#cost)). |
| `DATABASE_URL` | Postgres URL for the Level 1.5 source (see below). |
| `MILO_RUNS_DIR` | Where run logs and the shared cache live (default `runs/`). |

`render_page` needs Playwright (`pip install playwright && playwright install chromium`).
Without it the tool reports `render_unavailable` and the model can use `fetch_url`.

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

- The thinking request shape `{"thinking": {"type": "enabled" | "disabled"}}` and how each model reacts to it.
- The `search_recency_filter` value sent by default (`noLimit`).
- That the cached-token detail is reported as `prompt_tokens_details.cached_tokens`.
- Error bodies, status codes and rate-limit behaviour. Retries cover 429/500/502/503/504, and every raw error is stored.
- Whether a response always echoes `tool_calls` exactly as we send them back. Assistant messages are echoed with
  `content` and `tool_calls` only.

**Not used: Z.ai Web Reader.** The experiment tests our own fetch / render / extract / PDF / document tools, so
page reading is never delegated back to Z.ai.

## Run provenance

Every run stores the complete effective GLM configuration, without the API key, in `batch.json`, in the
`run_started` event and in `result.json` (`glm_config`). That covers the model, base URL, chat path, search
path, search engine, thinking setting, `max_tokens`, temperature, `tool_choice`, the full extra request body,
the search backend, and the timestamp it was recorded.

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

## First real run: vehicle #44 only

The integration handshake is benchmark vehicle #44: XPeng G6, 2026, MAX, model code NSGHA,
upstream_record_id `101122`, 486 hp, AWD. It runs exactly one vehicle and stops.

```bash
export GLM_API_KEY=...            # never commit it
export GLM_MODEL=glm-5.3          # or another GLM model id
python -m src.cli --dry-run       # prints vehicle, Level 1.5 source, effective config and pricing; calls nothing
python -m src.cli                 # researches 101122 once, prints a summary, then exits
```

In the UI, choose **One vehicle**; #44 is preselected. The run lands in `runs/<batch>/101122/`:

- `input.json`: the Level 1.5 payload.
- `events.jsonl`: every model turn (with `reasoning_content` and latency), tool call and full tool result
  (including complete search results), document, evidence item, `api_call` and `api_error`.
- `documents/`: the fetched documents.
- `result.json`: final structured result, evidence, tool calls, token usage, `search_api_calls`, latency, cost,
  `glm_config` and any raw `api_error`.

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
| `render_page` | Headless Chromium render for JS/SPA pages; returns text and links. |
| `extract_html` | Headings, lists, links and paged readable text. |
| `extract_tables` | HTML tables, `<dl>` spec grids and PDF tables as rows/columns. |
| `find_in_document` | Focused search in visible text, raw source or embedded structured data. |
| `get_structured_data` | JSON-LD, `__NEXT_DATA__`, `application/json`, window state objects, meta tags, microdata. |
| `get_cached_document` | Re-reads a document already downloaded by any run. |
| `store_evidence` | Logs field, value, source, quote and the model's note; returns an evidence id. |

## Output

The model is asked for one JSON object with `summary`, `fields`
(`{name: {value, unit, notes, evidence_ids}}`), `conflicts`,
`additional_findings`, `level3` and `research_trace`. There is no forced
found/not-found enum. Nulls, free text, multiple values and unrequested fields are
all kept and shown. Parsing only exists to render the UI. If the final reply is not
JSON, the app asks once for the same content as JSON and keeps the raw text either way.

## UI

- **Run**: per vehicle, a live tool-call feed while running; afterwards the human
  view, JSON, evidence, tool calls, documents, Level 1.5 input and the full event log.
- **Documents**: the shared cache, with URL, content-type, extraction path,
  bytes/text size and a text preview.
- **Results**: every field the model returned, in long or wide form, with no
  pass/fail.
- **Benchmark**: coverage, fields filled, extra fields, evidence, sources, tool
  calls, cache hits, conflicts, findings, time, tokens and cost. These are
  observations, not scores. Also has a comparison across batches (model, prompt
  version, search backend).

## Files

```
runs/<batch>/batch.json                configuration (model, prompt version, tools, data source)
runs/<batch>/<record_id>/input.json    Level 1.5 payload sent to GLM
runs/<batch>/<record_id>/events.jsonl  every model turn, tool call/result, document and evidence
runs/<batch>/<record_id>/result.json   final output, evidence, tool calls, usage, cost, glm_config, api_error
runs/<batch>/<record_id>/documents/    copies of every document the run touched
runs/_cache/                           shared documents + search results
```

## Tests

```bash
pip install pytest
python -m pytest
```

Tests use fake HTTP sessions and a scripted GLM client and never touch the network.
`.github/workflows/tests.yml` runs them on every push and pull request (no secrets, no deployment).

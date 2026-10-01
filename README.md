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
GLM defaults (base URL, endpoint paths, search engine name, request/response
field names) have not been verified against the live API yet. Every one of them
is configurable.

| Setting | Purpose |
| --- | --- |
| `GLM_API_KEY` | Required. |
| `GLM_MODEL` | Any GLM chat model id. Also editable in the UI, so different GLM models can be compared. |
| `GLM_BASE_URL` | Default `https://api.z.ai/api/paas/v4`; use `https://open.bigmodel.cn/api/paas/v4` for bigmodel. |
| `GLM_CHAT_PATH` | Chat completions endpoint, relative to the base URL or a full URL (default `chat/completions`). |
| `GLM_SEARCH_PATH` | Web search endpoint, relative to the base URL or a full URL (default `web_search`). |
| `SEARCH_BACKEND` | `glm` (GLM `web_search` API) or `duckduckgo` (keyless HTML search). |
| `GLM_SEARCH_ENGINE` | Engine name for the GLM search API (`search-prime`, `search_std`, `search_pro`, …). |
| `GLM_EXTRA_BODY` | JSON merged into every chat request, e.g. `{"thinking": {"type": "enabled"}}`. |
| `GLM_PRICE_INPUT_PER_MTOK` / `GLM_PRICE_OUTPUT_PER_MTOK` | USD per 1M tokens for the cost column (0 → n/a). |
| `DATABASE_URL` | Postgres URL for the Level 1.5 source (see below). |
| `MILO_RUNS_DIR` | Where run logs and the shared cache live (default `runs/`). |

`render_page` needs Playwright (`pip install playwright && playwright install chromium`).
Without it the tool reports `render_unavailable` and the model can use `fetch_url`.

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
runs/<batch>/<record_id>/result.json   final output, evidence, tool calls, usage and metrics
runs/_cache/                           shared documents + search results
```

## Tests

```bash
pip install pytest
python -m pytest
```

Tests use fake HTTP sessions and a scripted GLM client and never touch the network.

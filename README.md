# MILO — GLM Web Research Benchmark v1

A small, isolated Streamlit demo that measures one thing: given the full Level 1.5
record of a known vehicle variant and a strong set of web tools, how much useful
additional information can a GLM model find, organize and return?

It is **not** part of the MILO pipeline, does not protect the catalog and does not
promote anything to production.

```
Supabase Level 1.5 → benchmark loader → GLM research phase → research tools → evidence store / document cache
  → failed requested-field detection → targeted field retries → compact research bundle
  → GLM finalization phase → structured result → Streamlit UI
```

## No epistemic gate in code

- No verifier, no domain allowlist, no confidence threshold, no rejection of
  unofficial sources, no conflict resolution in code, no deterministic variant
  matching after the model.
- `search_official_domains` is a convenience bias, not a gate.
- `store_evidence` is a journal, not a judge.
- Only technical validation exists: parseable JSON / tool arguments, `http(s)`
  URLs, timeouts and a response size cap.
- Operational controls (duplicate/novelty tracking, research budget, context compaction,
  attempt limits) only manage cost and context. None of them judges whether a source or
  value is correct, and the model can always search again.
- Market/variant provenance is prompt guidance plus extra `store_evidence` fields (`market`,
  `variant`). Values from other markets are never blocked; they are kept with their source.

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
| `GLM_MODEL` | Research model id (e.g. `glm-5.3-flash`), sent unchanged as the API `model`. Also editable in the UI. |
| `GLM_FINALIZER_MODEL` | Optional. Model id for the compact finalization call only; empty = `GLM_MODEL`. |
| `GLM_CHAT_MAX_ATTEMPTS` | Total chat/completions HTTP attempts per request (default **2**; 1 = no retry). |
| `GLM_SEARCH_MAX_ATTEMPTS` | Total web_search HTTP attempts per request (default **3**). |
| `GLM_CHAT_TIMEOUT_S` | Read timeout per attempt in seconds (default 240). |
| `AGENT_MAX_STEPS` | Soft research budget in model turns (default **12**). |
| `ENRICHMENT_FIELDS` / `ENRICHMENT_SCHEMA_PATH` | Requested enrichment fields (default: `data/enrichment_fields.json`). |
| `FIELD_RECOVERY_ENABLED`, `FIELD_RECOVERY_MAX_ATTEMPTS`, `FIELD_RECOVERY_MAX_STEPS`, `FIELD_RECOVERY_MAX_TOTAL_STEPS` | Targeted field retries (defaults `true`, `2`, `4`, `24` turns per vehicle; `0` = no cap). See [Targeted field recovery](#targeted-field-recovery). |
| `AGENT_NO_NEW_RESEARCH_TURNS` | Finalize after N consecutive turns with no new research artifact (default 2; 0 = off). |
| `AGENT_MAX_TOOL_OUTPUT_CHARS`, `AGENT_KEEP_RECENT_TOOL_RESULTS`, `AGENT_COMPACT_TOOL_OUTPUT_CHARS`, `AGENT_FINALIZER_BUNDLE_MAX_CHARS` | Context limits (see [Research and finalization](#research-and-finalization)). |
| `TOOL_PREVIEW_CHARS`, `TOOL_MAX_TEXT_CHARS`, `TOOL_MAX_LINKS`, `TOOL_MAX_TABLE_ROWS`, `TOOL_MAX_TABLES` | Tool output caps. The full document always stays in the cache. |
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
- Error bodies, status codes and rate-limit behaviour. Retries cover timeouts, connection errors and
  429/500/502/503/504 within the configured attempt limits, and every raw error is stored.
- Whether a response always echoes `tool_calls` exactly as we send them back. Assistant messages are echoed with
  `content` and `tool_calls` only.

**Not used: Z.ai Web Reader.** The experiment tests our own fetch / render / extract / PDF / document tools, so
page reading is never delegated back to Z.ai.

## Research and finalization

The GLM-5.3 baseline (batch `20261001T185509Z-glm-5.3-one`, vehicle #44) showed that research works but
re-sending the whole 30-step conversation for the final answer does not: the final call timed out
repeatedly and no result was saved. Each vehicle run now has two explicit phases:

1. **Research phase** (`GLM_MODEL`). Tool-calling turns up to the soft budget `AGENT_MAX_STEPS`
   (default 12). Fetch tools store the full document in the cache and return only a `document_id`,
   metadata and a short preview. The model is told to query stored documents (`find_in_document`,
   `extract_tables`, `get_structured_data`, `extract_html`) before searching again. Older tool results
   in the conversation are compacted to stubs that keep `document_id`, URL, title, query and result URLs.
   Repeated searches, re-fetches and identical document queries are noticed and pointed out to the
   model. Research stops with a recorded `stop_reason`: `model_finished`, `max_steps`,
   `no_new_research` (N turns in a row with no new document, evidence, source or document query),
   `user_cancelled`, `api_failure` or `research_exception`.
2. **Finalization phase** (`GLM_FINALIZER_MODEL`, default `GLM_MODEL`). If the research model did not
   already return valid JSON, ONE no-tools call receives a compact research bundle (`src/bundle.py`):
   the Level 1.5 identity, the targets, every stored evidence item (with market/variant), candidate facts
   grouped by field, fields with several stored values (no winner picked), conflicts the model noted,
   document metadata, the document excerpts the model actually read, a concise action list, targets with
   no evidence and the last model notes. It is capped at `AGENT_FINALIZER_BUNDLE_MAX_CHARS`. The raw
   conversation is never sent. The exact request is saved as `finalizer_request.json`.

Statuses: `completed`, `max_steps_finalized`, `no_new_research_finalized`, `completed_unparsed`,
`finalization_failed`, `research_failed`, `interrupted`, `recovered_finalized`, plus `incomplete` for
runs reconstructed from events.

**Every started run leaves a `result.json`**, including research failures, finalizer timeouts and
Ctrl+C. It holds the status, the errors, all tool calls, evidence, documents, counters, usage per
phase, API attempt statistics, the partial research bundle and the last model content, and never any
fabricated structured fields. Ctrl+C makes no further model call. The CLI saves the partial result
and exits with code 130.

### Targeted field recovery

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
- `foreign_market_only`: every candidate is explicitly marked as another market and the model has not
  declared the field found;
- `variant_not_exact`: every candidate is explicitly marked as another trim (`variant_match=different`)
  and the model has not declared it found;
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
re-evaluated; a still-failed field gets attempt #2.

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
- the market, variant and `variant_match`.

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
`targets_without_stored_evidence`. There is no automatic resume yet.

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
events and cache, then makes only the finalization call, using `GLM_FINALIZER_MODEL`, else `GLM_MODEL`.
There is no search and no fetch. History is preserved:

- events are appended to `events.jsonl`, with sequence numbers continuing;
- an existing `result.json` is first copied to `result.pre-recovery-<stamp>.json`;
- the request and bundle are saved under `recovery/<stamp>/`;
- the new `result.json` is marked `recovered: true`.

A run that already has structured output is only re-finalized with `--force`.

### Market and variant provenance

The prompts ask the model to:

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
| `render_page` | Headless Chromium render for JS/SPA pages; returns text and links. |
| `extract_html` | Headings, lists, links and paged readable text. |
| `extract_tables` | HTML tables, `<dl>` spec grids and PDF tables as rows/columns. |
| `find_in_document` | Focused search in visible text, raw source or embedded structured data. |
| `get_structured_data` | JSON-LD, `__NEXT_DATA__`, `application/json`, window state objects, meta tags, microdata. |
| `get_cached_document` | Re-reads a document already downloaded by any run. |
| `store_evidence` | Logs field, value, source, quote and the model's note; returns an evidence id. |

## Output

The model is asked for one JSON object with `summary`, `variant_identity`, `fields`
(`{name: {value, unit, market, provenance, alternatives, notes, evidence_ids}}`), `conflicts`,
`provenance_summary`, `additional_findings`, `level3` and `research_trace`. There is no forced
found/not-found enum. Nulls, free text, multiple values and unrequested fields are
all kept and shown. Parsing only exists to render the UI. If the final reply is not
JSON, the finalizer gets one repair request (still compact context), and the raw text is kept either
way.

## UI

- **Run**: per vehicle, a live tool-call feed while running. Afterwards:
  - human view, JSON and partial research;
  - evidence, tool calls and documents;
  - model responses with reasoning;
  - Level 1.5 input, config and cost;
  - API attempts and the full event log.

  Runs without `result.json` are shown from their events, with a banner.
- **Documents**: the documents the batch touched (including incomplete runs) or the whole shared cache,
  with URL, content-type, extraction path, bytes/text size and a text preview.
- **Results**: every field the model returned, with market and provenance, in long or wide form and with
  no pass/fail. Runs without a final JSON show "Not produced" and their partial research.
- **Benchmark**: every run, including failed and incomplete ones. Metrics cover:
  - status and finalization status, stop reason and research steps;
  - research and finalizer model calls;
  - coverage, fields and extra fields;
  - evidence (and evidence with a market), provenance counts, sources and documents;
  - tool calls, cache hits and duplicate work;
  - API attempts, timeouts and unknown-usage attempts;
  - finalizer input size, tokens, time and recorded cost.

  These are observations, not scores. Batches can also be compared (research and finalizer model,
  prompt version, search backend).

## Files

```
runs/<batch>/batch.json                configuration (model, prompt version, tools, data source)
runs/<batch>/<record_id>/input.json    Level 1.5 payload sent to GLM
runs/<batch>/<record_id>/events.jsonl  every model turn, tool call/result, document and evidence
runs/<batch>/<record_id>/result.json   final or partial output, evidence, tool calls, usage, cost, glm_config, api_error
runs/<batch>/<record_id>/finalizer_request.json   exact compact messages sent to the finalizer
runs/<batch>/<record_id>/recovery/<stamp>/        --finalize-existing request and bundle
runs/<batch>/<record_id>/documents/    copies of every document the run touched
runs/_cache/                           shared documents + search results
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
- a Streamlit `AppTest` smoke test.
`.github/workflows/tests.yml` runs them on every push and pull request (no secrets, no deployment).

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

## What code decides, and what it never decides

- No domain allowlist and no rejection of unofficial sources: a forum, an aggregator or a marketplace can
  be cited. `search_official_domains` is a convenience bias, not a gate.
- **Evidence admission is deterministic** (`src/evidence_admission.py`, see
  [Evidence admission](#evidence-admission-variant-binding-and-source-authority)): `store_evidence` stores a
  fact only when the cited document was actually retrieved, the quote occurs in it and states the value
  (literally, or by a schema-approved unit conversion / pattern), and the field applies to the vehicle. No
  model call is involved; a rejected request returns its reasons to the model.
- **Variant identity, market and source authority are computed server-side** from the source itself. The
  model's `variant_match` / `market` are kept as claims (`model_variant_claim`, `model_market_claim`); a
  model's `note` is commentary and never evidence or provenance.
- Code never picks between admitted values, never majority-votes and never resolves a conflict: the field
  evaluator (`field_recovery.current_evaluation`) stays the one field-state authority, and the finalizer
  still decides how to present conflicts.
- Cross-field consistency checks (CO2 vs fuel, curb vs gross mass, rim vs tire, ...) are QA signals only:
  they never create, replace or rank a value.
- Conflict classification (`src/conflict_normalizer.py`) only recognises when same-field values are the same
  number (another unit, or a trim-bound scalar inside a model-line range); it never votes or picks a value.
  Market portability (`src/market_portability.py`) lets an official foreign fact count for the target market
  only where the field's schema policy allows it and no target-market source contradicts it; the source
  market is never rewritten.
- Tail scheduling (`src/tail_planner.py`: triage, clusters, source_yield_score, search hints, novelty) decides
  only what recovery to spend money on, never what is true. Search hints and candidates are not evidence.
- Operational controls (duplicate/novelty tracking, research budget, context compaction, attempt limits)
  only manage cost and context.

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
| `FIELD_RECOVERY_ENABLED`, `FIELD_RECOVERY_MAX_ATTEMPTS`, `FIELD_RECOVERY_MAX_STEPS`, `FIELD_RECOVERY_MAX_TOTAL_STEPS` | Targeted field retries (defaults `true`, `2`, `4`, `24` turns per vehicle; `0` = no cap). See [Targeted field recovery](#targeted-field-recovery-legacy-per-field-mode). |
| `RECOVERY_MODE`, `CLUSTER_MAX_ATTEMPTS`, `CLUSTER_BASE_TURNS`, `CLUSTER_MAX_TURNS`, `CLUSTER_SEARCH_BUDGET` | Tail recovery mode (`cluster` default, or `legacy`) and the cluster attempt limits (defaults `2` attempts, `2` base turns, ceiling `4`, `4` billable searches per attempt). See [Clustered tail recovery](#clustered-tail-recovery-default). |
| `RESEARCH_MEMORY_ENABLED`, `FACT_REUSE_MAX_AGE_DAYS`, `NEGATIVE_ROUTE_MAX_AGE_DAYS` | Cross-run memory: verified fact reuse, negative routes, recovery yield (default on, 365 / 30 days). See [Verified fact reuse](#verified-fact-reuse-negative-research-memory-and-the-feedback-dataset). |
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
`finalization_failed`, `research_failed`, `interrupted`, `recovered_finalized`, `finalization_pending`
(the durable checkpoint, see below), plus `incomplete` for runs reconstructed from events.

### Layered field harvesting

The full per-vehicle pipeline is now:

```
PRIMARY RESEARCH (source acquisition)      model turns, tools
DETERMINISTIC HARVEST                      0 model calls: every document -> candidates for ALL fields
MODEL DOCUMENT SWEEP                       1 model turn; a 2nd ONLY to read turn-1 cached inspections; cached tools only
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
- **Document sweep** (`src/document_sweep.py`): one compact packet for all applicable fields (identity,
  current states, candidate matrix with quotes and hints, stored evidence, cached-document metadata).
  Only `find_in_document`, `extract_tables`, `extract_html`, `get_structured_data`,
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
`unbound` as not about the target (`variant_not_exact`); only `exact` target-market evidence ends a recovery
attempt early. Vocabulary (model families with Hebrew names, body / propulsion / drivetrain terms) lives in
`data/identity_vocabulary.json`.

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

### Clustered tail recovery (default)

After the document sweep, the fields that are still open (the "tail") are recovered in **clusters**, not one
field at a time (`RECOVERY_MODE=cluster`, the default; `RECOVERY_MODE=legacy` keeps the per-field retries
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
- **Local-first.** While a cluster has candidates no model has seen, it first gets one pass with cached-document
  tools only (the packet shows those unseen candidates first), and every round runs such local passes before any
  web attempt. The local pass does not use up the cluster's web attempts. With no unseen local material that
  model pass is skipped and web research starts directly.
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
  domain of a domain search erroring). Later runs see the routes as `known_unproductive_routes`. A cluster whose
  open fields have each failed in at least two earlier, independent runs skips its web attempt
  (`NEGATIVE_MEMORY_SKIP`).
  This is scheduling only: it never creates evidence, never changes a field state, never marks a field
  not_applicable and never resolves a conflict. The routes expire after `NEGATIVE_ROUTE_MAX_AGE_DAYS` (30).
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
| negative route hits | 0 | 1 | 0 | 0 | 0 |
| fields ok (of 14) | 11 | 11 | 11 | 10 | 9 |

A2 reuses the technical facts (tank, battery, torque, performance, dimensions) but not price, warranty, height or
curb weight. It also does not reuse the boot volume, because A left that field foreign-market-only. B reuses nothing,
and its boot stays 581 L, never A's 596 L. The re-run's saved search comes from the model following
`known_unproductive_routes` (the policy model in the benchmark does). The engine itself only skips a cluster after
two earlier, independent failed runs per field. The scale metrics are
`verified_fact_cache_hits`, `negative_route_cache_hits`, search / document / candidate cache hits,
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
- `foreign_market_only`: every candidate is explicitly marked as another market and the model has not
  declared the field found;
- `variant_not_exact`: every candidate is about another variant (`variant_match=different`, by the server-side
  binding veto or the model) or from a source that does not name the model (`unbound`)
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
`targets_without_stored_evidence`. There is no automatic resume yet.

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
though the evaluator may still call such a field usable.

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
events and cache, then makes only the finalization call, using `GLM_FINALIZER_MODEL`, else `GLM_MODEL`.
There is no search and no fetch. History is preserved:

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

The model is asked for one JSON object with `summary`, `variant_identity`, `fields`
(`{name: {value, unit, market, provenance, alternatives, notes, evidence_ids}}`), `conflicts`,
`provenance_summary`, `additional_findings`, `level3` and `research_trace`. There is no forced
found/not-found enum. Nulls, free text, multiple values and unrequested fields are
all kept and shown. Parsing only exists to render the UI. If the final reply is not
JSON, the finalizer gets one repair request (still compact context), and the raw text is kept either
way.

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

- **Live batch dashboard (Hebrew)**: workers only enqueue events; the script thread drains the queue
  every ~200 ms and renders. The header shows completed / active / waiting for the model / waiting for
  search / failed vehicles, live pool occupancy per model and Search-Prime, run time, known cost, model
  calls, searches, 429s, timeouts and the summed Level-2 progress over applicable fields (with the note
  that this measures research coverage, not correctness). Each vehicle card shows the stage, the model
  and whether it is queued or in flight, the current field, *why* this step runs (derived from
  orchestration metadata, never a model call), the current tool action, Level-2 progress over the
  vehicle's APPLICABLE fields (43 for a BEV), with-evidence / needs-follow-up / without-evidence /
  conflicting counts, the Recovery budget, the provider's `reasoning_content` when it was actually
  returned ("חשיבת המודל כפי שהוחזרה מהספק"; "המודל חושב…" while a request is in flight), a Hebrew
  field-progress table (state, values, evidence, markets, last source, recovery attempts, how it was
  completed) and the detailed raw activity log. Field and group labels come from the schema
  (`field_display_name`); raw/debug views keep canonical names. The dashboard makes no API call.
- **Run**: afterwards, per vehicle:
  - human view, JSON and partial research;
  - evidence, tool calls and documents;
  - model responses with reasoning;
  - Level 1.5 input, config and cost;
  - API attempts and the full event log;
  - the Hebrew deterministic candidate matrix (מועמדים) with layered metrics and raw candidate JSON.

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
- clustered tail recovery (`tests/test_tail_recovery.py`, offline benchmark in `tests/fixtures/corolla_tail.py`):
  conflict classes, market portability and its veto, triage, local-first, novelty-based turn extensions,
  search budgets in provider calls, breadth-first clusters and the legacy-vs-cluster comparison;
- verified fact reuse and feedback (`tests/test_reuse_feedback.py`, cold/warm benchmark in
  `tests/fixtures/corolla_family.py`): scope keys, stale / colliding records, cross-powertrain re-admission,
  negative memory never touching field state, concurrent memory writes and exports, the required Corolla
  training cases and strict labels.
`.github/workflows/tests.yml` runs them on every push and pull request (no secrets, no deployment).

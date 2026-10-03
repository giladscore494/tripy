"""GLM agent for one vehicle: research, targeted field retries, then a compact finalization.

    RESEARCH PHASE      tool-calling turns with GLM_MODEL (source acquisition); raw trace ->
                        events.jsonl, documents -> document cache, evidence -> evidence store
          ↓
    DETERMINISTIC       every document touched (in ANY phase) is harvested once for candidates of
    HARVEST             ALL applicable fields (src/candidate_harvest.py). 0 model calls. Candidates
                        are never evidence and never change a field state.
          ↓
    DOCUMENT SWEEP      1 model turn (a 2nd only to read turn-1 cached inspections; max 2) over the candidate
                        matrix with cached-document tools only (src/document_sweep.py): review and
                        promote candidates with store_evidence, catch what the parser missed.
                        0 searches, 0 fetches.
          ↓
    FIELD DETECTION     every REQUESTED enrichment field (schema-driven, src/fields.py) is
                        evaluated from the model's own evidence/declarations (current_evaluation)
          ↓
    TAIL RECOVERY       deterministic tail triage + recovery clusters (src/tail_planner.py; conflicts first
                        classified by src/conflict_normalizer.py): one compact attempt per CLUSTER of related
                        fields, BREADTH-FIRST over clusters, local-first (cached documents only while unreviewed
                        local material exists), adaptive turns (2, up to 4 only on real novelty), a billable
                        search budget per attempt counting provider calls. Every new document is harvested for
                        ALL fields and every cluster is pruned via current_evaluation() before the next paid turn.
                        RECOVERY_MODE=legacy keeps the per-field retries (src/field_recovery.py).
          ↓
    COMPACT BUNDLE      identity, targets, evidence, candidate facts, document metadata,
                        excerpts, concise actions, missing targets (src/bundle.py)
          ↓
    CHECKPOINT          result.json with status finalization_pending is written atomically and durably
                        BEFORE the paid finalizer request starts (a hard process death leaves a
                        finalizable run). If it cannot be written, the finalizer is NOT called.
          ↓
    FINAL ASSEMBLY      FINAL_ASSEMBLY=deterministic (default): the output is assembled in CODE from the field states
                        and admitted evidence (src/final_assembly.py); one small no-tool call with
                        GLM_FINALIZER_MODEL may only narrate summary / research_trace. FINAL_ASSEMBLY=llm: ONE
                        no-tools finalizer call writes the output from the bundle (the previous behaviour)
          ↓
    structured JSON

The finalizer never receives the research conversation. Every started run leaves
a result.json, including research failures, finalization failures and
interrupts (Ctrl+C / Streamlit stop), with whatever was collected.

The loop is deliberately permissive: the model chooses tools and sources and
stores evidence. The code keeps the conversation within technical limits, notices
repeated work, logs everything and (deterministic assembly) writes every final value.

Reasoning: every request carries a top-level `reasoning_effort` (research high, document sweep / recovery /
finalizer low by default, src/phase_settings.py); a thinking object of type "disabled" is never sent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .bundle import BUNDLE_VERSION, admission_summary, build_research_bundle
from .consistency_checks import run_checks
from .concurrency import BatchCancelled
from .glm_client import GLMError
from .context import ResearchTracker, call_signature, compact_stub, model_view, replay_result
from .storage.trace import FETCH_TOOLS
from .field_recovery import (RETRY_STATES, current_evaluation, early_resolution_check, parse_retry_reply,
                             retry_packet, retry_queue)
from .fields import normalize_field_name
from .fields import grouped, parse_field_list, propulsion_of, public_spec, resolve_requested_fields, semantic_notes
from .phase_settings import for_phase
from .pricing import UNKNOWN_USAGE_NOTE, default_pricing, phase_run_cost
from .schemas import LEVEL3_TOPICS, parse_model_output
from .storage import trace
from .storage.cache import DocumentCache
from .storage.atomic import atomic_write_json
from .storage.run_log import RunLog, read_events, utc_now
from .tools import ToolConfig, ToolContext, dispatch, tool_specs, unavailable_tools
from .tools.evidence import EvidenceStore
from .acquisition import (ACQUISITION_MODES, ACQUISITION_TOOLS, CARD_TOOLS, DISCOVERY_TOOLS, AcquisitionTracker,
                          acquisition_tool_specs, annotate_search_result, cluster_source_type, document_card,
                          missing_categories_note, model_tokens, navigation_links, recovery_clusters)
from .candidate_harvest import RunHarvester
from .final_assembly import run_deterministic_finalization
from .parser_gaps import log_parser_gaps
from .evidence_admission import AdmissionContext
from .research_memory import ResearchMemory
from .variant_notes import record_id_of, variant_notes

OUTPUT_SHAPE = """{
  "vehicle_id": "<government record id>",
  "summary": "short description of what you found and how",
  "variant_identity": {"government": "<trim / model code as in the Level 1.5 record>",
                       "local_commercial_name": "<name or null>",
                       "mapping_basis": "explicit_source | inference", "evidence_ids": [], "notes": "..."},
  "fields": {
    "<field_name>": {"value": <any or null>, "unit": "<unit or null>",
                     "market": "<IL | other market code | null>",
                     "provenance": "israel_direct | foreign_direct | inferred | unresolved",
                     "alternatives": [{"value": <any>, "market": "...", "variant": "...", "evidence_ids": ["e2"]}],
                     "valid_as_of": "<date the value is valid as of, for time-sensitive fields, or null>",
                     "notes": "<optional>", "evidence_ids": ["e1"]}
  },
  "conflicts": [{"field": "<name>", "values": [{"value": <a>, "market": "IL", "evidence_ids": ["e1"]},
                                              {"value": <b>, "market": "MY", "evidence_ids": ["e2"]}],
                 "model_comment": "..."}],
  "provenance_summary": {"israeli_market_values": ["<field>"], "foreign_market_values": ["<field>"],
                         "inferred_variant_mappings": [{"mapping": "...", "basis": "..."}],
                         "conflicts": ["<field>"], "unresolved_fields": ["<field>"]},
  "additional_findings": [{"topic": "...", "finding": "...", "evidence_ids": []}],
  "level3": {"<topic>": {"finding": "...", "evidence_ids": []}},
  "research_trace": ["short step descriptions"]
}
evidence_ids always reference store_evidence records (e1, e2, ...), never document_ids."""

PROVENANCE_GUIDANCE = """Variant identity and market provenance:
- The target is the exact government-catalog variant in the Level 1.5 record (an Israeli-market vehicle:
  model year, government trim, model code, power, drivetrain). Treat any mapping from the government trim
  to a local commercial trim name as an inference unless a source states it explicitly, and say which.
- Sources from any market (Israel, Europe, UK, Malaysia, Denmark, China, ...) are all usable. Keep the
  market of every value: pass `market` (and `variant` when the source names a trim) to store_evidence.
- When the same field differs by market, prefer the Israeli-market value for this Israeli target and keep
  the other-market value with its source as an alternative and in `conflicts`. Never silently replace an
  Israeli value with a foreign one.
- Variant-sensitive values (price, licence fee, warranty, tyre size, weight, trim equipment, charging
  specs) belong to the trim and market their source describes. Do not carry a value over from another
  trim (e.g. a Black Edition, Core+ or RWD version) or another market as if it were this variant; if it
  cannot be tied to this exact variant, use null or alternatives with notes.
- Keep EV energy consumption (kWh/100km, field energy_consumption_kwh_100km) separate from fuel
  consumption in l/100km.
- Every value you report should cite evidence records (e1, e2, ...) created with store_evidence,
  containing the field, exact value, the document_id of a fetched document and a short verbatim quote that
  states the value. A document_id is not an evidence id.
- Evidence is admitted by deterministic runtime checks: the quote must occur in the cited (fetched) document
  and state the value itself. Never store a value you inferred (e.g. a gear count from "e-CVT", a number of
  climate zones the source does not state); store only what the source says. Each requested field has a
  semantic_definition; store the quantity it defines (e.g. a hybrid's torque_nm is the combustion engine's).
- The runtime computes each evidence item's market, source_authority and variant binding (binding_level,
  variant_match) from the source itself; your market / variant_match are only claims. Notes are commentary
  and never count as evidence or provenance.
- Time-sensitive values (price, licence fee, warranty) are valid as of a date: pass valid_as_of when the
  source states one."""

SYSTEM_PROMPT = """You are a vehicle research agent working on a benchmark.

You receive the complete Level 1.5 record of ONE specific vehicle variant from the Israeli government
catalog (identity, engine, structure, environment, safety). Treat that record as fixed context: do not
change it. Your job is to find additional information about THIS variant on the web, organize it and
return a useful result.

You have web tools: search_web, search_official_domains, fetch_url, fetch_pdf, render_page,
inspect_document_for_fields, extract_html, extract_tables, find_in_document, get_structured_data,
get_cached_document and store_evidence. Use them however you judge best. No source type is forbidden and none is required;
official manufacturer/importer pages, spec PDFs, press kits, reviews, forums and databases are all
allowed. You decide what to trust and how to describe it.

Guidance:
- Match the exact variant (model year, trim, powertrain, market) as well as you can. If a source
  describes a different market or trim, say so in notes rather than silently using it.
- When you rely on a source for a value, call store_evidence with the field, value, URL and a short
  verbatim quote, and cite the returned evidence_id in your answer.
- If sources disagree, report the disagreement in `conflicts` (you may still pick a value in `fields`).
- Values may be numbers, text, null, or several alternatives. You may add fields beyond the
  suggested list.

""" + PROVENANCE_GUIDANCE + """

Your role in this step: SOURCE ACQUISITION (documents are external memory).
- fetch_url / fetch_pdf / render_page store the full document in a document store and return only a
  document_id, metadata and a short preview. The full text is NOT in this conversation.
- Once a useful document is fetched, deterministic code harvests ALL requested Level 2 fields from it
  (labels, units, spec tables, embedded data) and inspects it locally; a later review step checks those
  candidates against the cached documents, and a targeted recovery handles what is still open. You do NOT
  have to look up each field in each document, and fields remaining open is not a reason to search a
  document you already have field by field.
- So spend your turns on obtaining a compact, high-value source set for THIS exact variant, trying first
  (search results carry acquisition_priority; 1 = try first):
  1. the official importer's model / price-list / brochure pages for the target market;
  2. the official manufacturer's technical specifications;
  3. official technical PDFs and documents;
  4. official media / press kits;
  5. high-quality publishers, then aggregators, then marketplaces and communities.
  For market-bound fields (price, licence fee, local trim name, warranty) target-market sources come before
  foreign manufacturer pages (priority_commercial). Lower-priority sources stay allowed when they are what
  exists; nothing is forbidden.
- Inspect a fetched document only when you need to establish its identity (model, year, trim, powertrain,
  market), its usefulness or its scope. To check several fields in one document, make ONE
  inspect_document_for_fields(document_id, fields) call (a deterministic local lookup) instead of one
  find_in_document call per field.
- Do not re-fetch, re-read or re-search what you already have. Research ends automatically after a few
  consecutive turns that acquire nothing new (a new usable document, a new official source, a new candidate
  for an open field or newly admitted evidence); re-reading cached documents does not count as progress.
- You may store obvious evidence you come across with store_evidence, but resolving every field is not
  this step's job. Level 3 (reliability, resale, insurance, recalls) should not take the budget away from
  acquiring the Level 2 sources.
- Re-fetching a URL you already fetched returns the same stored document. Older tool results in this
  conversation are shortened, but every document_id stays valid and can be queried again.
- If a requested field does not exist for this vehicle (e.g. a fuel tank on an EV), call
  report_field_status(field, "not_applicable").
- When storing evidence, you may pass your variant_match claim (exact | different | unclear); the runtime
  computes the effective binding, and a rejected store_evidence returns its reasons.
- You have a small budget of research turns. When it runs out (or acquisition stops progressing), later
  steps harvest, review and recover from your documents and a separate step compiles the final answer.

When you are done, reply with ONLY one JSON object (no tool call) shaped like:
""" + OUTPUT_SHAPE

ACQUISITION_SYSTEM_PROMPT = """You are the SOURCE ACQUISITION step of a vehicle research benchmark. Source acquisition
is your only job.

You receive the Level 1.5 record of ONE exact vehicle variant from the Israeli government catalog (model year,
government trim, model code, power, drivetrain). Treat that record as fixed context. Obtain a compact, high-value
document set about THIS variant. Deterministic code harvests every document you fetch for all requested fields, and
later steps review those values and recover what is still open. You do not extract, verify or record field values,
and you do not write the final answer.

Tools in this step: search_web, search_official_domains, fetch_url, fetch_pdf, render_page, nothing else.
fetch_url / fetch_pdf / render_page store the full document and return a document_id, metadata and a short preview.
For HTML pages the result also has `links`: the page's most useful outbound links (same site, PDFs, specification,
brochure, price list and warranty pages first).

Variant identity: prefer documents about the exact variant (model year, trim, powertrain, market). A document about
another market or trim is still usable (its market and variant are recorded by code); mention it in your final
reason only if it matters.

Where to look first (search results carry acquisition_priority; 1 = try first):
  1. the official importer's model / price-list / brochure pages for the target market;
  2. the official manufacturer's technical specifications;
  3. official technical PDFs and documents;
  4. official media / press kits;
  5. high-quality publishers, then aggregators, then marketplaces and communities.
priority_technical orders sources for technical specifications; priority_commercial orders them for market-bound
information (price, licence fee, local trim name, warranty), where target-market sources come before foreign
manufacturer pages. Lower-priority sources stay allowed when they are what exists; nothing is forbidden.

How to work:
- From an importer or manufacturer model page, follow its `links` to the specification PDF, brochure, price list
  and warranty page rather than searching again.
- Do not re-fetch a URL or repeat a search. Failed fetches (403 / 404 / 429) and repeated searches are not progress;
  research ends automatically when turns stop acquiring new usable documents.
- A turn may end with an [operational note] listing the source categories still missing: prioritize them.
- Prefer the listed official URLs (known pages from the official site's sitemap) and `links` from fetched pages over
  URLs you construct yourself.
- You have a small budget of turns.

When the document set is good enough (or nothing more can be found), reply with ONLY this JSON object and no tool
call:
{"done": true, "reason": "<short>"}"""

# ACQUISITION_DOCUMENT_CARD=on only (inserted after the `links` sentence of ACQUISITION_SYSTEM_PROMPT); not part of
# PROMPT_VERSION, so runs without the card keep their prompt version; run_started records the sent prompt's hash.
DOCUMENT_CARD_PARAGRAPH = """fetch_url / fetch_pdf results may also carry a server-computed `document_card`: the document's
variant match, market, source authority, whether it is usable, and how many requested fields per source category it
already supplies in scope. Use it to judge whether a document is the right variant / market and which source
categories it covered, instead of fetching it again. It is not evidence."""

FINALIZER_SYSTEM_PROMPT = """You are the finalization step of a vehicle research benchmark.

A research agent with web tools has already researched ONE specific vehicle variant. You receive a
compact research bundle instead of its conversation: the fixed Level 1.5 identity, the requested
targets, the evidence items it stored (with source URLs and quotes), candidate facts grouped by field,
metadata of the documents it fetched, excerpts it read from them, conflicts it noted, a concise list of
its research actions, targets with no stored evidence, and its last notes.

You cannot browse or call tools. Organize the research into the final answer. Evidence items with an
admission_status carry runtime-computed provenance: variant_match / binding_level (does the source describe the
exact target variant; "different" and "unbound" items are NOT about the target), market, source_authority and, for
time-sensitive values, valid_as_of (older items without admission_status carry the research model's own claims). Report a time-sensitive value with its valid_as_of, never as timeless. An item with
portable_to_target_market=true is an official foreign-market fact that the field's portability policy lets count for
the target market (portability_basis says why): use it, but report its own market, never as a target-market
source. field_states[field].conflict_class says why same-field values differ. You decide how to use
the material and which values to report; cite evidence_ids (e1, e2, ... from the bundle's evidence list,
never document_ids) where they support a value. If sources disagree, report it in `conflicts` (you may
still pick a value in `fields`). If a value comes from your own background knowledge or only from a
document excerpt without an evidence record, say so in that field's notes. Leave out or set to null what
the research did not find, and list it under provenance_summary.unresolved_fields.

""" + PROVENANCE_GUIDANCE.replace("pass `market` (and `variant` when the source names a trim) to store_evidence",
                                  "use the `market` / `variant` stored with each evidence item") + """

Reply with ONLY one JSON object shaped like:
""" + OUTPUT_SHAPE


FIELD_RECOVERY_SYSTEM_PROMPT = """You are performing a focused recovery attempt for ONE enrichment field on ONE exact
vehicle variant. Earlier research did not obtain a usable result for this field.

Do not research the vehicle generally. Your only research objective is the requested field.

- First inspect the relevant documents already in the cache (listed in the task) with find_in_document,
  extract_tables, get_structured_data or extract_html, before issuing broad web searches.
- The task may contain prior_relevant_excerpts: excerpts already exposed during earlier research/recovery
  turns. Use them as working memory before re-reading the same documents. If they already answer the
  requested field, store the appropriate evidence (excerpts are not evidence by themselves) and finish.
  If they show that a document was already inspected but do not answer the field, prefer a new targeted
  query or search strategy rather than reopening the same broad text. Re-read a cached document only
  when you have a concrete reason that the provided excerpts are insufficient.
- The task may contain deterministic_candidates: values the runtime's parser located in cached documents
  for this field (with quote, document_id, market/variant hints and parser_confidence = how sure the parser
  is of the label/value pairing, NOT that it applies to this variant). They are not evidence. If one is
  valid for the exact target variant and market, store it with store_evidence (citing its document_id and
  quote); otherwise use them to aim your research instead of rediscovering them.
- The task includes already_attempted_operations. Do not repeat an operation listed there when the exact
  same operation has already produced a result. Use a different query, another document, another
  extraction method, or a targeted web search instead. If an exact duplicate tool call is emitted anyway,
  the runtime may reuse the previous result without executing it again.
- If cached material is insufficient, run highly targeted searches using the strongest identifiers you
  have (model code, exact trim, model year, market, the field's own wording) rather than repeating the
  earlier queries listed in the task.
- Prefer sources for the target market; values from other markets or trims are allowed but must be
  stored with their market / variant / variant_match so they are not mistaken for the target variant.
- Call store_evidence for any value you rely on (field = the requested field name, exact value, source
  URL or document_id, short verbatim quote, market, variant, variant_match).
- While reading the same source, if you encounter a clearly stated value for ANOTHER requested enrichment
  field (listed in other_requested_fields), you may store that evidence too, as a by-product. Do not
  branch into research for that other field.
- If failure_reason is "conflicting", this is conflict-resolution research: do not search the field from
  scratch. Follow the task in `conflict`: investigate why the candidates differ (trim, model year,
  drivetrain, normal vs boost/performance mode, nominal vs peak, unit conversion, source wording,
  manufacturer documentation). Keep every candidate. Reply conflict_resolved only if evidence establishes
  which value applies to the exact target variant: store that evidence first and cite its evidence_ids in
  the reply (a conflict_resolved without valid evidence_ids leaves the field conflicting); otherwise
  reply conflicting.
  A reply of "found" does not resolve a conflict.
- If the field does not exist for this vehicle, say not_applicable. If you cannot resolve it within the
  budget, say unresolved. Never invent a value.

When done, reply with ONLY one JSON object (no tool call):
{"field": "<the requested field name>",
 "status": "found | conflict_resolved | not_applicable | unresolved | conflicting | foreign_market_only | variant_not_exact",
 "value": <any or null>, "unit": "<unit or null>", "market": "<market or null>",
 "evidence_ids": ["e12"], "notes": "..."}"""

DOCUMENT_SWEEP_SYSTEM_PROMPT = """You are the document-sweep step of a vehicle research benchmark for ONE exact vehicle
variant. Research has already downloaded documents, and a deterministic parser has extracted CANDIDATE values
for the requested enrichment fields from them. Candidates are not evidence: a parser found a label next to a
value; `parser_confidence` says how sure it is of that pairing, never that the value belongs to this variant.

Your job, for ALL requested fields at once, using ONLY what is already downloaded:
1. Review the deterministic candidates.
2. Reject candidates from the wrong market, model year or trim (use market_hint, variant_hint, year_hint,
   trim_mentioned, official_domain, the quote and the document) - simply do not store them.
3. Promote every valid candidate with store_evidence: field, value, unit, document_id, the short verbatim
   quote, market, variant, variant_match (exact | different | unclear).
4. When credible sources give different values, store each credible one with its own market/variant; do not
   pick a winner silently and do not try to resolve conflicts here.
5. Candidates are listed per field in presentation order (a scheduling order: official / target-market / exact-bound
   / same-row label-value pairings first; never a truth ranking). More may exist (candidates_not_shown): reach them
   with inspect_document_for_fields when the shown ones are not enough.
6. For fields_without_candidates, local_snippets (when present) hold the text around the field's label in cached
   documents, found by deterministic code: read them first; store a value only from the document they cite.
   fields_not_found_locally: none of their dictionary labels occurs in the text of any cached document; do not
   search the cached documents for them again unless you have a specific reason (e.g. embedded structured data).
   For the rest (and fields whose candidates are all rejected) inspect the cached documents, preferably with ONE
   inspect_document_for_fields(document_id, fields) per document (else find_in_document / extract_tables /
   get_structured_data / extract_html / get_cached_document), and store any valid fact the parser missed.
7. If a field does not exist for this vehicle, report_field_status(field, "not_applicable").
The packet may be one chunk of a larger review (`chunk`): review only the fields it lists.
You cannot search the web or fetch pages in this step; such calls are refused. Fields that remain open get a
targeted web follow-up later. Turn budget: at most turn_budget turns (never more than 2), used adaptively.
If the candidates are enough, put every store_evidence / report_field_status call you can justify into your
FIRST turn and the sweep ends there. A second turn is given ONLY when your first turn inspected cached
documents (find_in_document / extract_tables / extract_html / get_structured_data / get_cached_document) and
those calls returned content, so you can read the results and then store evidence. Results of tool calls made
in your last turn are never shown to you.
When done (or out of turns), reply with ONLY a JSON object:
{"reviewed": [{"field": "<name>", "decision": "promoted | rejected | conflict | not_found", "reason": "..."}],
 "notes": "..."}"""

CLUSTER_RECOVERY_SYSTEM_PROMPT = """You are performing a focused recovery attempt for ONE CLUSTER of related enrichment
fields (e.g. the technical specification fields) on ONE exact vehicle variant. Earlier research and a review of the
downloaded documents did not settle these fields. Do not research the vehicle generally.

Objective: find or exploit the best source for this cluster and resolve EVERY field of the cluster it supports.
- `fields` lists the cluster's open fields with their meaning (semantic_definition), current state and triage.
- `deterministic_candidates` are values the runtime's parser located in cached documents; they are not evidence.
  If one is valid for the exact target variant, store it with store_evidence (document_id + short verbatim quote).
- `ranked_documents` are the cached documents ranked for this cluster (source_yield_score is a scheduling rank,
  not a truth score). Inspect the best ones with find_in_document / extract_tables / get_structured_data before
  anything else.
- mode "local_only": only cached-document tools are available in this attempt; web calls are refused.
- mode "web": `search_hints` are results of searches already paid for and not yet fetched (routing hints, not
  evidence): fetching a strong hint is cheaper than a new search. Each attempt has a budget of billable provider
  searches (search_budget_provider_calls); search_official_domains costs one search per domain and a cached query
  costs nothing. A search beyond the budget is refused.
- After each turn the runtime harvests every new document for ALL open fields, re-evaluates every field and tells
  you which fields are resolved (do not research those again) and which new candidates appeared.
- `conflicts` holds a compact packet per conflicting field (competing evidence with quotes, sources, binding and
  conflict_class). Keep every candidate; reply conflict_resolved only when stored evidence settles which value
  applies to the exact target variant, citing its evidence_ids.
- Evidence is admitted by deterministic checks: the quote must occur in the cited document and state the value for
  that field. Never store inferred values. Store values for other open fields you meet in the same source too.
- Turn budget: turn_budget.base turns; more turns (up to turn_budget.max) are given only when a turn produced real
  novelty (a new official document, new candidates, newly admitted evidence, better binding or field state).
When done (or when nothing more can be found), reply with ONLY one JSON object (no tool call):
{"cluster": "<cluster>", "fields": [{"field": "<name>", "status": "found | conflict_resolved | not_applicable |
 unresolved | conflicting | foreign_market_only | variant_not_exact", "evidence_ids": ["e12"], "notes": "..."}],
 "notes": "..."}"""

ADJUDICATION_SYSTEM_PROMPT = """You are the candidate-adjudication step of a vehicle research benchmark. The target is ONE exact
vehicle variant from the Israeli government catalog (`vehicle_identity`: model year, government trim, model code,
powertrain, power) and the target market (`target_market`). Each requested field has a definition: it says exactly which
quantity the field means.

A deterministic parser located the candidate values below in documents that research already downloaded, and every
candidate (or snippet) already passed the deterministic source checks: the quote occurs in the document and names the
field with the value. You do NOT search, fetch, store or retype anything, and you have no tools. Your only job is to
judge whether each candidate states the requested quantity for THIS exact variant: the right trim, model year,
powertrain and market, and not, for example, another trim's column, a "from" price, a peak instead of a nominal figure,
a system value instead of the engine value, or another quantity entirely. Use the quote, the context, the source domain,
market, binding_level (how specifically the document is about this variant), variant_match and the parser hints.
When two values are both credible for the target, accept both: conflicts are handled later. Never invent a value.

reason codes: ok | other_trim | other_year | other_market | other_powertrain | wrong_quantity | not_this_field | unclear
(ok = it states this field for this exact variant).

Reply with ONLY one JSON object, no prose, in the shape the task asks for:
- task adjudicate_unambiguous: {"decisions": [{"id": "c1", "accept": true, "reason": "ok"}, ...]} (one per item).
- task adjudicate_ambiguous: {"fields": [{"field": "<name>", "accept": ["c3"], "reject": [{"id": "c4",
  "reason": "other_trim"}]}]}.
- task locate_missing: for each field a snippet states, {"fields": [{"field": "<name>", "value": <value>, "unit":
  "<unit>", "snippet": "s2", "span": [start, end]}]}, where span are character offsets [start, end) inside that
  snippet's text covering the statement of the value (its label and the value). Omit a field the snippets do not
  state; never guess."""

GROUNDED_SYSTEM_PROMPT = """You are the grounded-candidate step of a vehicle research benchmark. The target is ONE exact
vehicle variant (`vehicle_identity`) and the target market (`target_market`). You receive ONE document that research
already downloaded, split into numbered text blocks, and a few requested fields with their definitions. Some of these
fields may be stated in the document in prose or in a layout no parser reads.

For every requested field the document states for this vehicle, point at the statement: the block id and the character
offsets [start, end) inside that block's text covering the statement (its label or subject and the value), plus the
value and its unit as written. Omit every field the document does not state. Never guess, never compute or convert a
value, never combine statements from different blocks. You have no tools; you do not store anything. Your answer is
only a pointer that code verifies against the document and a later step judges.

Reply with ONLY one JSON object: {"items": [{"field": "<name>", "value": <value>, "unit": "<unit or null>",
"block": "b12", "start": 41, "end": 96}]}"""

REACQUIRE_SYSTEM_PROMPT = """You are a targeted RE-ACQUISITION step of a vehicle research benchmark for ONE exact vehicle
variant. Earlier steps could not settle the fields of ONE recovery cluster listed below. Your only job is to obtain the
document that states them: search and fetch. Code harvests every document you fetch and a later step judges the values;
you do not extract, verify or store anything.

Tools: search_web, search_official_domains, fetch_url, fetch_pdf, render_page, nothing else. Prefer the listed official
URLs (from the official site's sitemap) and `links` of fetched pages over URLs you construct. Do not fetch a URL listed
as already fetched and do not repeat a known unproductive route. The budget is small (a few turns, a few billable
searches, a few fetches). When the right document is fetched, or nothing more can be found, reply with ONLY:
{"done": true, "reason": "<short>"}"""

REPAIR_PROMPT = ("Your last reply could not be parsed as JSON. Return the same content as ONE valid JSON object "
                 "and nothing else.")

PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + ACQUISITION_SYSTEM_PROMPT + FINALIZER_SYSTEM_PROMPT
                                 + FIELD_RECOVERY_SYSTEM_PROMPT + DOCUMENT_SWEEP_SYSTEM_PROMPT
                                 + CLUSTER_RECOVERY_SYSTEM_PROMPT + ADJUDICATION_SYSTEM_PROMPT
                                 + GROUNDED_SYSTEM_PROMPT + REACQUIRE_SYSTEM_PROMPT
                                 + BUNDLE_VERSION).encode("utf-8")).hexdigest()[:12]
CLUSTER_TURN_CEILING = 4    # absolute per-attempt ceiling, whatever CLUSTER_MAX_TURNS says


def research_system_prompt(mode: str = "legacy", document_card: bool = False) -> str:
    """The research system prompt of an acquisition mode (contract: ACQUISITION_SYSTEM_PROMPT; legacy: SYSTEM_PROMPT)
    without tools this host cannot run (e.g. render_page without Playwright). `document_card` (contract only) adds
    the one DOCUMENT_CARD_PARAGRAPH (ACQUISITION_DOCUMENT_CARD=on); off, the prompt is exactly the previous one."""
    prompt = ACQUISITION_SYSTEM_PROMPT if mode == "contract" else SYSTEM_PROMPT
    if mode == "contract" and document_card:
        prompt = prompt.replace("\n\nVariant identity:", "\n" + DOCUMENT_CARD_PARAGRAPH + "\n\nVariant identity:", 1)
    for name in unavailable_tools():
        prompt = prompt.replace(f" {name},", "").replace(f" / {name}", "")
    return prompt


@dataclass
class SearchBudget:
    """Billable provider searches one cluster attempt may make. Charged with the underlying provider calls a tool
    call actually made (search_official_domains = one per uncached domain); cache hits cost nothing."""
    limit: int
    used: int = 0
    refused: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

STOP_REASONS = ("model_finished", "max_steps", "no_new_research", "no_new_artifact", "acquisition_sufficient",
                "extension_exhausted", "user_cancelled", "api_failure", "research_exception")
STATUSES = ("completed", "max_steps_finalized", "no_new_research_finalized", "completed_unparsed",
            "finalization_failed", "research_failed", "interrupted", "incomplete", "recovered_finalized",
            "finalization_pending", "acquisition_sufficient_finalized", "under_acquired_finalized")
FINALIZED_STATUS = {"max_steps": "max_steps_finalized", "no_new_research": "no_new_research_finalized",
                    "no_new_artifact": "no_new_research_finalized",
                    "acquisition_sufficient": "acquisition_sufficient_finalized",
                    "extension_exhausted": "under_acquired_finalized"}
# finalization_pending: the durable pre-finalization checkpoint (research complete, finalizer not finished).
PARTIAL_STATUSES = ("interrupted", "research_failed", "finalization_failed", "incomplete", "finalization_pending")
PHASE_LABELS = {"research": "Research", "deterministic_harvest": "Deterministic Harvest",
                "document_sweep": "Document Sweep", "field_detection": "Field Detection",
                "field_recovery": "Field Recovery", "finalization": "Finalization"}


def interruption_message(phase: str | None) -> str:
    label = PHASE_LABELS.get(phase or "", phase or "the run")
    return (f"Run interrupted during {label}. All completed research/evidence was preserved. "
            "The result below is partial.")


@dataclass
class AgentConfig:
    max_steps: int = 6                        # primary research turn ceiling (PRIMARY_RESEARCH_MAX_TURNS / AGENT_MAX_STEPS)
    max_tool_output_chars: int = 6000         # per tool result sent to the model this turn
    keep_recent_tool_results: int = 4         # newest tool results kept as sent; older ones are compacted
    compact_tool_output_chars: int = 700      # size of a compacted older tool result
    no_new_research_turns: int = 2            # finalize after N consecutive idle turns; 0 = off
    # ACQUISITION_MODE: "contract" = primary research is source acquisition only (search / fetch tools, acquisition
    # prompt, progress = new documents and candidates, extension futility, always finalized from the bundle);
    # "legacy" = the previous behaviour exactly.
    acquisition_mode: str = "contract"
    # ACQUISITION_DOCUMENT_CARD (contract research only; default off): successful fetch_url / fetch_pdf results carry a
    # server-computed `document_card` (variant match, market, authority, usability, in-scope candidate fields per
    # recovery cluster); scheduling metadata, never evidence. The research prompt then gets one paragraph about it.
    acquisition_document_card: bool = False
    # The run profile chosen for the run (src/run_profiles.py; "" = none, e.g. the CLI). Recorded, never read by the
    # engine: a profile only sets other fields of this config.
    run_profile: str = ""
    # Primary research is source acquisition (src/acquisition.py). It stops after N consecutive turns that acquired
    # nothing (no new usable document, official source, candidate for an open field, admitted evidence or better
    # binding; re-reads, repeated searches and failed fetches are no progress). 0 = off.
    primary_research_no_artifact_stop: int = 2
    # Optional acquisition-sufficiency transition: >= N useful documents AND >= X % of the applicable fields covered by a
    # candidate (or settled). Disabled while either is 0. X may be a fraction (0.6) or a percentage (60).
    primary_research_min_useful_documents: int = 0
    primary_research_candidate_field_coverage_threshold: float = 0.0
    # FAIL-SAFE minimum acquisition base: no scheduler stop (no-artifact streak, sufficiency, idle stop, the normal
    # ceiling) ends research before the run holds >= N useful documents AND >= X % of the applicable fields have a
    # candidate from a source in their market scope (any market for market_sensitivity "low", else the target market).
    # An under-acquired run keeps acquiring up to the hard ceiling. Both 0 = gate off.
    primary_research_min_base_documents: int = 3
    primary_research_min_base_scoped_coverage: float = 50.0
    primary_research_hard_max_turns: int = 12     # only an under-acquired run may go beyond max_steps
    finalizer_bundle_max_chars: int = 60000   # cap on the compact research bundle
    temperature: float | None = None
    max_tokens: int | None = None
    include_level3: bool = False              # Level 3 open research is opt-in (INCLUDE_LEVEL3 / UI / --level3)
    # "" = provider default (not sent), "enabled", "disabled". "disabled" is never sent (glm-5.3 models always think:
    # HTTP 400 code 1210); it maps to no thinking object + reasoning_effort low (src/phase_settings.py)
    thinking: str = ""
    # GLM_REASONING_EFFORT: low | medium | high | max; "" = each phase's own setting, else its phase default
    # (research high, document_sweep / recovery / finalizer low). Sent as the top-level `reasoning_effort` field.
    reasoning_effort: str = ""
    extra_body: dict = field(default_factory=dict)
    # Requested enrichment fields: names or specs; empty = every field of the enrichment schema.
    requested_fields: list = field(default_factory=list)
    target_market: str = "IL"
    # Targeted field recovery (one focused retry task per requested field that primary research missed).
    field_recovery_enabled: bool = True
    field_recovery_max_attempts: int = 2      # per field; a field spec's `recovery_attempts` overrides it
    field_recovery_max_steps: int = 4         # model turns per retry attempt
    field_recovery_max_total_steps: int = 24  # hard cap on retry turns per vehicle (all fields/attempts); 0 = none
    # Bounded working memory of already-exposed excerpts handed to each retry (src/excerpts.py).
    field_recovery_prior_excerpts_max_items: int = 8
    field_recovery_prior_excerpts_max_chars: int = 8000
    field_recovery_prior_excerpt_max_chars: int = 1500
    field_recovery_candidates_per_field: int = 8   # deterministic candidates handed to a field retry
    # Layered harvesting: deterministic candidates from every document + one bounded model document sweep.
    layered_harvest_enabled: bool = True
    # Adaptive: up to N turns (absolute max 2). Turn 2 happens ONLY when turn 1 inspected cached documents and got
    # content back to interpret; a sweep that just promotes candidates ends after 1 turn. 1 = never a follow-up;
    # 0 = no sweep.
    document_sweep_max_turns: int = 2
    # Packet limits of ONE sweep call; a larger packet is split deterministically by recovery_cluster (0 = no limit).
    # The per-field limit only shapes the packet: every candidate stays stored and reachable through local tools.
    document_sweep_candidates_per_field: int = 3
    document_sweep_packet_max_chars: int = 28000
    document_sweep_max_fields: int = 12
    document_sweep_max_candidates: int = 16
    # SWEEP_MODE: "adjudication" = the model only judges pre-checked candidates in small no-tool JSON packets (src/
    # adjudication.py; the DOCUMENT_SWEEP_* chunk limits above do not apply); "legacy" = the tool-loop sweep above.
    sweep_mode: str = "adjudication"
    adjudication_max_u_items: int = 40         # candidates per U (unambiguous) packet
    adjudication_max_a_fields: int = 6         # fields per A (ambiguous) packet
    adjudication_max_a_candidates: int = 18    # candidates per A packet
    adjudication_max_m_fields: int = 6         # fields per M (missing, label snippets only) packet
    adjudication_max_m_snippets: int = 12      # snippets per M packet
    adjudication_u_max_tokens: int = 1500      # max_tokens of a U / A / M call (thinking: the document_sweep phase)
    adjudication_a_max_tokens: int = 2000
    adjudication_m_max_tokens: int = 1500
    # FINAL_ASSEMBLY: "deterministic" = the final output is built in code from the field states and admitted evidence
    # (src/final_assembly.py; a model may only narrate summary / research_trace); "llm" = the finalizer model writes
    # the output from the compact bundle (the previous behaviour).
    final_assembly: str = "deterministic"
    # Tail recovery: "reacquire" (per recovery cluster: a short targeted acquisition episode, harvest, grounded
    # candidates and adjudication; the recovery model never stores evidence), "cluster" (one tool-using attempt per
    # recovery cluster, see src/tail_planner.py) or "legacy" (per field).
    recovery_mode: str = "reacquire"
    # SITE_MAP (contract acquisition and reacquire recovery): official domains' sitemaps -> ranked real URLs offered to
    # the acquisition model (src/site_map.py); discovery metadata only
    site_map: bool = True
    # GROUNDED_CANDIDATES: one no-tool call per top document for open fields without an admissible candidate; the
    # model points at a span, code cuts the quote and dry-runs admission; candidates only (src/grounded.py)
    grounded_candidates: bool = True
    cluster_max_attempts: int = 2             # per cluster; the cluster's own fields' recovery_attempts cap it too
    cluster_base_turns: int = 2               # turns every cluster attempt may use
    cluster_max_turns: int = 4                # ceiling (never above 4): extra turns only after real novelty
    cluster_search_budget: int = 4            # billable provider searches per cluster attempt (cache hits are free)
    cluster_candidates_per_field: int = 6
    cluster_packet_max_chars: int = 30000
    # Cross-run research memory (src/research_memory.py): verified fact reuse, negative routes, recovery yield.
    research_memory_enabled: bool = True
    # Per-phase inference settings (src/phase_settings.py): {"research" | "document_sweep" | "recovery" | "finalizer":
    # {model, thinking, max_tokens, temperature, timeout_s}}. Empty = every phase inherits the global settings.
    phase_settings: dict = field(default_factory=dict)
    # refuse (without executing) a web route an earlier run already found unproductive for the attempt's open fields;
    # never skips research as such: any other route still runs
    negative_route_blocking: bool = True


AGENT_ENV = {
    "max_steps": "AGENT_MAX_STEPS",
    "primary_research_no_artifact_stop": "PRIMARY_RESEARCH_NO_ARTIFACT_STOP",
    "primary_research_min_useful_documents": "PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS",
    "primary_research_min_base_documents": "PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS",
    "primary_research_hard_max_turns": "PRIMARY_RESEARCH_HARD_MAX_TURNS",
    "max_tool_output_chars": "AGENT_MAX_TOOL_OUTPUT_CHARS",
    "keep_recent_tool_results": "AGENT_KEEP_RECENT_TOOL_RESULTS",
    "compact_tool_output_chars": "AGENT_COMPACT_TOOL_OUTPUT_CHARS",
    "no_new_research_turns": "AGENT_NO_NEW_RESEARCH_TURNS",
    "finalizer_bundle_max_chars": "AGENT_FINALIZER_BUNDLE_MAX_CHARS",
    "field_recovery_max_attempts": "FIELD_RECOVERY_MAX_ATTEMPTS",
    "field_recovery_max_steps": "FIELD_RECOVERY_MAX_STEPS",
    "field_recovery_max_total_steps": "FIELD_RECOVERY_MAX_TOTAL_STEPS",
    "field_recovery_prior_excerpts_max_items": "FIELD_RECOVERY_PRIOR_EXCERPTS_MAX_ITEMS",
    "field_recovery_prior_excerpts_max_chars": "FIELD_RECOVERY_PRIOR_EXCERPTS_MAX_CHARS",
    "field_recovery_prior_excerpt_max_chars": "FIELD_RECOVERY_PRIOR_EXCERPT_MAX_CHARS",
    "document_sweep_max_turns": "DOCUMENT_SWEEP_MAX_TURNS",
    "document_sweep_candidates_per_field": "DOCUMENT_SWEEP_CANDIDATES_PER_FIELD",
    "document_sweep_packet_max_chars": "DOCUMENT_SWEEP_MAX_PACKET_CHARS",
    "document_sweep_max_fields": "DOCUMENT_SWEEP_MAX_FIELDS",
    "document_sweep_max_candidates": "DOCUMENT_SWEEP_MAX_CANDIDATES",
    "adjudication_max_u_items": "ADJUDICATION_MAX_U_ITEMS",
    "adjudication_max_a_fields": "ADJUDICATION_MAX_A_FIELDS",
    "adjudication_max_a_candidates": "ADJUDICATION_MAX_A_CANDIDATES",
    "adjudication_max_m_fields": "ADJUDICATION_MAX_M_FIELDS",
    "adjudication_max_m_snippets": "ADJUDICATION_MAX_M_SNIPPETS",
    "adjudication_u_max_tokens": "ADJUDICATION_U_MAX_TOKENS",
    "adjudication_a_max_tokens": "ADJUDICATION_A_MAX_TOKENS",
    "adjudication_m_max_tokens": "ADJUDICATION_M_MAX_TOKENS",
    "cluster_max_attempts": "CLUSTER_MAX_ATTEMPTS",
    "cluster_base_turns": "CLUSTER_BASE_TURNS",
    "cluster_max_turns": "CLUSTER_MAX_TURNS",
    "cluster_search_budget": "CLUSTER_SEARCH_BUDGET",
}
TOOL_ENV = {
    "preview_chars": "TOOL_PREVIEW_CHARS",
    "max_text_chars": "TOOL_MAX_TEXT_CHARS",
    "max_links": "TOOL_MAX_LINKS",
    "max_table_rows": "TOOL_MAX_TABLE_ROWS",
    "max_tables": "TOOL_MAX_TABLES",
}


def _env_ints(mapping: dict[str, str], env: Callable[[str], str | None] = os.environ.get) -> dict[str, int]:
    out = {}
    for attr, name in mapping.items():
        raw = (env(name) or "").strip()
        if raw:
            try:
                out[attr] = int(raw)
            except ValueError:
                pass
    return out


def _env_bool(raw: str | None) -> bool | None:
    raw = (raw or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def agent_config_from_env(env: Callable[[str], str | None] = os.environ.get, **overrides) -> AgentConfig:
    values: dict[str, Any] = _env_ints(AGENT_ENV, env)
    # PRIMARY_RESEARCH_MAX_TURNS is the name of the research turn ceiling; AGENT_MAX_STEPS stays an alias
    values.update(_env_ints({"max_steps": "PRIMARY_RESEARCH_MAX_TURNS"}, env))
    for attr, name in (("primary_research_candidate_field_coverage_threshold",
                        "PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD"),
                       ("primary_research_min_base_scoped_coverage", "PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE")):
        raw = (env(name) or "").strip()
        if raw:
            try:
                values[attr] = float(raw)
            except ValueError:
                pass
    enabled = _env_bool(env("FIELD_RECOVERY_ENABLED"))
    if enabled is not None:
        values["field_recovery_enabled"] = enabled
    level3 = _env_bool(env("INCLUDE_LEVEL3"))
    if level3 is not None:
        values["include_level3"] = level3
    memory = _env_bool(env("RESEARCH_MEMORY_ENABLED"))
    if memory is not None:
        values["research_memory_enabled"] = memory
    blocking = _env_bool(env("NEGATIVE_ROUTE_BLOCKING"))
    if blocking is not None:
        values["negative_route_blocking"] = blocking
    layered = _env_bool(env("LAYERED_HARVEST_ENABLED"))
    if layered is not None:
        values["layered_harvest_enabled"] = layered
    if (env("ENRICHMENT_FIELDS") or "").strip():
        values["requested_fields"] = parse_field_list(env("ENRICHMENT_FIELDS"))
    if (env("TARGET_MARKET") or "").strip():
        values["target_market"] = env("TARGET_MARKET").strip()
    from .phase_settings import phase_settings_from_env

    phases = phase_settings_from_env(env)
    if phases:
        values["phase_settings"] = phases
    if (env("ACQUISITION_MODE") or "").strip().lower() in ACQUISITION_MODES:
        values["acquisition_mode"] = env("ACQUISITION_MODE").strip().lower()
    if (env("SWEEP_MODE") or "").strip().lower() in ("adjudication", "legacy"):
        values["sweep_mode"] = env("SWEEP_MODE").strip().lower()
    card = _env_bool(env("ACQUISITION_DOCUMENT_CARD"))
    if card is not None:
        values["acquisition_document_card"] = card
    if (env("RECOVERY_MODE") or "").strip().lower() in RECOVERY_MODES:
        values["recovery_mode"] = env("RECOVERY_MODE").strip().lower()
    for attr, name in (("site_map", "SITE_MAP"), ("grounded_candidates", "GROUNDED_CANDIDATES")):
        flag = _env_bool(env(name))
        if flag is not None:
            values[attr] = flag
    if (env("FINAL_ASSEMBLY") or "").strip().lower() in FINAL_ASSEMBLY_MODES:
        values["final_assembly"] = env("FINAL_ASSEMBLY").strip().lower()
    from .phase_settings import parse_effort

    effort = parse_effort(env("GLM_REASONING_EFFORT"))
    if effort:
        values["reasoning_effort"] = effort
    if (env("GLM_THINKING") or "").strip().lower() in ("enabled", "disabled"):
        values["thinking"] = env("GLM_THINKING").strip().lower()      # "disabled" is mapped, never sent
    raw_extra = (env("GLM_EXTRA_BODY") or "").strip()
    if raw_extra:
        try:
            extra = json.loads(raw_extra)
            if isinstance(extra, dict):
                values["extra_body"] = extra
        except ValueError:
            pass        # reported by app_config.validate_config
    return AgentConfig(**{**values, **overrides})


def tool_config_from_env(env: Callable[[str], str | None] = os.environ.get, **overrides) -> ToolConfig:
    return ToolConfig(**{**_env_ints(TOOL_ENV, env), **overrides})


FINAL_ASSEMBLY_MODES = ("deterministic", "llm")
RECOVERY_MODES = ("reacquire", "cluster", "legacy")


def request_extra(config: AgentConfig, settings: dict | None = None) -> dict:
    """Extra fields merged into a chat request: extra_body, the thinking object and the top-level reasoning_effort.
    `settings`: one call's resolved phase settings (phase_settings.for_phase); None = the global settings only.

    A thinking object of type "disabled" is NEVER sent, whether configured (GLM_THINKING, a phase, the UI, a saved
    request) or in extra_body (GLM_EXTRA_BODY): it is stripped and the call gets reasoning_effort low unless an effort
    is set explicitly (phase_settings.thinking_disabled_mapping). An explicit effort (a phase's own or the global one)
    and thinking "enabled" win over extra_body; extra_body's own reasoning_effort wins over a phase default."""
    from .phase_settings import PROVIDER_DEFAULT, parse_effort, thinking_disabled_mapping

    extra = dict(config.extra_body or {})
    if isinstance(extra.get("thinking"), dict) and str(extra["thinking"].get("type") or "").lower() == "disabled":
        extra.pop("thinking")
    if settings is None:
        explicit = parse_effort(config.reasoning_effort)
        mapping = thinking_disabled_mapping(str(config.thinking or "").lower(), explicit, config.extra_body)
        thinking, effort, mapped = mapping["thinking"], mapping["effort"], mapping["mapped"]
        source = "global" if explicit else "thinking_disabled" if mapped else None
    else:
        thinking, effort = settings.get("thinking") or "", settings.get("reasoning_effort")
        source, mapped = settings.get("reasoning_effort_source"), settings.get("thinking_disabled_mapped")
    if mapped:              # a configured "disabled" means no thinking object at all, not extra_body's own one
        extra.pop("thinking", None)
    if thinking == "enabled":
        extra["thinking"] = {"type": "enabled"}
    if effort == PROVIDER_DEFAULT or (effort is None and source in ("phase", "global", "extra_body")):
        extra.pop("reasoning_effort", None)       # an explicit "provider default": send no effort at all
    elif effort and not (source == "phase_default" and extra.get("reasoning_effort")):
        extra["reasoning_effort"] = effort
    return extra


def is_thinking_rejection(exc: BaseException) -> bool:
    """HTTP 400 "This model always engages in thinking and cannot be disabled" (provider code 1210)."""
    body = str(getattr(exc, "body", "") or "")
    return getattr(exc, "status", None) == 400 and (re.search(r"\b1210\b", body) is not None
                                                     or "cannot be disabled" in body.lower())


def research_model_of(client) -> str:
    return client.model


def finalizer_model_of(client, config: AgentConfig | None = None) -> str:
    from .phase_settings import clean

    override = clean(getattr(config, "phase_settings", None)).get("finalizer", {}).get("model")
    if override:
        return override
    settings = getattr(client, "settings", None)
    explicit = getattr(settings, "finalizer_model", "") if settings is not None else ""
    return explicit or getattr(client, "finalizer_model", "") or client.model


def effective_glm_config(client, config: AgentConfig, tool_config: ToolConfig) -> dict:
    """The complete GLM configuration a run used (never includes the API key)."""
    settings = client.settings.public() if hasattr(client, "settings") else {"model": client.model}
    settings.setdefault("research_model", research_model_of(client))
    settings["finalizer_model"] = finalizer_model_of(client, config)
    extra = request_extra(config)
    from .phase_settings import describe

    phases = describe(config, {"research_model": research_model_of(client), "finalizer_model":
                               settings["finalizer_model"], "timeout_s": settings.get("timeout_s"),
                               "chat_max_attempts": settings.get("chat_max_attempts")})
    return {
        **settings,
        "thinking": extra.get("thinking", "provider_default"),
        # the effective effort of each phase is in phase_settings[phase].reasoning_effort
        "reasoning_effort": {phase: values.get("reasoning_effort") for phase, values in phases.items()},
        "max_tokens": config.max_tokens if config.max_tokens else "provider_default",
        "temperature": config.temperature if config.temperature is not None else "provider_default",
        "tool_choice": "auto",
        "extra_request_body": extra,
        "search_backend": tool_config.search_backend,
        "phase_settings": phases,
        "recorded_at": utc_now(),
    }


def effective_config(client, config: AgentConfig, tool_config: ToolConfig) -> dict:
    return {"glm": effective_glm_config(client, config, tool_config), "agent": asdict(config),
            "tools": asdict(tool_config), "prompt_version": PROMPT_VERSION, "bundle_version": BUNDLE_VERSION}


def build_user_message(payload: dict, include_level3: bool, max_steps: int | None = None,
                       notes: dict | None = None, requested: list[dict] | None = None,
                       no_artifact_stop: int | None = None) -> str:
    specs = requested if requested is not None else resolve_requested_fields(None)
    targets = {group: [(s["name"], s.get("description") or s["name"]) for s in items if s.get("applicable", True)]
               for group, items in grouped(specs).items()}
    targets = {group: items for group, items in targets.items() if items}
    lines = [
        "Level 1.5 record (fixed context):",
        json.dumps(payload, ensure_ascii=False, indent=1),
        "",
    ]
    if notes:
        lines += ["Operator notes for this variant (context and inferences, not verified facts):",
                  json.dumps(notes, ensure_ascii=False, indent=1), ""]
    lines += [
        "Requested enrichment fields (keys and meaning; report not_applicable for what does not apply, add what "
        "you find useful):",
    ]
    for group, fields in targets.items():
        lines.append(f"- {group}: " + "; ".join(f"{key} = {label}" for key, label in fields))
    semantics = semantic_notes(specs)
    if semantics:
        lines += ["", "Field semantics (store exactly this quantity; other figures are rejected):"]
        lines += [f"- {key}: {text}" for key, text in semantics.items()]
    if include_level3:
        lines += ["", "Level 3 open research (put results under `level3`):"]
        lines += [f"- {key}: {label}" for key, label in LEVEL3_TOPICS.items()]
    else:
        lines += ["", "Level 3 research is disabled for this run; leave `level3` empty."]
    if max_steps:
        lines += ["", f"Research budget: about {max_steps} model turns (more only while the acquired source set is "
                      "still too thin). You may finish earlier."
                  + (f" Once enough sources are acquired, research also ends after {no_artifact_stop} consecutive "
                     "turns that acquire nothing new." if no_artifact_stop else "")]
    lines += ["", "Start researching."]
    return "\n".join(lines)


def build_acquisition_message(payload: dict, include_level3: bool, max_steps: int | None = None,
                              notes: dict | None = None, requested: list[dict] | None = None,
                              no_artifact_stop: int | None = None, target_market: str = "IL") -> str:
    """The contract-mode research task: the fixed record, operator notes, the target market and the source
    categories (recovery clusters) the requested fields need, with field NAMES only (no descriptions or semantic
    definitions: field work happens in later steps)."""
    specs = requested if requested is not None else resolve_requested_fields(None)
    lines = ["Level 1.5 record (fixed context):", json.dumps(payload, ensure_ascii=False, indent=1), ""]
    if notes:
        lines += ["Operator notes for this variant (context and inferences, not verified facts):",
                  json.dumps(notes, ensure_ascii=False, indent=1), ""]
    lines += [f"Target market: {target_market}", "",
              "Source categories to acquire (the requested fields grouped by recovery cluster, and the source type "
              "that usually answers each; code extracts the values from the documents you fetch):"]
    for cluster, items in recovery_clusters(specs).items():
        names = ", ".join(s["name"] + (f" ({s['display_name_he']})" if s.get("display_name_he") else "")
                          for s in items)
        lines.append(f"- {cluster} [{cluster_source_type(items, target_market)}]: {names}")
    if include_level3:
        lines += ["", "Level 3 open research (put results under `level3`):"]
        lines += [f"- {key}: {label}" for key, label in LEVEL3_TOPICS.items()]
    if max_steps:
        lines += ["", f"Research budget: about {max_steps} model turns (more only while the acquired source set is "
                      "still too thin). You may finish earlier."
                  + (f" Once enough sources are acquired, research also ends after {no_artifact_stop} consecutive "
                     "turns that acquire nothing new." if no_artifact_stop else "")]
    lines += ["", "Start acquiring sources."]
    return "\n".join(lines)


def finalizer_messages(bundle: dict) -> list[dict]:
    return [
        {"role": "system", "content": FINALIZER_SYSTEM_PROMPT},
        {"role": "user", "content": "Research bundle (JSON):\n" + json.dumps(bundle, ensure_ascii=False, default=str)
                                    + "\n\nReturn the final JSON object now."},
    ]


def outgoing_messages(messages: list[dict], cfg: AgentConfig) -> list[dict]:
    """What the research model sees: older tool results replaced by compact stubs, private keys dropped.

    The full results remain in events.jsonl and the document cache; stubs keep
    document_ids, URLs and queries so the model can reopen anything.
    """
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep = cfg.keep_recent_tool_results
    old = set(tool_positions[:-keep]) if keep else set(tool_positions)
    out = []
    for i, message in enumerate(messages):
        content = message.get("content") or ""
        if i in old and message.get("_compact") and len(message["_compact"]) < len(content):
            content = message["_compact"]
        out.append({**{k: v for k, v in message.items() if not k.startswith("_")}, "content": content})
    return out


def _assistant_echo(message: dict) -> dict:
    echo: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        echo["tool_calls"] = message["tool_calls"]
    return echo


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def check_cancelled(cancel_event) -> None:
    """Safe point for cooperative batch cancellation (see src/concurrency.BatchCancelled)."""
    if cancel_event is not None and cancel_event.is_set():
        raise BatchCancelled("batch cancelled")


class ModelCaller:
    """Calls GLM, logs every response as a `model_response` event and books usage by phase."""

    def __init__(self, client, run_log: RunLog, config: AgentConfig, run_context: dict | None = None,
                 cancel_event=None):
        self.client, self.run_log, self.config = client, run_log, config
        self.thinking_mapped_logged = False      # thinking_disabled_mapped is logged once per run
        self.usage = {group: trace.empty_usage() for group in trace.PHASE_GROUPS}
        self.last_content: str | None = None
        self.run_context = run_context if run_context is not None else {}
        self.cancel_event = cancel_event

    def __call__(self, messages: list[dict], *, phase: str, tools: list[dict] | None = None,
                 model: str | None = None, meta: dict | None = None, activity: dict | None = None,
                 max_tokens: int | None = None, settings_phase: str | None = None) -> dict:
        """`meta` adds explicit context to the logged model_response (e.g. the field and attempt of a
        field-recovery turn). It never changes `phase`, which usage accounting groups by. `activity`
        only labels this call's request-lifecycle events (e.g. the research turn); it is not logged
        on the model_response. `settings_phase` takes the request settings (reasoning effort, timeouts,
        attempts, model) of another phase while accounting stays with `phase` (e.g. a recovery re-acquisition
        turn runs with the research settings and is booked as recovery)."""
        check_cancelled(self.cancel_event)
        meta = meta or {}
        self.run_context.clear()
        self.run_context.update({"field": meta.get("field"), "field_attempt": meta.get("attempt"),
                                 "turn": meta.get("turn"), **(activity or {})})
        # per-phase settings (src/phase_settings.py); unset keys inherit the global configuration
        from .phase_settings import for_phase

        settings = for_phase(self.config, settings_phase or phase)
        extra = request_extra(self.config, settings)
        # Extra JSON must not override a call's explicit token cap or add tools to finalization.
        if max_tokens is not None and "max_tokens" in extra:
            extra["max_tokens"] = max_tokens
        if trace.phase_group(settings_phase or phase) == "finalization":
            extra.pop("tools", None)
            extra.pop("tool_choice", None)
        if settings["thinking_disabled_mapped"] and not self.thinking_mapped_logged:
            self.thinking_mapped_logged = True
            try:
                self.run_log.event("thinking_disabled_mapped", phase=phase, source=settings["thinking_disabled_source"],
                                   reasoning_effort=settings["reasoning_effort"],
                                   note="thinking 'disabled' is never sent (the provider rejects it, code 1210): no "
                                        "thinking object, reasoning_effort low unless set explicitly")
            except Exception:  # noqa: BLE001 - telemetry never costs the call
                pass
        kwargs: dict[str, Any] = {"tools": tools, "temperature": settings["temperature"],
                                  "max_tokens": max_tokens or settings["max_tokens"], "extra": extra or None}
        model = model or settings["model"]
        if model:
            kwargs["model"] = model
        if settings["timeout_s"]:
            kwargs["timeout_s"] = settings["timeout_s"]
        if settings["max_attempts"]:
            kwargs["max_attempts"] = settings["max_attempts"]
        effort = (extra or {}).get("reasoning_effort")
        try:
            response = self.client.chat(messages, **kwargs)
        except GLMError as exc:
            if not is_thinking_rejection(exc):
                raise
            # Defensive: the provider still says thinking cannot be disabled. ONE extra request without a thinking
            # object and with effort low; it does not count against max_attempts (the 400 was not retried), and a
            # second rejection surfaces as a normal error.
            retry_extra = {k: v for k, v in (extra or {}).items() if k != "thinking"}
            retry_extra["reasoning_effort"] = "low"
            self.run_log.event("reasoning_retry", phase=phase, model=model or research_model_of(self.client),
                               status=exc.status, error=_error_text(exc), sent_thinking=(extra or {}).get("thinking"),
                               sent_reasoning_effort=effort, retry_reasoning_effort="low")
            kwargs["extra"] = retry_extra
            effort = "low"
            response = self.client.chat(messages, **kwargs)
        latency = int(getattr(response, "latency_ms", 0) or 0)
        trace.add_usage(self.usage[trace.phase_group(phase)], response.usage, latency)
        raw = getattr(response, "raw", {}) or {}
        content = response.message.get("content")
        if (content or "").strip():
            self.last_content = content
        self.run_log.event("model_response", phase=phase, model=model or research_model_of(self.client),
                           finish_reason=response.finish_reason, usage=response.usage, reasoning_effort=effort,
                           reasoning_tokens=trace.reasoning_tokens(response.usage),
                           latency_ms=getattr(response, "latency_ms", None),
                           response_meta={k: raw.get(k) for k in ("id", "request_id", "model", "created") if k in raw},
                           content=content, reasoning_content=response.message.get("reasoning_content"),
                           tool_calls=response.message.get("tool_calls"), **(meta or {}))
        return response.message


def run_finalization(caller: ModelCaller, *, run_log: RunLog, payload: dict, config: AgentConfig, cache=None,
                     documents_dir: Path | None = None, stop_reason: str | None, phase: str = "finalization",
                     model: str, request_path: Path | None = None, bundle: dict | None = None) -> dict:
    """Build the compact bundle from the run's events and make the no-tools finalization call(s).

    Never raises for API/parse problems (they are returned in `error`); lets
    KeyboardInterrupt and other BaseExceptions through to the caller.
    """
    if bundle is None:
        bundle = build_research_bundle(trace_events(run_log), payload, cache=cache, documents_dir=documents_dir,
                                       include_level3=config.include_level3,
                                       max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason,
                                       target_market=config.target_market)
    messages = finalizer_messages(bundle)
    input_chars = sum(len(m["content"]) for m in messages)
    before = dict(caller.usage["finalization"])
    info: dict[str, Any] = {"phase": phase, "model": model, "bundle_version": BUNDLE_VERSION,
                            "bundle_chars": bundle.get("bundle_chars"), "finalizer_input_chars": input_chars,
                            "excerpts_included": bundle["truncation"].get("excerpts_included"),
                            "evidence_items": len(bundle.get("evidence") or []),
                            "started_at": utc_now(), "request_path": str(request_path) if request_path else None}
    if request_path is not None:
        atomic_write_json(request_path, {"model": model, "phase": phase, "created_at": info["started_at"],
                                         "messages": messages})
    run_log.event("finalization_started", **{k: v for k, v in info.items() if k != "started_at"})
    t0 = time.monotonic()
    output, parse_note, text, repair_text, error, api_error = None, None, None, None, None, None
    try:
        message = caller(messages, phase=phase, model=model)
        text = message.get("content") or ""
        output, parse_note = parse_model_output(text)
        if output is None and text:
            # Technical repair only: ask once for valid JSON, still within the compact context.
            repair = messages + [_assistant_echo(message), {"role": "user", "content": REPAIR_PROMPT}]
            reply = caller(repair, phase=f"{phase}_repair", model=model)
            repair_text = reply.get("content") or ""
            output, parse_note = parse_model_output(repair_text)
            parse_note = f"repaired:{parse_note}"
    except Exception as exc:  # API failures, timeouts after the configured attempts, malformed responses
        error = _error_text(exc)
        api_error = exc.as_dict() if hasattr(exc, "as_dict") else None
        parse_note = "finalization_failed"
        run_log.event("finalization_failed", phase=phase, model=model, error=error, api_error=api_error)
    after = caller.usage["finalization"]
    used = {k: after[k] - before.get(k, 0) for k in after}
    info.update({
        "status": "failed" if error else ("parsed" if output is not None else "unparsed"),
        "error": error, "api_error": api_error, "parse_note": parse_note,
        "latency_ms": int((time.monotonic() - t0) * 1000), "model_latency_ms": used["model_latency_ms"],
        "model_calls": used["model_calls"], "prompt_tokens": used["prompt_tokens"],
        "completion_tokens": used["completion_tokens"], "total_tokens": used["total_tokens"],
        "raw_text": text, "repair_text": repair_text, "finished_at": utc_now(),
    })
    if not error:
        run_log.event("finalization_finished", phase=phase, model=model, status=info["status"],
                      parse_note=parse_note, latency_ms=info["latency_ms"],
                      usage={k: used[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
    return {"output": output, "parse_note": parse_note, "text": text, "error": error, "api_error": api_error,
            "bundle": bundle, "info": info}


def trace_events(run_log: RunLog) -> list[dict]:
    return read_events(run_log.events_path)


class ToolSession:
    """Executes tool calls for any phase (research or field recovery) with one shared context:
    one evidence store, one document list, one duplicate/novelty tracker, one tool-call log.

    An exact repeat of a read-only call already completed in this vehicle run (same canonical
    signature, see context.call_signature) is answered from the earlier result BEFORE dispatch:
    no HTTP request, no extraction, no billable search, no new document, no novelty. The model
    still gets a `role=tool` response, annotated as reused. The run logs a `tool_reused` event
    instead of tool_call/tool_result, so counters and cost only count real executions.
    """

    def __init__(self, ctx: ToolContext, run_log: RunLog, config: AgentConfig, cancel_event=None,
                 on_documents: Callable[[list[str], str], None] | None = None, specs: list[dict] | None = None):
        self.ctx, self.run_log, self.config = ctx, run_log, config
        self.specs = specs                 # the run's requested field specs (the document card's clusters)
        self.tracker = ResearchTracker()
        self.tool_calls: list[dict] = []
        self.step = 0                      # global tool-turn counter across phases
        self.cancel_event = cancel_event
        self.on_documents = on_documents   # e.g. the deterministic harvester (every phase, every new document)
        self.blocked = 0
        self.turn_results: list[dict] = []
        self.search_budget: SearchBudget | None = None   # set per cluster attempt; None = no per-attempt budget
        # set per cluster attempt: refuses a web route equivalent to one an earlier run found unproductive
        self.route_guard: Callable[[str, dict], dict | None] | None = None
        self.route_blocks = 0

    def execute(self, calls: list[dict], messages: list[dict], *, phase: str, allowed: tuple | None = None,
                **tags: Any):
        """Run one model turn's tool calls, append the tool messages, return the turn's novelty.

        `allowed`: tool names this phase may execute (e.g. the document sweep's cached-document tools).
        Any other call is answered with an error WITHOUT being executed: no HTTP request, no search."""
        self.step += 1
        step = self.step
        self.tracker.begin_turn(step)
        self.turn_results = []             # (name, result) of every call answered this turn, replayed or executed
        for call in calls:
            check_cancelled(self.cancel_event)
            fn = call.get("function") or {}
            name, raw_args = fn.get("name", ""), fn.get("arguments")
            call_id = call.get("id") or f"call_{len(self.tool_calls) + 1}"
            if allowed is not None and name not in allowed:
                self.blocked += 1
                result = {"error": "tool_not_allowed_in_phase", "phase": phase,
                          "message": f"{name} is not available in this step. Allowed: {', '.join(allowed)}."}
                self.run_log.event("tool_blocked", step=step, phase=phase, call_id=call.get("id"), name=name,
                                   arguments=raw_args, **tags)
                self.tool_calls.append({"step": step, "phase": phase, **tags, "name": name, "arguments": raw_args,
                                        "duration_ms": 0, "cache_hit": None, "error": result["error"],
                                        "document_id": None, "duplicate": False, "blocked": True})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
                continue
            signature = call_signature(name, raw_args)
            prior = self.tracker.lookup(signature)
            if prior is not None:
                result = replay_result(prior)
                if name == "fetch_url" and phase != "research":   # navigation links: research fetches only
                    result.pop("links", None)
                if name in FETCH_TOOLS and phase != "research":     # the document card: research fetches only
                    result.pop("document_card", None)
                self.turn_results.append({"name": name, "result": result, "reused": True})
                self.tracker.note_reused(name)
                self.run_log.event("tool_reused", step=step, phase=phase, call_id=call.get("id"), name=name,
                                   arguments=raw_args, **tags, original_step=prior["step"],
                                   original_phase=prior["phase"], original_field=prior.get("field"),
                                   original_attempt=prior.get("attempt"), signature=signature)
                self.tool_calls.append({
                    "step": step, "phase": phase, **tags, "name": name, "arguments": raw_args, "duration_ms": 0,
                    "cache_hit": None, "error": None, "document_id": result.get("document_id"),
                    "duplicate": True, "reused": True, "reused_from_step": prior["step"],
                })
                messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": model_view(result, self.config.max_tool_output_chars, duplicate=name in FETCH_TOOLS),
                    "_compact": compact_stub(name, raw_args, result, self.config.compact_tool_output_chars),
                })
                continue
            refusal = self.route_guard(name, trace.parse_args(raw_args)) \
                if self.route_guard is not None and name in trace.SEARCH_TOOLS + trace.FETCH_TOOLS else None
            if refusal is not None:
                self.route_blocks += 1
                self.blocked += 1
                result = {"error": "known_unproductive_route", **refusal,
                          "message": ("An earlier run already made this exact (or an equivalent) request for these "
                                      "fields and it gave no new usable material. Try a different query, source or "
                                      "document; nothing about the field's value is implied.")}
                self.run_log.event("tool_blocked", step=step, phase=phase, call_id=call.get("id"), name=name,
                                   arguments=raw_args, reason="known_unproductive_route", **refusal, **tags)
                self.tool_calls.append({"step": step, "phase": phase, **tags, "name": name, "arguments": raw_args,
                                        "duration_ms": 0, "cache_hit": None, "error": result["error"],
                                        "document_id": None, "duplicate": False, "blocked": True})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
                continue
            budget = self.search_budget if name in trace.SEARCH_TOOLS else None
            if budget is not None:
                from .tools.search import planned_provider_calls

                planned = planned_provider_calls(self.ctx, name, trace.parse_args(raw_args))
                if planned > budget.remaining:
                    budget.refused += 1
                    self.blocked += 1
                    result = {"error": "search_budget_exhausted", "planned_provider_calls": planned,
                              "remaining_provider_calls": budget.remaining,
                              "message": (f"{name} would make {planned} billable search(es); this attempt has "
                                          f"{budget.remaining} left. Use cached documents or the search_hints.")}
                    self.run_log.event("tool_blocked", step=step, phase=phase, call_id=call.get("id"), name=name,
                                       arguments=raw_args, reason="search_budget_exhausted", planned=planned,
                                       remaining=budget.remaining, **tags)
                    self.tool_calls.append({"step": step, "phase": phase, **tags, "name": name,
                                            "arguments": raw_args, "duration_ms": 0, "cache_hit": None,
                                            "error": result["error"], "document_id": None, "duplicate": False,
                                            "blocked": True})
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
                    continue
            misses_before = self.ctx.counters["search_cache_misses"]
            self.run_log.event("tool_call", step=step, phase=phase, call_id=call.get("id"), name=name,
                               arguments=raw_args, **tags)
            t_tool = time.monotonic()
            result = dispatch(self.ctx, name, raw_args)
            if phase == "research" and name in trace.SEARCH_TOOLS:
                adm = self.ctx.admission       # where to try first (src/acquisition.py): scheduling metadata only
                maker = getattr(adm, "manufacturer", None) or self.ctx.vehicle.get("manufacturer")
                annotate_search_result(result, maker, getattr(adm, "target_market", None) or self.config.target_market)
            if phase == "research" and name == "fetch_url" and self.config.acquisition_mode == "contract":
                self._attach_navigation_links(result)
            if budget is not None:
                budget.used += self.ctx.counters["search_cache_misses"] - misses_before
            elapsed = int((time.monotonic() - t_tool) * 1000)
            self.run_log.event("tool_result", step=step, phase=phase, call_id=call.get("id"), name=name,
                               result=result, **tags)
            duplicate, note = self.tracker.observe(name, raw_args, result, phase=phase)
            if duplicate:  # a near repeat that is not the same canonical operation (historical behavior)
                self.run_log.event("duplicate_work", step=step, phase=phase, name=name, arguments=raw_args,
                                   note=note, detected="after_execution", **tags)
            self.tracker.remember(signature, {"step": step, "phase": phase, **tags, "result": result})
            self.turn_results.append({"name": name, "result": result, "reused": False})
            if self.on_documents is not None:
                self.on_documents(self.ctx.documents_opened, phase)
            if (phase == "research" and name in CARD_TOOLS and self.config.acquisition_mode == "contract"
                    and self.config.acquisition_document_card):
                self._attach_document_card(result)   # after the harvest hook, before the tool message
            self.tool_calls.append({
                "step": step, "phase": phase, **tags, "name": name, "arguments": raw_args, "duration_ms": elapsed,
                "cache_hit": result.get("cache_hit") if isinstance(result, dict) else None,
                "error": result.get("error") if isinstance(result, dict) else None,
                "document_id": result.get("document_id") if isinstance(result, dict) else None,
                "duplicate": duplicate,
                "route_failed": _route_failed(result),
                **({"routes": _call_routes(self.ctx, name, raw_args, result)}
                   if name in trace.SEARCH_TOOLS + trace.FETCH_TOOLS else {}),
            })
            messages.append({
                "role": "tool", "tool_call_id": call_id,
                "content": model_view(result, self.config.max_tool_output_chars, duplicate=duplicate, note=note),
                "_compact": compact_stub(name, raw_args, result, self.config.compact_tool_output_chars),
            })
        return self.tracker.end_turn()

    def _attach_navigation_links(self, result: Any) -> None:
        """Contract-mode research: a fetched HTML page carries its ranked outbound links (src/acquisition.py
        rank_links), from the cached body; nothing is fetched. Navigation metadata only; never raises."""
        try:
            identity = getattr(self.ctx.admission, "identity", None)
            tokens = model_tokens(getattr(identity, "family", None), self.ctx.vehicle.get("model"))
            links = navigation_links(self.ctx, result, tokens)
        except Exception as exc:  # noqa: BLE001 - navigation hints must never cost the run
            self.run_log.event("navigation_links_failed", error=_error_text(exc))
            return
        if links is not None:
            result["links"] = links

    def _attach_document_card(self, result: Any) -> None:
        """Contract-mode research with ACQUISITION_DOCUMENT_CARD: a fetched document carries its server-computed
        document card (src/acquisition.py document_card): identity / scope metadata, never evidence. Never raises."""
        try:
            adm = self.ctx.admission
            card = document_card(adm=adm, cache=self.ctx.cache, specs=self.specs or [], result=result,
                                 target_market=getattr(adm, "target_market", None) or self.config.target_market)
        except Exception as exc:  # noqa: BLE001 - scheduling metadata must never cost the run
            self.run_log.event("document_card_failed", error=_error_text(exc),
                               document_id=result.get("document_id") if isinstance(result, dict) else None)
            return
        if card is not None:
            result["document_card"] = card
            self.run_log.event("document_card", document_id=result.get("document_id"), card=card)


def early_done_note(base: dict, missing: str = "") -> str:
    """The operational note after a deferred contract-mode "done" (the minimum acquisition base is unmet)."""
    return ("[operational note] Research cannot end yet: the acquired source set is still thin "
            f"({base.get('useful_documents')} useful document(s), minimum {base.get('min_documents')}; "
            f"{base.get('scoped_coverage_pct')}% of the requested fields have a candidate from a source in their market "
            f"scope, minimum {base.get('min_scoped_coverage_pct')}%). " + (f"{missing} " if missing else "")
            + "Acquire NEW sources now (official importer / manufacturer specification pages or PDFs, target-market "
              'price lists or brochures). Reply {"done": true, ...} again only if nothing more can be found.')


def _doc_metas(events: list[dict], cache, documents_dir: Path) -> list[dict]:
    from .bundle import document_meta

    event_meta = trace.document_event_meta(events)
    return [document_meta(d, cache, documents_dir, event_meta) for d in trace.document_ids(events)]


def run_field_recovery(*, session: ToolSession, caller: ModelCaller, specs: list[dict], payload: dict,
                       config: AgentConfig, run_log: RunLog, cache, documents_dir: Path,
                       operator_notes: dict | None, phase_ref: dict, vehicle: dict | None = None) -> dict:
    """Detect failed requested fields from the research state, then run focused retries for them only.

    Generic over whatever fields were requested. BREADTH-FIRST: round 1 gives every queued field its
    first attempt (A1, B1, C1, ...); round 2 only revisits fields still unresolved (A2, C2, ...), so a
    second attempt can never starve a field that has not had its first. An attempt may still use up to
    FIELD_RECOVERY_MAX_STEPS model turns. The queue is dynamic: after EVERY attempt all requested
    fields are re-evaluated, and a queued field another field's recovery resolved (e.g. by-product
    evidence from the same spec table) is skipped without its own model call.
    KeyboardInterrupt propagates; an API failure stops further retries (the run still finalizes).
    """
    from .candidate_harvest import candidate_matrix
    from .document_sweep import compact_candidate

    events = trace_events(run_log)
    searches_before = session.ctx.counters["search_cache_misses"]
    billable_before = session.ctx.counters["search_api_calls"]
    primary = current_evaluation(events, specs, config.target_market)
    run_log.event("field_evaluation", stage="primary", fields=primary,
                  summary=_state_counts(primary))
    queue = retry_queue(primary, specs, config.field_recovery_max_attempts) if config.field_recovery_enabled else []
    run_log.event("field_retry_queue", fields=[q["field"] for q in queue], queue=queue,
                  enabled=config.field_recovery_enabled, order="breadth_first")
    by_name = {s["name"]: s for s in specs}
    queued = [q["field"] for q in queue]
    current = {e["field"]: e for e in primary}
    attempts_log: list[dict] = []
    resolved_directly: list[str] = []
    resolved_indirectly: dict[str, dict] = {}
    previous_by_field: dict[str, list[dict]] = {name: [] for name in queued}
    state = {"total_steps": 0, "stopped": None, "cut_short": None, "early_count": 0, "budget_skipped": 0}

    def reevaluate(during_field: str, attempt: int) -> None:
        """Refresh the state of every requested field; record queued fields another recovery resolved."""
        latest = current_evaluation(trace_events(run_log), specs, config.target_market)
        for entry in latest:
            name = entry["field"]
            was = current[name]
            current[name] = entry
            if (name in queued and name != during_field and name not in resolved_indirectly
                    and name not in resolved_directly and was["retry_eligible"] and not entry["retry_eligible"]):
                resolved_indirectly[name] = {"resolved_during_field": during_field, "attempt": attempt,
                                             "state": entry["state"]}
                run_log.event("field_recovery_queue_resolved_indirectly", field=name,
                              resolved_during_field=during_field, attempt=attempt, state=entry["state"])

    def cap_reached() -> bool:
        cap = config.field_recovery_max_total_steps
        return bool(cap and state["total_steps"] >= cap)

    def run_attempt(item: dict, attempt: int, round_no: int) -> None:
        name = item["field"]
        previous = previous_by_field[name]
        events = trace_events(run_log)
        before = current[name]
        packet = retry_packet(spec=by_name[name], evaluation=before, events=events, payload=payload,
                              doc_metas=_doc_metas(events, cache, documents_dir), attempt=attempt,
                              max_attempts=item["max_attempts"], max_steps=config.field_recovery_max_steps,
                              target_market=config.target_market, previous_attempts=previous,
                              operator_notes=operator_notes,
                              excerpt_limits={"max_items": config.field_recovery_prior_excerpts_max_items,
                                              "max_chars": config.field_recovery_prior_excerpts_max_chars,
                                              "max_excerpt_chars": config.field_recovery_prior_excerpt_max_chars})
        packet["other_requested_fields"] = {s["name"]: s.get("description") for s in specs
                                            if s["name"] != name and s.get("applicable", True)}
        if config.layered_harvest_enabled and config.field_recovery_candidates_per_field:
            matrix = candidate_matrix(events, [by_name[name]], vehicle,
                                      per_field=config.field_recovery_candidates_per_field)
            cands = matrix["fields"].get(name) or []
            if cands:
                packet["deterministic_candidates"] = [compact_candidate(c) for c in cands]
        run_log.event("field_recovery_started", field=name, attempt=attempt, max_attempts=item["max_attempts"],
                      round=round_no, failure_reason=before["state"], turn_budget=config.field_recovery_max_steps,
                      already_attempted_operations=len(packet["already_attempted_operations"]),
                      prior_excerpt_items=packet["prior_excerpt_stats"]["items"],
                      prior_excerpt_chars=packet["prior_excerpt_stats"]["chars"],
                      prior_excerpts=packet["prior_relevant_excerpts"],
                      deterministic_candidates=len(packet.get("deterministic_candidates") or []),
                      packet_chars=len(json.dumps(packet, ensure_ascii=False, default=str)))
        phase_ref["name"] = "field_recovery"
        messages = [{"role": "system", "content": FIELD_RECOVERY_SYSTEM_PROMPT},
                    {"role": "user", "content": "Field recovery task (JSON):\n"
                                                + json.dumps(packet, ensure_ascii=False, default=str)}]
        reply_text, turns, error, early = None, 0, None, False
        idle = 0
        budget = max(1, config.field_recovery_max_steps)
        try:
            for turn_index in range(1, budget + 1):
                if cap_reached():
                    state["stopped"] = "max_total_steps"   # hard per-vehicle cap: no further model turn
                    state["cut_short"] = name
                    break
                meta = {"field": name, "attempt": attempt, "max_attempts": item["max_attempts"],
                        "turn": turn_index, "turn_budget": budget}
                message = caller(outgoing_messages(messages, config), phase="field_recovery",
                                 tools=tool_specs(), meta=meta)
                turns += 1
                state["total_steps"] += 1
                messages.append(_assistant_echo(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    reply_text = message.get("content") or ""
                    break
                # A turn whose calls were all replayed (or found nothing new) is idle, so a model that
                # keeps re-emitting the same call ends within AGENT_NO_NEW_RESEARCH_TURNS.
                novelty = session.execute(calls, messages, phase="field_recovery", field=name, attempt=attempt)
                # Early success: if the evidence stored by this turn already makes the target field `ok`,
                # end the attempt now instead of paying for a model turn that only says "found".
                # Only `ok` short-circuits; every other state still needs model work or an explicit reply.
                may_stop, now = early_resolution_check(by_name[name], trace_events(run_log), config.target_market)
                if may_stop:
                    early = True
                    state["early_count"] += 1
                    state["budget_skipped"] += budget - turn_index
                    run_log.event("field_recovery_early_resolved", field=name, attempt=attempt,
                                  after_turn=turn_index, state="ok", turn_budget=budget,
                                  turn_budget_skipped=budget - turn_index, evidence_ids=now["evidence_ids"])
                    break
                idle = 0 if novelty.total else idle + 1
                if config.no_new_research_turns and idle >= config.no_new_research_turns:
                    break
        except GLMError as exc:  # stop spending on retries; finalize with what we have
            error = _error_text(exc)
            state["stopped"] = "api_failure"
            run_log.event("field_recovery_failed", field=name, attempt=attempt, error=error,
                          api_error=exc.as_dict())
        reply = parse_retry_reply(reply_text, name)
        if reply and reply.get("status"):
            cited = reply.get("evidence_ids")
            run_log.event("field_status", field=name, status=str(reply["status"]).lower(),
                          note=reply.get("notes"), source=f"field_recovery_attempt_{attempt}",
                          evidence_ids=[str(i) for i in cited] if isinstance(cited, list) else [])
        reevaluate(name, attempt)
        after = current[name]
        if not after["retry_eligible"] and name not in resolved_directly:
            resolved_directly.append(name)
        excerpt_docs = {e.get("document_id") for e in packet["prior_relevant_excerpts"] if e.get("document_id")}
        rereads = sum(1 for c in session.tool_calls
                      if c.get("phase") == "field_recovery" and c.get("field") == name
                      and c.get("attempt") == attempt and c["name"] in ("get_cached_document", "extract_html")
                      and _call_document(c) in excerpt_docs)
        record = {"field": name, "attempt": attempt, "round": round_no, "state_before": before["state"],
                  "state_after": after["state"], "turns": turns, "reply": reply,
                  "reply_text": None if reply else reply_text, "error": error, "early_resolved": early,
                  "packet_chars": len(json.dumps(packet, ensure_ascii=False, default=str)),
                  "prior_excerpt_items": packet["prior_excerpt_stats"]["items"],
                  "prior_excerpt_chars": packet["prior_excerpt_stats"]["chars"],
                  "deterministic_candidates": len(packet.get("deterministic_candidates") or []),
                  "document_rereads_after_prior_excerpt": rereads}
        attempts_log.append(record)
        previous.append({k: record[k] for k in ("attempt", "state_after", "reply", "turns")})
        run_log.event("field_recovery_finished", **{k: v for k, v in record.items() if k != "_routes"})

    rounds = max((item["max_attempts"] for item in queue), default=0)
    for round_no in range(1, rounds + 1):
        for item in queue:
            name = item["field"]
            if item["max_attempts"] < round_no or not current[name]["retry_eligible"]:
                continue  # out of attempts, or resolved (directly or by another field's recovery)
            if cap_reached():
                state["stopped"] = "max_total_steps"   # hard per-vehicle cap: no further attempt is started
                break                                  # (not "cut short": no attempt of this field was running)
            run_attempt(item, round_no, round_no)
            if state["stopped"]:
                break
        if state["stopped"]:
            break
    total_steps, stopped, cut_short = state["total_steps"], state["stopped"], state["cut_short"]
    final = [current[e["field"]] for e in primary]
    run_log.event("field_evaluation", stage="after_recovery", fields=final, summary=_state_counts(final))
    retried = sorted({a["field"] for a in attempts_log})
    cap = config.field_recovery_max_total_steps
    not_attempted = [f for f in queued if f not in retried and current[f]["retry_eligible"]]
    if cut_short and not current[cut_short]["retry_eligible"]:
        cut_short = None
    if stopped == "max_total_steps":
        run_log.event("field_recovery_budget_exhausted", turn_budget=cap, turns_used=total_steps,
                      fields_not_attempted=not_attempted, field_cut_short=cut_short)
    first = sorted({a["field"] for a in attempts_log if a["attempt"] == 1})
    resolved_queue = [f for f in queued if not current[f]["retry_eligible"]]
    return {
        "enabled": config.field_recovery_enabled,
        "mode": "legacy",
        "order": "breadth_first",
        "field_recovery_turn_budget": cap or None,
        "field_recovery_turns_used": total_steps,
        "field_recovery_turns_remaining": max(0, cap - total_steps) if cap else None,
        "fields_not_attempted_due_to_budget": not_attempted if stopped == "max_total_steps" else [],
        "field_cut_short_by_budget": cut_short,
        "requested": len(specs),
        "primary_states": _state_counts(primary),
        "final_states": _state_counts(final),
        "queue": queued,
        "fields_retried": retried,
        "fields_recovered": [f for f in retried if not current[f]["retry_eligible"]],
        "fields_still_failed": [f for f in retried if current[f]["retry_eligible"]],
        "fields_resolved_directly": [f for f in resolved_directly if not current[f]["retry_eligible"]],
        "fields_resolved_indirectly": resolved_indirectly,
        "fields_not_attempted": not_attempted,
        "attempts": attempts_log,
        "attempt_count": len(attempts_log),
        "attempt_order": [f"{a['field']}#{a['attempt']}" for a in attempts_log],
        "turns": total_steps,
        "early_resolution_count": state["early_count"],
        "turn_budget_skipped_by_early_resolution": state["budget_skipped"],
        "turns_saved_by_early_resolution": state["early_count"],   # deprecated alias of early_resolution_count
        "prior_excerpt_items": sum(a.get("prior_excerpt_items") or 0 for a in attempts_log),
        "prior_excerpt_chars": sum(a.get("prior_excerpt_chars") or 0 for a in attempts_log),
        "attempts_with_prior_excerpts": sum(1 for a in attempts_log if a.get("prior_excerpt_items")),
        "document_rereads_after_prior_excerpt": sum(a.get("document_rereads_after_prior_excerpt") or 0
                                                    for a in attempts_log),
        # coverage of the breadth-first schedule (observational)
        "recovery_fields_given_first_attempt": len(first),
        "recovery_fields_never_attempted": len([f for f in queued if f not in retried]),
        "recovery_second_attempts_started": sum(1 for a in attempts_log if a["attempt"] >= 2),
        "recovery_unique_fields_touched": len(retried),
        "recovery_unique_fields_resolved": len(resolved_queue),
        "recovery_resolution_per_turn": round(len(resolved_queue) / total_steps, 3) if total_steps else None,
        "fields_never_attempted_due_to_budget": not_attempted if stopped == "max_total_steps" else [],
        **tail_metrics(primary, final, model_calls=total_steps,
                       search_calls=session.ctx.counters["search_cache_misses"] - searches_before,
                       billable_search_calls=session.ctx.counters["search_api_calls"] - billable_before),
        "stopped": stopped,
        "evaluation_primary": primary,
        "evaluation_final": final,
    }


def _trim_packet(packet: dict, max_chars: int) -> dict:
    """Fit a cluster packet under its size cap: drop the lowest-ranked documents, then search hints, then the
    oldest operations, then candidates beyond the first per field. Fields, conflicts and evidence are kept."""
    def size() -> int:
        return len(json.dumps(packet, ensure_ascii=False, default=str))

    for key, floor in (("ranked_documents", 3), ("search_hints", 2), ("already_attempted_operations", 5)):
        while size() > max_chars and len(packet.get(key) or []) > floor:
            packet[key] = packet[key][:-1] if key != "already_attempted_operations" else packet[key][1:]
    cands = packet.get("deterministic_candidates") or {}
    while size() > max_chars and any(len(v) > 1 for v in cands.values()):
        name = max(cands, key=lambda n: len(cands[n]))
        cands[name] = cands[name][:-1]
    return packet


def _cluster_reply(text: str | None, cluster: str) -> dict | None:
    parsed, _ = parse_model_output(text)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("cluster") and str(parsed["cluster"]) != cluster:
        return None
    return parsed


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def _route_failed(result: Any) -> bool:
    """Did a web call fail as a call (HTTP error status, or every domain of a domain search erroring)? Such a call
    says nothing about whether the route has material."""
    if not isinstance(result, dict):
        return True
    if result.get("error"):
        return True
    status = result.get("status")
    if isinstance(status, int) and status >= 400:
        return True
    domains = result.get("per_domain")
    if isinstance(domains, dict) and domains and all(isinstance(v, dict) and v.get("error") for v in domains.values()):
        return True
    return False


def _call_routes(ctx: ToolContext, name: str, raw_args: Any, result: Any) -> list[dict]:
    """The provider routes an executed web call made (src/research_memory.provider_routes, with this session's search
    provider and the vehicle's default official domains)."""
    from .research_memory import provider_routes
    from .tools.search import default_domains, search_provider

    return provider_routes(name, trace.parse_args(raw_args), provider=search_provider(ctx),
                           default_domains=default_domains(ctx.vehicle), result=result)


def _routes_of(calls: list[dict]) -> list[dict]:
    """The provider routes of executed tool calls that worked (each call's `routes`, recorded when it ran): a 429, an
    HTTP error or any other failed call says nothing about the route and is never remembered as unproductive; a domain
    search records only the domains whose provider search ran. A call without recorded routes contributes only what is
    identifiable from its arguments alone (fetches; never a search, whose provider is unknown)."""
    from .research_memory import provider_routes

    routes = []
    for c in calls:
        if c.get("blocked") or c.get("reused") or c.get("error") or c.get("route_failed") \
                or c["name"] not in trace.SEARCH_TOOLS + trace.FETCH_TOOLS:
            continue
        routes += c["routes"] if "routes" in c else provider_routes(c["name"], trace.parse_args(c.get("arguments")))
    return routes


def _route_guard(negative: dict[str, dict], open_fields: list[str], specs_by_name: dict, all_specs: list[dict],
                 default_domains: list[str] | None = None, provider: tuple | None = None):
    """A dispatch guard for one cluster attempt. It refuses a web call only when an earlier run recorded EVERY provider
    route the call would make as unproductive for every open field the call serves (src/research_memory.route_identity:
    a search is its provider, normalized query, domain and effective result count, a domain search one route per
    domain; a fetch is its mechanism and normalized URL, plus the effective wait of a render). A search serves the
    open fields its query names (all of them when it names none); a fetched page serves them all. Another provider,
    domain, result window, fetch mechanism or render wait is another route and runs; so does a domain search with any
    domain not recorded. It never stops research as such, and implies nothing about a field's value."""
    from .research_memory import provider_routes, route_signature

    dead: dict[str, set[str]] = {}
    for field in open_fields:
        for route in (negative.get(field) or {}).get("routes") or []:
            signature = route_signature(route)          # recomputed from the record; unidentifiable records: none
            if signature:
                dead.setdefault(signature, set()).add(field)

    def guard(name: str, args: dict) -> dict | None:
        planned = provider_routes(name, args, provider=provider, default_domains=default_domains)
        if not planned:
            return None
        route = args.get("query") if name in trace.SEARCH_TOOLS else args.get("url")
        if name in trace.SEARCH_TOOLS:
            served = [f for f in open_fields
                      if _route_names_field({"tool": name, "route": route}, specs_by_name.get(f) or {"name": f},
                                            all_specs)] or list(open_fields)
        else:
            served = list(open_fields)
        if not all(set(served) <= dead.get(r["signature"], set()) for r in planned):
            return None                         # a route never tried for an open field: it runs
        refusal = {"route": str(route)[:300], "fields": sorted(served)}
        domains = [r["domain"] for r in planned if r.get("domain")]
        if domains:
            refusal["domains"] = domains
        return refusal

    return guard


def _label_match(query: str, spec: dict) -> int:
    labels = (spec.get("aliases_en") or []) + (spec.get("aliases_he") or []) + [spec.get("name", "").replace("_", " ")]
    return max((len(str(a)) for a in labels if a and f" {str(a).lower()} " in query), default=0)


def _route_names_field(route: dict, spec: dict, all_specs: list[dict]) -> bool:
    """Is a failed SEARCH about this field? Its query must use one of the field's labels, and no other field's label
    may match it more specifically ("dc fast charging time" is about DC, not AC charging; "battery warranty" is a
    warranty). A fetched URL is not tied to one field and never alone makes a field's routes unproductive."""
    if route.get("tool") not in trace.SEARCH_TOOLS:
        return False
    query = " " + " ".join(str(route.get("route") or "").lower().split()) + " "
    mine = _label_match(query, spec)
    return mine > 0 and mine >= max((_label_match(query, s) for s in all_specs), default=0)


def _record_recovery_memory(memory: ResearchMemory, attempts: list[dict], current: dict, route_keys: dict,
                            target, run_log: RunLog, specs_by_name: dict, route_ids: dict,
                            all_specs: list[dict]) -> None:
    """After a cluster recovery: failed routes (a web attempt with no novelty at all) per still-open field, and one
    yield row per cluster attempt / field. Scheduling memory only."""
    from .tail_planner import cluster_of

    entries = []
    for a in attempts:
        if a.get("mode") != "web" or not a.get("_routes"):
            continue
        for f in a["fields"]:
            spec = specs_by_name.get(f) or {"name": f}
            searched = [r for r in a["_routes"] if _route_names_field(r, spec, all_specs)]
            if current[f]["retry_eligible"] and f in route_keys and searched:
                entries.append({"scope_key": route_keys[f], "spec_identity": route_ids.get(f),
                                "cluster": a["cluster"], "field": f,
                                "routes": searched + [r for r in a["_routes"] if r["tool"] in trace.FETCH_TOOLS],
                                "outcome": "no_new_material"})
    run_key = str(run_log.dir)
    memory.record_routes(entries, run_key)
    base = {"manufacturer": getattr(target, "manufacturer", None), "propulsion": getattr(target, "propulsion", None)}
    rows = [{"kind": "cluster", **base, "cluster": a["cluster"], "mode": a["mode"], "turns": a["turns"],
             "searches": a.get("search_provider_calls", 0), "documents": a.get("documents_fetched", 0),
             "resolutions": len(a.get("fields_resolved") or []),
             "byproduct_resolutions": len(a.get("byproduct_resolutions") or []), "tokens": a.get("tokens", 0)}
            for a in attempts]
    from .source_authority import split_host

    family: dict[str, str] = {}
    for item in trace.evidence_items(trace_events(run_log)):
        name = normalize_field_name(item.get("field"))
        if item.get("source_domain") and name not in family:
            family[name] = split_host(item["source_domain"])[1]       # registrable label: the source family
    for name in sorted({f for a in attempts for f in a["fields"]}):
        mine = [a for a in attempts if name in a["fields"]]
        rows.append({"kind": "field", **base, "field": name, "cluster": cluster_of(specs_by_name.get(name) or {}),
                     "source_family": family.get(name),
                     "attempts": len(mine), "turns": sum(a["turns"] for a in mine),
                     "searches": sum(a.get("search_provider_calls", 0) for a in mine),
                     "resolved": not current[name]["retry_eligible"]})
    memory.record_yield(rows, run_key)


def tail_metrics(primary: list[dict], final: list[dict], *, model_calls: int, search_calls: int,
                 billable_search_calls: int | None = None) -> dict:
    """Tail efficiency (observational; same definitions for legacy and cluster recovery). `search_calls` are
    provider searches made (cache misses, including errors and keyless backends); `billable_search_calls` the
    priced web_search calls."""
    start = {e["field"] for e in primary if e["retry_eligible"]}
    remaining = {e["field"] for e in final if e["retry_eligible"]}
    resolved = sorted(start - remaining)
    # verdicts on fields whose policy allows portability at all (a never-portable field is not a "rejection")
    from .market_portability import POLICY_ONLY_BASES

    portability = [v for e in final for v in (e.get("portability") or {}).values()
                   if v.get("portability_basis") not in POLICY_ONLY_BASES]
    return {
        "tail_fields_at_start": len(start),
        "tail_fields_resolved": len(resolved),
        "tail_fields_remaining": len(remaining),
        "tail_fields_resolved_list": resolved,
        "tail_model_calls": model_calls,
        "tail_search_calls": search_calls,
        "tail_billable_search_calls": search_calls if billable_search_calls is None else billable_search_calls,
        # conflicts the evaluator already normalized when the tail started: no recovery search was spent on them
        "conflicts_normalized_without_search": sum(1 for e in primary if any(
            str(i).startswith("conflict_normalized:") for i in e.get("info") or [])),
        "portable_facts_accepted": sum(1 for v in portability if v.get("portable_to_target_market")),
        "portable_facts_rejected": sum(1 for v in portability if not v.get("portable_to_target_market")),
        "fields_resolved_per_tail_turn": round(len(resolved) / model_calls, 3) if model_calls else None,
        "fields_resolved_per_tail_search": round(len(resolved) / search_calls, 3) if search_calls else None,
    }


# consecutive cluster attempts failing with an API error (timeout, 5xx, ...) before all recovery stops (provider outage)
RECOVERY_API_FAILURE_STOP = 2


def run_cluster_recovery(*, session: ToolSession, caller: ModelCaller, specs: list[dict], payload: dict,
                         config: AgentConfig, run_log: RunLog, cache, documents_dir: Path,
                         operator_notes: dict | None, phase_ref: dict, vehicle: dict | None = None,
                         memory: ResearchMemory | None = None) -> dict:
    """Clustered tail recovery (see src/tail_planner.py). The recovery unit is a CLUSTER of related open fields.

    BREADTH-FIRST over clusters: every cluster gets attempt 1 before any cluster gets attempt 2, so one hard field
    cannot spend the global budget first. Each attempt is local-only (cached-document tools) while the cluster has
    candidates no model has reviewed, else web. An attempt starts with `cluster_base_turns` turns and gets another
    (up to `cluster_max_turns`, never above 4) only after a turn with real novelty; a base budget that ends without
    novelty stops the attempt. Billable searches are budgeted per attempt in provider calls. After every turn all
    requested fields are re-evaluated with current_evaluation() (new documents were already harvested for ALL fields
    by the session hook), every cluster is pruned, and the model is told which fields are resolved and which new
    candidates appeared, before any further paid turn. Field states come only from current_evaluation().
    """
    from .candidate_harvest import candidate_matrix
    from .conflict_normalizer import resolver_packet
    from .document_sweep import compact_candidate
    from .field_recovery import related_searches, vehicle_identity
    from .tail_planner import (LOCAL_TOOLS, candidate_key, candidates_fresh_first, cluster_packet, fresh_candidates,
                               novelty, plan_clusters, rank_documents, search_hints, snapshot, triage,
                               usable_candidate_matrix)

    def usable_matrix(events: list[dict]) -> dict:
        # candidates of unusable documents (a 404 / 403 error page, an empty fetch) are never presented, fresh or
        # triaged (src/tail_planner.usable_candidate_matrix)
        return usable_candidate_matrix(candidate_matrix(events, specs, vehicle), cache)

    market = config.target_market
    adm = session.ctx.admission
    billable_before = session.ctx.counters["search_api_calls"]
    by_name = {s["name"]: s for s in specs}
    identity = vehicle_identity(payload, market)
    events = trace_events(run_log)
    primary = current_evaluation(events, specs, market)
    run_log.event("field_evaluation", stage="primary", fields=primary, summary=_state_counts(primary))
    matrix = usable_matrix(events)
    triaged = triage(primary, specs, matrix, events, config.field_recovery_max_attempts) \
        if config.field_recovery_enabled else {}
    clusters = plan_clusters(triaged, specs)
    queued = [f for c in clusters for f in c["fields"]]
    run_log.event("field_retry_queue", fields=queued, enabled=config.field_recovery_enabled,
                  order="breadth_first_clusters", mode="cluster",
                  queue=[{"field": f, **triaged[f]} for f in queued], clusters=clusters,
                  policy_blocked=[f for f, t in triaged.items() if t["triage"] == "policy_blocked"])
    current = {e["field"]: e for e in primary}
    state = {"total_steps": 0, "stopped": None, "cut_short": None, "early_count": 0, "budget_skipped": 0,
             "no_novelty_stops": 0, "budget_extensions": 0, "searches": 0, "search_refusals": 0,
             "negative_route_blocks": 0, "failed_attempts": 0, "consecutive_api_failures": 0}
    # Cross-run memory (scheduling only): routes that already failed for these fields in the same identity scope,
    # and the historical yield of each cluster for this manufacturer / propulsion.
    from .research_memory import reuse_level, route_applies, route_label, scope_key, spec_identity
    from .tools.search import default_domains, search_provider

    provider = search_provider(session.ctx)

    target = getattr(adm, "identity", None)
    # time-sensitive fields (price, fees, warranty) keep no negative memory: a page that had nothing may have it now
    route_keys = {f: k for f in queued if not by_name[f].get("time_sensitive")
                  and (k := scope_key(target, reuse_level(by_name[f]) or "exact_market_trim"))}
    route_ids = {f: spec_identity(by_name[f]) for f in route_keys}
    negative, history = {}, {}
    if memory is not None:
        try:
            negative = memory.negative_routes(route_keys, route_ids)
            history = memory.cluster_yield(getattr(target, "manufacturer", None), getattr(target, "propulsion", None))
        except Exception as exc:  # unreadable memory never costs the recovery
            run_log.event("research_memory_read_failed", error=_error_text(exc))
    negative_shown: set[str] = set()
    if negative or history:
        run_log.event("research_memory_consulted", negative_route_fields=sorted(negative),
                      cluster_yield=history, note="scheduling only: never evidence, never a field state")
    attempts_log: list[dict] = []
    attempted_fields: set[str] = set()
    resolved_by_cluster: dict[str, list[str]] = {}
    resolved_indirectly: dict[str, dict] = {}
    low_yield: set[str] = set()
    local_done: set[str] = set()
    previous: dict[str, list[dict]] = {c["cluster"]: [] for c in clusters}
    ceiling = _clamp(config.cluster_max_turns, CLUSTER_TURN_CEILING, 1, CLUSTER_TURN_CEILING)
    base = _clamp(config.cluster_base_turns, 2, 1, ceiling)
    allowed_attempts = {c["cluster"]: min(max(0, int(config.cluster_max_attempts)),
                                          max(triaged[f]["max_attempts"] for f in c["fields"])) for c in clusters}

    def cap_reached() -> bool:
        cap = config.field_recovery_max_total_steps
        return bool(cap and state["total_steps"] >= cap)

    def reevaluate(cluster: str, attempt: int) -> None:
        latest = current_evaluation(trace_events(run_log), specs, market)
        for entry in latest:
            name = entry["field"]
            was = current[name]
            current[name] = entry
            if name in queued and was["retry_eligible"] and not entry["retry_eligible"]:
                resolved_by_cluster.setdefault(cluster, []).append(name)
                if name not in by_cluster.get(cluster, ()):
                    resolved_indirectly.setdefault(name, {"resolved_during_field": f"cluster:{cluster}",
                                                          "attempt": attempt, "state": entry["state"]})
                    run_log.event("field_recovery_queue_resolved_indirectly", field=name,
                                  resolved_during_field=f"cluster:{cluster}", attempt=attempt, state=entry["state"])

    by_cluster = {c["cluster"]: set(c["fields"]) for c in clusters}

    def run_attempt(plan: dict, attempt: int, round_no: int, local: bool) -> None:
        name = plan["cluster"]
        open_fields = [f for f in plan["fields"] if current[f]["retry_eligible"]]
        events = trace_events(run_log)
        matrix = usable_matrix(events)
        tri = triage(list(current.values()), specs, matrix, events, config.field_recovery_max_attempts, low_yield)
        mode = "local_only" if local else "web"
        attempted_fields.update(open_fields)
        budget = None if local else SearchBudget(max(0, int(config.cluster_search_budget)))
        session.search_budget = budget
        model_tools = [s for s in tool_specs() if s["function"]["name"] in LOCAL_TOOLS] if local else tool_specs()
        conflicts = {}
        for f in open_fields:
            if current[f]["state"] == "conflicting":
                ids = {str(i) for i in current[f].get("conflict_evidence_ids") or []}
                items = [i for i in trace.evidence_items(events) if str(i.get("evidence_id")) in ids]
                conflicts[f] = resolver_packet(spec=by_name[f], items=items,
                                               classification=current[f].get("conflict_class") or {},
                                               identity=identity, prior_queries=related_searches(events, by_name[f]))
        doc_metas = _doc_metas(events, cache, documents_dir)
        ranked = rank_documents(doc_metas=doc_metas, matrix=matrix, fields=open_fields, events=events, adm=adm,
                                cache=cache, target_market=market)
        hints = [] if local else search_hints(
            events=events, specs=[by_name[f] for f in open_fields], identity=identity,
            manufacturer=(adm.manufacturer if adm is not None else None),
            opened_urls=[m.get("final_url") or m.get("url") for m in doc_metas] + [m.get("url") for m in doc_metas])
        other_open = [f for f in queued if f not in open_fields and current[f]["retry_eligible"]]
        turn_budget = {"base": base, "max": ceiling,
                       "rule": "turns beyond base only after a turn with real novelty; none at all after base "
                               "without novelty"}
        # unseen candidates first: the candidates that justify a local pass are the ones it shows
        ordered_cands = candidates_fresh_first(matrix, events, open_fields)
        max_attempts = allowed_attempts[name] + (1 if name in local_done else 0)
        packet = cluster_packet(
            cluster=name, fields=open_fields, specs=specs, evaluation=list(current.values()), triaged=tri,
            events=events, payload=payload, target_market=market, matrix=matrix, ranked_documents=ranked,
            hints=hints, mode=mode, attempt=attempt, max_attempts=max_attempts, turn_budget=turn_budget,
            search_budget=budget.limit if budget else None, previous_attempts=previous[name],
            other_open_fields=other_open, conflicts=conflicts,
            candidates_per_field=config.cluster_candidates_per_field, operator_notes=operator_notes,
            candidates=ordered_cands)
        # routes this session could repeat (a search recorded with another provider is not one), as the guard sees them
        known_dead = {f: labels for f in open_fields if f in negative
                      and (labels := [route_label(r) for r in negative[f]["routes"] if route_applies(r, provider)][:8])}
        if known_dead and not local:
            packet["known_unproductive_routes"] = known_dead      # earlier runs: these routes found nothing new
            negative_shown.update(known_dead)
        session.route_guard = _route_guard(negative, open_fields, by_name, specs,
                                           default_domains(session.ctx.vehicle), provider) \
            if (negative and not local and config.negative_route_blocking) else None
        packet = _trim_packet(packet, config.cluster_packet_max_chars)
        shown = packet.get("deterministic_candidates") or {}
        presented = [candidate_key(c) for f in open_fields for c in (ordered_cands.get(f) or [])[:len(shown.get(f)
                                                                                                        or [])]]
        packet_chars = len(json.dumps(packet, ensure_ascii=False, default=str))
        states_before = {f: current[f]["state"] for f in open_fields}
        run_log.event("cluster_recovery_started", cluster=name, attempt=attempt, round=round_no, mode=mode,
                      fields=open_fields, triage={f: (tri.get(f) or {}).get("triage") for f in open_fields},
                      max_attempts=max_attempts, turn_budget=base, turn_ceiling=ceiling,
                      search_budget=budget.limit if budget else 0, offered_candidate_keys=presented,
                      ranked_documents=[{k: d.get(k) for k in ("document_id", "source_yield_score")} for d in ranked],
                      search_hints=len(hints), conflicts=sorted(conflicts), packet_chars=packet_chars)
        phase_ref["name"] = "field_recovery"
        messages = [{"role": "system", "content": CLUSTER_RECOVERY_SYSTEM_PROMPT},
                    {"role": "user", "content": "Cluster recovery task (JSON):\n"
                                                + json.dumps(packet, ensure_ascii=False, default=str)}]
        reply_text, turns, error, early, stop = None, 0, None, False, None
        turn_novelty: list[list[str]] = []
        calls_before = len(session.tool_calls)
        dead_routes: list[dict] = []
        tokens_before = caller.usage["field_recovery"].get("total_tokens", 0)
        extensions = 0
        searches_before = session.ctx.counters["search_cache_misses"]
        label = f"cluster:{name}"
        announced_keys: list[str] = []     # candidates added to the NEXT request: presented once that call returns
        try:
            for turn_index in range(1, ceiling + 1):
                if cap_reached():
                    state["stopped"], state["cut_short"] = "max_total_steps", label
                    break
                if turn_index > base:
                    extensions += 1
                    state["budget_extensions"] += 1
                    run_log.event("cluster_recovery_budget_extended", cluster=name, attempt=attempt,
                                  turn=turn_index, novelty=turn_novelty[-1])
                events = trace_events(run_log)
                watched = [f for f in queued if current[f]["retry_eligible"]]
                before = snapshot(events=events, evaluation=list(current.values()),
                                  documents=list(session.ctx.documents_opened), open_fields=watched,
                                  matrix=usable_matrix(events))
                meta = {"field": label, "cluster": name, "fields": open_fields, "attempt": attempt,
                        "max_attempts": max_attempts, "turn": turn_index, "turn_budget": base,
                        "turn_ceiling": ceiling, "mode": mode}
                message = caller(outgoing_messages(messages, config), phase="field_recovery", tools=model_tools,
                                 meta=meta)
                # the call returned: the candidates it carried were really put in front of the model
                if turn_index == 1 and presented:
                    run_log.event("candidates_presented", source="cluster_recovery", cluster=name, attempt=attempt,
                                  presented_candidate_keys=presented)
                if announced_keys:
                    run_log.event("cluster_candidates_announced", cluster=name, attempt=attempt, turn=turn_index - 1,
                                  presented_candidate_keys=announced_keys)
                    announced_keys = []
                turns += 1
                state["total_steps"] += 1
                messages.append(_assistant_echo(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    reply_text = message.get("content") or ""
                    break
                turn_start = len(session.tool_calls)
                session.execute(calls, messages, phase="field_recovery",
                                allowed=LOCAL_TOOLS if local else None, cluster=name, attempt=attempt)
                # New documents were harvested for ALL fields by the session hook; re-evaluate EVERY requested field
                # (admitted evidence -> current_evaluation) and prune every cluster before any further paid turn.
                reevaluate(name, attempt)
                events = trace_events(run_log)
                matrix_now = usable_matrix(events)
                after = snapshot(events=events, evaluation=list(current.values()),
                                 documents=list(session.ctx.documents_opened), open_fields=watched, matrix=matrix_now)
                found = novelty(before, after, adm, cache)
                turn_novelty.append(found)
                if not found:      # this turn's web routes gave nothing new: remembered (scheduling only)
                    dead_routes.extend(_routes_of(session.tool_calls[turn_start:]))
                still_open = [f for f in open_fields if current[f]["retry_eligible"]]
                run_log.event("cluster_turn_novelty", cluster=name, attempt=attempt, turn=turn_index, novelty=found,
                              fields_open=still_open)
                resolved_now = [f for f in open_fields if f not in still_open]
                if not still_open and all(early_resolution_check(by_name[f], events, market)[0]
                                          for f in resolved_now if current[f]["state"] == "ok"):
                    early = True
                    state["early_count"] += 1
                    state["budget_skipped"] += max(0, base - turn_index)
                    run_log.event("field_recovery_early_resolved", field=label, cluster=name, attempt=attempt,
                                  after_turn=turn_index, state="ok", turn_budget=base,
                                  turn_budget_skipped=max(0, base - turn_index), fields=resolved_now)
                    break
                if turn_index >= base and not found:
                    stop = "no_novelty"
                    state["no_novelty_stops"] += 1
                    run_log.event("cluster_recovery_no_novelty_stop", cluster=name, attempt=attempt,
                                  after_turn=turn_index, fields_open=still_open)
                    break
                announced = {f: [c for c in matrix_now["fields"].get(f) or []
                                 if candidate_key(c) not in before["candidates"]][:4] for f in still_open}
                announced = {f: v for f, v in announced.items() if v}
                fresh = {f: [compact_candidate(c) for c in v] for f, v in announced.items()}
                note = [f"Fields still open: {', '.join(still_open)}."]
                if resolved_now:
                    note.append(f"Resolved now (do not research again): {', '.join(resolved_now)}.")
                if fresh:
                    note.append("New candidates harvested from documents of this turn: "
                                + json.dumps(fresh, ensure_ascii=False, default=str)[:3000])
                if budget is not None:
                    note.append(f"Billable searches left in this attempt: {budget.remaining}.")
                if turn_index >= base:
                    note.append("This turn produced novelty, so one more turn is allowed; reply with the JSON when "
                                "done.")
                messages[-1]["content"] += "\n[operational note] " + " ".join(note)
                # shown to the model with the next request: logged (never again a reason for a local-only pass) only
                # once that call returns
                announced_keys = [candidate_key(c) for v in announced.values() for c in v]
        except GLMError as exc:
            # ends THIS attempt only: the next cluster still runs. Candidates this failed call carried were never
            # logged as presented, so they stay fresh. Only RECOVERY_API_FAILURE_STOP consecutive failed attempts
            # (a provider outage) stop all recovery.
            error = _error_text(exc)
            state["failed_attempts"] += 1
            state["consecutive_api_failures"] += 1
            run_log.event("field_recovery_failed", field=label, cluster=name, attempt=attempt, error=error,
                          api_error=exc.as_dict(), consecutive_api_failures=state["consecutive_api_failures"])
            if state["consecutive_api_failures"] >= RECOVERY_API_FAILURE_STOP:
                state["stopped"] = "api_failure"
                run_log.event("field_recovery_api_failure_stop", cluster=name, attempt=attempt,
                              consecutive_api_failures=state["consecutive_api_failures"])
        else:
            state["consecutive_api_failures"] = 0
        finally:
            session.search_budget = None
            state["negative_route_blocks"] += session.route_blocks
            session.route_blocks = 0
            session.route_guard = None
        reply = _cluster_reply(reply_text, name)
        for entry in (reply or {}).get("fields") or []:
            field_name = normalize_field_name((entry or {}).get("field")) if isinstance(entry, dict) else None
            if field_name in open_fields and entry.get("status"):
                cited = entry.get("evidence_ids")
                run_log.event("field_status", field=field_name, status=str(entry["status"]).lower(),
                              note=entry.get("notes"), source=f"cluster_recovery_{name}_attempt_{attempt}",
                              evidence_ids=[str(i) for i in cited] if isinstance(cited, list) else [])
        reevaluate(name, attempt)
        searched = session.ctx.counters["search_cache_misses"] - searches_before
        state["searches"] += searched
        if budget is not None:
            state["search_refusals"] += budget.refused
        if not any(turn_novelty) and mode == "web" and error is None:    # an API failure says nothing about yield
            low_yield.update(f for f in open_fields if current[f]["retry_eligible"])
        record = {"field": label, "cluster": name, "fields": open_fields, "attempt": attempt, "round": round_no,
                  "mode": mode, "states_before": states_before,
                  "states_after": {f: current[f]["state"] for f in open_fields},
                  "state_before": states_before[open_fields[0]] if len(open_fields) == 1 else
                  f"{sum(1 for f in open_fields if states_before[f] in RETRY_STATES)} open",
                  "state_after": current[open_fields[0]]["state"] if len(open_fields) == 1 else
                  f"{sum(1 for f in open_fields if current[f]['retry_eligible'])} open",
                  "turns": turns, "turn_budget": base, "turn_ceiling": ceiling, "budget_extensions": extensions,
                  "novelty": turn_novelty, "stop": stop or ("early_resolved" if early else None),
                  "early_resolved": early, "search_provider_calls": searched,
                  "search_budget": budget.limit if budget else 0, "search_refused": budget.refused if budget else 0,
                  "fields_resolved": [f for f in open_fields if not current[f]["retry_eligible"]],
                  "reply": reply, "reply_text": None if reply else reply_text, "error": error,
                  "failed": error is not None, "packet_chars": packet_chars,
                  "deterministic_candidates": sum(len(v) for v in shown.values()),
                  "ranked_documents": len(ranked), "search_hints": len(hints)}
        executed = [c for c in session.tool_calls[calls_before:] if not c.get("blocked") and not c.get("reused")]
        record["_routes"] = dead_routes
        record["documents_fetched"] = sum(1 for c in executed if c["name"] in trace.FETCH_TOOLS)
        record["tokens"] = caller.usage["field_recovery"].get("total_tokens", 0) - tokens_before
        record["byproduct_resolutions"] = sorted(f for f, v in resolved_indirectly.items()
                                                 if v.get("resolved_during_field") == label
                                                 and v.get("attempt") == attempt)
        attempts_log.append(record)
        previous[name].append({k: record[k] for k in ("attempt", "mode", "states_after", "turns", "stop",
                                                      "search_provider_calls")})
        run_log.event("field_recovery_finished", **record)

    def local_next(plan: dict) -> bool:
        """THE local-first predicate: this cluster has not had its local pass and its open fields (any state,
        conflicting included) have candidates no model has seen. Used for ordering AND for the attempt's mode."""
        if plan["cluster"] in local_done:
            return False
        events = trace_events(run_log)
        fresh = fresh_candidates(usable_matrix(events), events,
                                 [f for f in plan["fields"] if current[f]["retry_eligible"]])
        return bool(fresh)

    # Breadth-first rounds: each round gives every cluster at most one attempt. A cluster's one local-only pass is
    # free of its web attempt budget (allowed_attempts counts web attempts only).
    web_done = {c["cluster"]: 0 for c in clusters}
    skipped: set[str] = set()
    round_no = 0
    while not state["stopped"]:
        round_no += 1
        progressed = False
        # local passes first: no billable search is spent while unreviewed local material remains
        plans = [(plan, local_next(plan)) for plan in clusters]
        rate = {c: v["resolutions_per_turn"] for c, v in history.items()}
        for plan, _ in sorted(plans, key=lambda pl: (0 if pl[1] else 1, -rate.get(pl[0]["cluster"], 0.0))):
            name = plan["cluster"]
            open_fields = [f for f in plan["fields"] if current[f]["retry_eligible"]]
            if not open_fields or allowed_attempts[name] == 0:
                continue
            local = local_next(plan)            # re-checked: an earlier attempt this round may have changed it
            if not local and web_done[name] >= allowed_attempts[name]:
                continue
            if not local and previous[name] and previous[name][-1]["mode"] == "web" \
                    and all(f in low_yield for f in open_fields):
                if name not in skipped:
                    skipped.add(name)
                    run_log.event("cluster_recovery_skipped", cluster=name, round=round_no, reason="low_yield",
                                  fields=open_fields)
                continue
            if cap_reached():
                state["stopped"] = "max_total_steps"
                break
            if local:
                local_done.add(name)
            else:
                web_done[name] += 1
            run_attempt(plan, len(previous[name]) + 1, round_no, local)
            progressed = True
            if state["stopped"]:
                break
        if not progressed:
            break
    final = [current[e["field"]] for e in primary]
    run_log.event("field_evaluation", stage="after_recovery", fields=final, summary=_state_counts(final))
    if memory is not None:
        try:
            _record_recovery_memory(memory, attempts_log, current, route_keys, target, run_log, by_name, route_ids,
                                    specs)
        except Exception as exc:  # memory problems never cost the run
            run_log.event("research_memory_write_failed", error=_error_text(exc))
    for a in attempts_log:
        a.pop("_routes", None)
    total = state["total_steps"]
    cap = config.field_recovery_max_total_steps
    not_attempted = [f for f in queued if f not in attempted_fields and current[f]["retry_eligible"]]
    cut_short = state["cut_short"]
    if cut_short and not any(current[f]["retry_eligible"] for f in by_cluster.get(cut_short[8:], ())):
        cut_short = None
    if state["stopped"] == "max_total_steps":
        run_log.event("field_recovery_budget_exhausted", turn_budget=cap, turns_used=total,
                      fields_not_attempted=not_attempted, field_cut_short=cut_short)
    retried = sorted(attempted_fields)
    resolved_queue = [f for f in queued if not current[f]["retry_eligible"]]
    metrics = tail_metrics(primary, final, model_calls=total, search_calls=state["searches"],
                           billable_search_calls=session.ctx.counters["search_api_calls"] - billable_before)
    return {
        "enabled": config.field_recovery_enabled,
        "mode": "cluster",
        "order": "breadth_first_clusters",
        "planner_version": "tail-planner-v1",
        "field_recovery_turn_budget": cap or None,
        "field_recovery_turns_used": total,
        "field_recovery_turns_remaining": max(0, cap - total) if cap else None,
        "fields_not_attempted_due_to_budget": not_attempted if state["stopped"] == "max_total_steps" else [],
        "field_cut_short_by_budget": cut_short,
        "requested": len(specs),
        "primary_states": _state_counts(primary),
        "final_states": _state_counts(final),
        "queue": queued,
        "triage": triaged,
        "clusters": clusters,
        "fields_retried": retried,
        "fields_recovered": [f for f in retried if not current[f]["retry_eligible"]],
        "fields_still_failed": [f for f in retried if current[f]["retry_eligible"]],
        "fields_resolved_directly": [f for f in retried if not current[f]["retry_eligible"]
                                     and f not in resolved_indirectly],
        "fields_resolved_indirectly": resolved_indirectly,
        "fields_not_attempted": not_attempted,
        "attempts": attempts_log,
        "attempt_count": len(attempts_log),
        "attempt_order": [f"{a['field']}#{a['attempt']}" for a in attempts_log],
        "turns": total,
        "early_resolution_count": state["early_count"],
        "turn_budget_skipped_by_early_resolution": state["budget_skipped"],
        "turns_saved_by_early_resolution": state["early_count"],
        "recovery_fields_given_first_attempt": len({f for a in attempts_log if a["attempt"] == 1 for f in a["fields"]}),
        "recovery_fields_never_attempted": len([f for f in queued if f not in attempted_fields]),
        "recovery_second_attempts_started": sum(1 for a in attempts_log if a["attempt"] >= 2),
        "recovery_unique_fields_touched": len(retried),
        "recovery_unique_fields_resolved": len(resolved_queue),
        "recovery_resolution_per_turn": round(len(resolved_queue) / total, 3) if total else None,
        "fields_never_attempted_due_to_budget": not_attempted if state["stopped"] == "max_total_steps" else [],
        "cluster_attempts": len(attempts_log),
        "fields_resolved_by_cluster": {c: sorted(set(v)) for c, v in resolved_by_cluster.items()},
        "no_novelty_stops": state["no_novelty_stops"],
        "budget_extensions": state["budget_extensions"],
        "search_budget_refusals": state["search_refusals"],
        # equivalent routes refused at dispatch because an earlier run found them unproductive (not research skipped)
        "negative_route_cache_hits": state["negative_route_blocks"],
        "negative_route_fields_shown": len(negative_shown),
        "local_only_attempts": sum(1 for a in attempts_log if a["mode"] == "local_only"),
        **metrics,
        "stopped": state["stopped"],
        "failed_attempts": state["failed_attempts"],
        "api_failure_stop": state["stopped"] == "api_failure",
        "evaluation_primary": primary,
        "evaluation_final": final,
    }

def _call_document(call: dict) -> str | None:
    args = trace.parse_args(call.get("arguments"))
    return call.get("document_id") or args.get("document_id") or args.get("key")


def _state_counts(evaluation: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in evaluation:
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    return counts


def run_document_sweep(*, session: ToolSession, caller: ModelCaller, specs: list[dict], payload: dict,
                       config: AgentConfig, run_log: RunLog, cache, documents_dir: Path, phase_ref: dict,
                       vehicle: dict | None) -> dict:
    """LOCAL-FIRST pre-sweep (0 model calls), then the bounded, adaptive Model Document Sweep (src/document_sweep.py).

        every document already harvested for ALL fields (session hook + the harvest step before this)
        → candidates routed per field (presentation priority only, never truth)
        → deterministic batch inspection of every usable cached document for open fields WITHOUT candidates
          (src/document_inspection.py: label snippets with offsets; no evidence, no state)
        → current_evaluation() again; settled fields are removed
        → ONE sweep call when the packet fits DOCUMENT_SWEEP_MAX_PACKET_CHARS / _MAX_FIELDS / _MAX_CANDIDATES,
          otherwise deterministic chunks by recovery_cluster (fields re-evaluated before every chunk)

    Cached-document tools only: a search or fetch call is refused without being executed, so this stage makes 0
    searches and 0 fetches. Returns the sweep summary; a GLMError fails only its chunk (recorded in
    `failed_chunk_fields`; the next chunk still runs and recovery still runs), control-flow exceptions propagate."""
    from .candidate_harvest import candidate_matrix
    from .document_inspection import inspect_document
    from .document_sweep import (DOCUMENT_SWEEP_TOOLS, EXTERNAL_TOOLS, MAX_SWEEP_TURNS, ROUTING_AUTHORITY,
                                 inspection_has_content, packet_size, plan_chunks, promoted_or_missed, route_candidates,
                                 sweep_packet, sweep_summary, sweep_tool_specs)
    from .tail_planner import candidate_key, document_profile_for, usable_candidate_matrix, usable_document
    from .tail_planner import presented_keys as shown_keys

    market = config.target_market
    events = trace_events(run_log)
    before = current_evaluation(events, specs, market)
    max_turns = max(0, min(int(config.document_sweep_max_turns or 0), MAX_SWEEP_TURNS))
    # candidates of unusable documents (a 404 / 403 error page, an empty fetch) are never swept (both modes)
    matrix = usable_candidate_matrix(candidate_matrix(events, specs, vehicle), cache)
    unusable_candidates = matrix["candidates_from_unusable_documents"]
    doc_metas = _doc_metas(events, cache, documents_dir)
    eligible = [e["field"] for e in before if e["retry_eligible"]]
    skipped = None
    if not config.layered_harvest_enabled:
        skipped = "layered_harvest_disabled"
    elif not config.field_recovery_enabled:
        skipped = "field_recovery_disabled"     # research + finalize only: no post-research model stage
    elif max_turns == 0:
        skipped = "document_sweep_max_turns=0"
    elif not eligible:
        skipped = "no_unresolved_fields"
    elif not doc_metas:
        skipped = "no_cached_documents"
    if skipped:
        summary = sweep_summary(before=before, after=before, sweep_evidence=[], promoted=[], missed=[], presented=0,
                                model_calls=0, turns=0, blocked=0, external_calls=0, reply=None, skipped=skipped)
        summary.update(_sweep_telemetry(chunks=[], packet_chars=0, est_tokens=0, fields=0, candidates=0,
                                        documents=len(doc_metas), latency_ms=0, timeouts=0, usage=None, pre=None))
        summary.update(sweep_mode=config.sweep_mode, candidates_from_unusable_documents=unusable_candidates)
        run_log.event("document_sweep_skipped", reason=skipped, fields_unresolved=len(eligible),
                      sweep_mode=config.sweep_mode)
        return summary
    phase_ref["name"] = "document_sweep"
    start_seq = run_log.seq
    usage_before = dict(caller.usage["document_sweep"])

    # ---- local-first pre-sweep: routing + deterministic batch inspection (no model, no network) ----
    t_pre = time.monotonic()
    by_name = {s["name"]: s for s in specs}
    adm = session.ctx.admission
    profiles = {}
    for meta in doc_metas:
        doc = str(meta.get("document_id"))
        profiles[doc] = {**document_profile_for(adm, cache, doc), "doc_type": meta.get("doc_type")}
    routed = {f: route_candidates(matrix["fields"].get(f) or [], profiles, market,
                                  str(by_name.get(f, {}).get("market_sensitivity")) == "high") for f in eligible}
    no_candidates = [f for f in eligible if not routed.get(f)]
    usable = [d for d in profiles if usable_document(cache, d) and profiles[d].get("variant_match") != "different"]
    usable = sorted(usable, key=lambda d: -ROUTING_AUTHORITY.get(str(profiles[d].get("source_authority")), 0.0))
    snippets: dict[str, list[dict]] = {}
    inspected = []
    searchable: set[str] = set()      # fields the dictionary can look for (no rule: never "not found")
    for doc in usable if no_candidates else []:
        found = inspect_document(cache, doc, specs, no_candidates, max_matches_per_field=1, context_chars=100,
                                 candidates_per_field=0)
        inspected.append({"document_id": doc, "fields_located": sorted(found.get("matches") or {})})
        searchable |= set(no_candidates) - set(found.get("fields_without_dictionary") or []) \
            - set(found.get("unknown_fields") or [])
        for name, items in (found.get("matches") or {}).items():
            for item in items:
                if len(snippets.setdefault(name, [])) < max(1, config.document_sweep_candidates_per_field):
                    snippets[name].append({"document_id": doc, "snippet": item["snippet"].strip(),
                                           "offset": item["offset"], "matched_alias": item["matched_alias"]})
    pre = {"documents_inspected": len(inspected), "fields_without_candidates": no_candidates,
           "fields_located": sorted(snippets), "model_calls": 0,
           "duration_ms": int((time.monotonic() - t_pre) * 1000)}
    run_log.event("document_inspection", stage="pre_sweep", **pre,
                  locations={f: [{"document_id": x["document_id"], "offset": x["offset"],
                                  "matched_alias": x["matched_alias"]} for x in v] for f, v in snippets.items()},
                  note="deterministic local inspection: locations, not evidence")
    events = trace_events(run_log)
    current = current_evaluation(events, specs, market)
    open_fields = [e["field"] for e in current if e["retry_eligible"] and by_name.get(e["field"], {}).get(
        "applicable", True)]
    if config.sweep_mode != "legacy":
        # the Candidate Adjudication sweep (src/adjudication.py): its own packet limits, no tools offered
        return run_adjudication_sweep(session=session, caller=caller, specs=specs, payload=payload, config=config,
                                      run_log=run_log, cache=cache, before=before, routed=routed, snippets=snippets,
                                      profiles=profiles, open_fields=open_fields, doc_metas=doc_metas, pre=pre,
                                      start_seq=start_seq, usage_before=usage_before,
                                      unusable_candidates=unusable_candidates, vehicle=vehicle)

    def build(fields: list[str], max_chars: int, chunk: dict | None = None) -> dict:
        return sweep_packet(payload=payload, specs=specs, evaluation=current, matrix=matrix, events=events,
                            doc_metas=doc_metas, target_market=market, max_turns=max_turns,
                            per_field=max(1, config.document_sweep_candidates_per_field), max_chars=max_chars,
                            fields=fields, routed=routed, snippets=snippets, profiles=profiles, chunk=chunk,
                            not_found_locally=[f for f in no_candidates if f in searchable and f not in snippets])

    limits = {"chars": config.document_sweep_packet_max_chars, "fields": config.document_sweep_max_fields,
              "candidates": config.document_sweep_max_candidates}
    chunks = plan_chunks(open_fields, specs, lambda fields: build(fields, 10 ** 9), limits)
    run_log.event("document_sweep_plan", fields=open_fields, chunks=chunks, limits=limits,
                  removed_settled=[f for f in eligible if f not in open_fields])

    specs_for_tools = sweep_tool_specs(tool_specs())
    calls_before = len(session.tool_calls)
    blocked_before = session.blocked
    turns, presented = 0, 0
    chunk_errors: list[str] = []
    failed_chunk_fields: list[str] = []
    failed_chunk_offered: list[str] = []      # candidate keys offered in chunks that failed
    replies, follow_ups, chunk_log = [], [], []
    t_sweep = time.monotonic()
    for index, plan in enumerate(chunks, start=1):
        if index > 1:      # evidence stored by an earlier chunk may have settled fields of this one
            events = trace_events(run_log)
            current = current_evaluation(events, specs, market)
            still = {e["field"] for e in current if e["retry_eligible"]}
            fields = [f for f in plan["fields"] if f in still]
        else:
            fields = list(plan["fields"])
        info = {"index": index, "of": len(chunks), "clusters": plan["clusters"]}
        if not fields:
            chunk_log.append({**info, "fields": [], "skipped": "fields_settled_by_earlier_chunk", "model_calls": 0})
            run_log.event("document_sweep_chunk_skipped", **info, reason="fields_settled_by_earlier_chunk")
            continue
        packet = build(fields, config.document_sweep_packet_max_chars, info if len(chunks) > 1 else None)
        size = packet_size(packet)
        shown = sum(len(v) for v in packet["deterministic_candidates"].values())
        presented += shown
        presented_keys = [candidate_key(c) for name, items in packet["deterministic_candidates"].items()
                          for c in (routed.get(name) or [])[:len(items)]]
        run_log.event("document_sweep_started", turn_budget=max_turns, fields_to_review=packet["fields_to_review"],
                      candidates_presented=shown, fields_without_candidates=packet["fields_without_candidates"],
                      documents=len(doc_metas), packet_chars=size["chars"], allowed_tools=list(DOCUMENT_SWEEP_TOOLS),
                      offered_candidate_keys=presented_keys, chunk=info,
                      local_snippet_fields=sorted(packet.get("local_snippets") or {}),
                      # diagnostic telemetry (observational; the packet sent is unchanged)
                      packet_document_ids=[d.get("document_id") for d in packet.get("cached_documents") or []],
                      candidates_per_field={n: len(v) for n, v in packet["deterministic_candidates"].items()},
                      estimated_input_tokens=(len(DOCUMENT_SWEEP_SYSTEM_PROMPT) + size["chars"]) // 4)
        usage_chunk = dict(caller.usage["document_sweep"])
        t_chunk = time.monotonic()
        messages = [{"role": "system", "content": DOCUMENT_SWEEP_SYSTEM_PROMPT},
                    {"role": "user", "content": "Document sweep task (JSON):\n"
                                                + json.dumps(packet, ensure_ascii=False, default=str)}]
        reply_text, chunk_turns, follow_up, error = None, 0, None, None
        try:
            for turn_index in range(1, max_turns + 1):
                message = caller(outgoing_messages(messages, config), phase="document_sweep", tools=specs_for_tools,
                                 meta={"turn": turn_index, "turn_budget": max_turns, "chunk": index})
                if turn_index == 1 and presented_keys:   # the call returned: the chunk's candidates were seen
                    run_log.event("candidates_presented", source="document_sweep", chunk=info,
                                  presented_candidate_keys=presented_keys)
                turns += 1
                chunk_turns += 1
                messages.append(_assistant_echo(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    reply_text = message.get("content") or ""
                    break
                session.execute(calls, messages, phase="document_sweep", allowed=DOCUMENT_SWEEP_TOOLS)
                if turn_index >= max_turns:
                    break
                # Adaptive budget: another (paid) turn only when this one read cached documents and got content the
                # model has not seen yet. Promoting/rejecting candidates needs no follow-up.
                inspected_now = [r for r in session.turn_results if inspection_has_content(r["name"], r["result"])]
                if not inspected_now:
                    break
                follow_up = {"after_turn": turn_index, "inspection_results": len(inspected_now),
                             "tools": sorted({r["name"] for r in inspected_now}), "chunk": index}
                follow_ups.append(follow_up)
                run_log.event("document_sweep_follow_up", **follow_up)
                messages[-1]["content"] += ("\n[operational note] Final document-sweep turn: read the inspection "
                                            "results above, then store_evidence / report_field_status. Results of "
                                            "calls made in this turn will not be shown to you.")
        except GLMError as exc:
            # A failed chunk never ends the sweep: its fields stay open (they flow to recovery as any open field)
            # and the next chunk still runs.
            error = _error_text(exc)
            chunk_errors.append(error)
            failed_chunk_fields.extend(f for f in fields if f not in failed_chunk_fields)
            failed_chunk_offered.extend(presented_keys)
            run_log.event("document_sweep_failed", error=error, api_error=exc.as_dict(), chunk=info)
            run_log.event("document_sweep_chunk_failed", **info, fields=fields, error=error,
                          timeout=bool(getattr(exc, "timeout", False)), attempts=getattr(exc, "attempts", None),
                          note="chunk failed; the sweep continues with the next chunk")
        used = {k: caller.usage["document_sweep"][k] - usage_chunk.get(k, 0) for k in caller.usage["document_sweep"]}
        if reply_text:
            replies.append(reply_text)
        chunk_log.append({**info, "fields": fields, "fields_count": len(fields), "candidates": shown,
                          "packet_chars": size["chars"], "model_calls": chunk_turns,
                          "latency_ms": int((time.monotonic() - t_chunk) * 1000),
                          "prompt_tokens": used.get("prompt_tokens", 0),
                          "completion_tokens": used.get("completion_tokens", 0), "error": error,
                          "failed": error is not None})
    latency_ms = int((time.monotonic() - t_sweep) * 1000)
    events = trace_events(run_log)
    after = current_evaluation(events, specs, market)
    sweep_evidence = [e["evidence"] for e in events if e.get("kind") == "evidence" and (e.get("seq") or 0) > start_seq
                      and isinstance(e.get("evidence"), dict)]
    promoted, missed = promoted_or_missed(sweep_evidence, events)
    for item in missed:
        run_log.event("candidate_missed_by_deterministic_harvest", field=item.get("field"), value=item.get("value"),
                      unit=item.get("unit"), document_id=item.get("document_id"), source_url=item.get("source_url"),
                      quote=item.get("quote"), evidence_id=item.get("evidence_id"))
    executed = session.tool_calls[calls_before:]
    external = sum(1 for c in executed if c["name"] in EXTERNAL_TOOLS and not c.get("blocked"))
    parsed = [r for r in (parse_model_output(t)[0] for t in replies) if r is not None]
    reply: Any = (parsed[0] if len(parsed) == 1 else parsed) if parsed else ("\n".join(replies) or None)
    summary = sweep_summary(before=before, after=after, sweep_evidence=sweep_evidence, promoted=promoted,
                            missed=missed, presented=presented, model_calls=turns, turns=turns,
                            blocked=session.blocked - blocked_before, external_calls=external, reply=reply)
    # every chunk error (one string when a single chunk failed, so existing consumers still see an error)
    summary["error"] = (chunk_errors[0] if len(chunk_errors) == 1 else chunk_errors) if chunk_errors else None
    summary["chunk_errors"] = chunk_errors
    summary["failed_chunk_fields"] = failed_chunk_fields
    # candidates offered only in failed chunks: never seen by a model, so they stay fresh for recovery's local pass
    seen = shown_keys(events)
    summary["failed_chunk_candidates_kept_fresh"] = len({k for k in failed_chunk_offered if k not in seen})
    summary["follow_up_turn"] = follow_ups[0] if len(follow_ups) == 1 else (follow_ups or None)
    timeouts = sum(1 for e in events if e.get("kind") == "api_error" and (e.get("seq") or 0) > start_seq
                   and e.get("phase") == "document_sweep" and e.get("timeout"))
    used = {k: caller.usage["document_sweep"][k] - usage_before.get(k, 0) for k in caller.usage["document_sweep"]}
    sent = [c for c in chunk_log if c.get("model_calls")]
    summary.update(_sweep_telemetry(
        chunks=chunk_log, packet_chars=sum(c["packet_chars"] for c in sent),
        est_tokens=sum((len(DOCUMENT_SWEEP_SYSTEM_PROMPT) + c["packet_chars"]) // 4 for c in sent),
        fields=sum(c["fields_count"] for c in sent), candidates=presented, documents=len(doc_metas),
        latency_ms=latency_ms, timeouts=timeouts, usage=used, pre=pre))
    summary.update(sweep_mode="legacy", candidates_from_unusable_documents=unusable_candidates)
    run_log.event("document_sweep_finished", **{k: v for k, v in summary.items() if k != "reply"},
                  reply=summary["reply"])
    return summary


def run_adjudication_sweep(*, session: ToolSession, caller: ModelCaller, specs: list[dict], payload: dict,
                           config: AgentConfig, run_log: RunLog, cache, before: list[dict], routed: dict[str, list[dict]],
                           snippets: dict[str, list[dict]], profiles: dict[str, dict], open_fields: list[str],
                           doc_metas: list[dict], pre: dict, start_seq: int, usage_before: dict,
                           unusable_candidates: int, vehicle: dict | None = None, phase: str = "document_sweep",
                           settings_phase: str | None = None, event_prefix: str = "document_sweep",
                           stage: str = "sweep", exclude_keys: Iterable[str] = (),
                           grounded_pool: list[str] | None = None) -> dict:
    """The Candidate Adjudication sweep (SWEEP_MODE=adjudication, src/adjudication.py). Code groups the open fields'
    usable candidates, dry-runs admission on each (nothing stored), classifies the fields U / A / M and sends small
    no-tool JSON packets per recovery cluster; accepted candidates and located statements become synthetic
    store_evidence calls through ToolSession.execute, so Evidence Admission stays the only gate. A packet that fails
    (GLMError, timeout, unparseable after one repair turn, or any local error) presents nothing and leaves its fields
    open for recovery; the next packet still runs. Control-flow exceptions (cancellation) propagate.

    Grounded candidates (GROUNDED_CANDIDATES, src/grounded.py) run between the dry run and the classes, on the open
    fields the dry run left WITHOUT an admissible candidate; their admissible candidates join those fields as class A.
    The same runner serves re-acquisition recovery (stage "reacquire": phase / settings_phase / event_prefix select its
    accounting and events, `exclude_keys` drops candidates already presented, `grounded_pool` restricts the grounded
    documents to the episode's new documents)."""
    from .adjudication import (ADJUDICATION_VERSION, MECHANICAL_REASONS, SNIPPET_CHARS, DocumentReader, a_packet,
                               assign_ids, candidate_context, candidate_request, decision_flags, dry_run, field_class,
                               group_by_value, hint_keys, m_packet, packet_chars, plan_packets, u_packet)
    from .document_sweep import _domain, promoted_or_missed, sweep_summary
    from .field_recovery import vehicle_identity
    from .tail_planner import candidate_key, cluster_of
    from .tail_planner import presented_keys as shown_keys
    from .tools.evidence import admission_context

    market = config.target_market
    by_name = {s["name"]: s for s in specs}
    t_sweep = time.monotonic()
    group = trace.phase_group(phase)
    excluded = set(exclude_keys or ())
    reader = DocumentReader(cache)
    limits = {"u_items": config.adjudication_max_u_items, "a_fields": config.adjudication_max_a_fields,
              "a_candidates": config.adjudication_max_a_candidates, "m_fields": config.adjudication_max_m_fields,
              "m_snippets": config.adjudication_max_m_snippets}
    max_tokens = {"U": config.adjudication_u_max_tokens, "A": config.adjudication_a_max_tokens,
                  "M": config.adjudication_m_max_tokens}
    dry = {"candidates_total": 0, "admissible_initial": 0, "admissible_after_widen": 0, "widen_attempts": 0,
           "not_admissible": 0, "not_admissible_by_reason": {}}
    admissible: dict[str, list[dict]] = {}
    not_admissible: list[dict] = []
    classes: dict[str, str] = {}
    packets: list[dict] = []
    # ---- B0 grouping + B1 admission dry run + B2 classes (code only; a local failure leaves fields to recovery) ----
    try:
        adm = admission_context(session.ctx)
        documents = list(session.ctx.documents_opened)
        for name in open_fields:
            for cand in group_by_value([c for c in routed.get(name) or [] if candidate_key(c) not in excluded]):
                dry["candidates_total"] += 1
                decision = dry_run(adm, cache, candidate_request(name, cand), documents)
                quote, widened = cand.get("quote"), False
                reasons = list(decision.get("reasons") or [])
                if not decision.get("accepted") and set(reasons) & set(MECHANICAL_REASONS):
                    wider = widen_quote_safe(reader, cand)
                    if wider:
                        dry["widen_attempts"] += 1
                        second = dry_run(adm, cache, candidate_request(name, cand, wider), documents)
                        if second.get("accepted"):
                            decision, quote, widened = second, wider, True
                        else:
                            reasons += [f"after_widen:{r}" for r in second.get("reasons") or []]
                if decision.get("accepted"):
                    dry["admissible_after_widen" if widened else "admissible_initial"] += 1
                    admissible.setdefault(name, []).append({"field": name, "candidate": cand, "key": candidate_key(cand),
                                                            "quote": quote, "widened": widened,
                                                            "flags": decision_flags(decision)})
                    continue
                dry["not_admissible"] += 1
                for reason in reasons:
                    if not reason.startswith("after_widen:"):
                        dry["not_admissible_by_reason"][reason] = dry["not_admissible_by_reason"].get(reason, 0) + 1
                not_admissible.append({"candidate_key": candidate_key(cand), "field": name,
                                       "document_id": cand.get("document_id"), "reasons": reasons})
    except Exception as exc:  # noqa: BLE001 - never costs the run: every open field simply goes to recovery
        run_log.event("adjudication_prepare_failed", error=_error_text(exc), stage=stage)
        prepare_failed = True
    else:
        prepare_failed = False
    # ---- grounded candidates for the open fields the dry run left with nothing admissible (Part E) ----
    grounded: dict | None = None
    grounded_fields: set[str] = set()
    if not prepare_failed and config.grounded_candidates:
        missing = [f for f in open_fields if not admissible.get(f) and by_name.get(f, {}).get("applicable", True)]
        try:
            docs = grounded_documents(adm=session.ctx.admission, cache=cache, events=trace_events(run_log),
                                      doc_metas=doc_metas, fields=missing, specs=specs, vehicle=vehicle,
                                      target_market=market, only=grounded_pool) if missing else []
        except Exception as exc:  # noqa: BLE001
            docs = []
            run_log.event("grounded_candidates_failed", stage=stage, error=_error_text(exc))
        if not missing or not docs:
            run_log.event("grounded_candidates_skipped", stage=stage,
                          reason="no_fields_without_admissible_candidate" if not missing else "no_documents",
                          fields=missing)
        else:
            grounded = run_grounded_candidates(session=session, caller=caller, specs=specs, payload=payload,
                                               config=config, run_log=run_log, cache=cache, fields=missing,
                                               documents=docs, phase=phase, settings_phase=settings_phase,
                                               stage=stage)
            for name, items in grounded["admissible"].items():
                if name in open_fields:
                    admissible.setdefault(name, []).extend(items)
                    grounded_fields.add(name)
    try:
        if prepare_failed:
            raise RuntimeError("adjudication preparation failed")
        for name in open_fields:
            # a field with a grounded candidate is always A: the model judges it a second time, in context
            cls = "A" if name in grounded_fields else field_class(
                admissible.get(name) or [], routed.get(name) or [], bool(snippets.get(name)),
                hint_keys(by_name.get(name)))
            if cls:
                classes[name] = cls
        sizes = {f: (len(snippets.get(f) or []) if c == "M" else len(admissible.get(f) or []))
                 for f, c in classes.items()}
        packets = plan_packets(fields=open_fields, classes=classes,
                               clusters={f: cluster_of(by_name.get(f) or {"name": f}) for f in open_fields},
                               sizes=sizes, limits=limits)
    except Exception as exc:  # noqa: BLE001 - never costs the run: every open field simply goes to recovery
        if not prepare_failed:
            run_log.event("adjudication_prepare_failed", error=_error_text(exc), stage=stage)
        packets = []
    if not_admissible:   # one event per run: recovery treats these keys like rejected ones (never fresh again)
        run_log.event("adjudication_not_admissible", rows=not_admissible, count=len(not_admissible))
    run_log.event("adjudication_plan", version=ADJUDICATION_VERSION, stage=stage, fields=open_fields, classes=classes,
                  grounded_fields=sorted(grounded_fields),
                  not_sent=[f for f in open_fields if f not in classes], limits=limits, dry_run=dry,
                  packets=[{k: p[k] for k in ("class", "cluster", "fields", "items")} for p in packets])

    identity = vehicle_identity(payload, market)
    model_calls, presented = 0, 0
    chunk_log: list[dict] = []
    chunk_errors: list[str] = []
    failed_fields: list[str] = []
    failed_offered: list[str] = []
    replies: list[Any] = []
    stats = {"packets": {"U": 0, "A": 0, "M": 0}, "failed_packets": {"U": 0, "A": 0, "M": 0}, "decisions": 0,
             "accepted": 0, "rejected_by_model": 0, "invalid_decisions": 0, "located": 0, "store_requests": 0,
             "admitted": 0, "duplicates": 0, "rejected_by_admission": 0, "quote_not_in_document": 0,
             "repair_turns": 0}
    for index, plan in enumerate(packets, start=1):
        cls = plan["class"]
        info = {"index": index, "of": len(packets), "class": cls, "clusters": [plan["cluster"]]}
        usage_chunk = dict(caller.usage[group])
        t_chunk = time.monotonic()
        items: list[dict] = []
        offered: list[str] = []
        error, chunk_calls, size = None, 0, 0
        record: dict[str, Any] = {}
        try:
            if cls == "M":
                rows = []
                for name in plan["fields"]:
                    for snip in (snippets.get(name) or [])[:limits["m_snippets"]]:
                        meta = cache.get(str(snip["document_id"])) or {}
                        rows.append({"field": name, "document_id": snip["document_id"],
                                     "source": _domain(meta.get("final_url") or meta.get("url")),
                                     "text": str(snip.get("snippet") or "")[:SNIPPET_CHARS]})
                ids = assign_ids(rows, prefix="s")
                for sid, row in ids.items():
                    row["id"] = sid
                packet = m_packet(identity=identity, target_market=market, specs=by_name, snippets=rows)
            else:
                cap = limits["u_items"] if cls == "U" else limits["a_candidates"]
                items = [dict(item) for name in plan["fields"] for item in (admissible.get(name) or [])[:cap]]
                ids = assign_ids(items)
                for cid, item in ids.items():
                    item["id"] = cid
                    if cls == "A":
                        item["context"] = candidate_context(reader, item["candidate"])
                offered = [item["key"] for item in items]
                packet = u_packet(identity=identity, target_market=market, specs=by_name, items=items) \
                    if cls == "U" else a_packet(identity=identity, target_market=market, specs=by_name, items=items,
                                                hints={f: hint_keys(by_name.get(f)) for f in plan["fields"]})
            size = packet_chars(packet)
        except Exception as exc:  # noqa: BLE001
            error = f"packet_build_failed: {_error_text(exc)}"
            packet = None
        if packet is not None:
            stats["packets"][cls] += 1
            run_log.event(f"{event_prefix}_started", sweep_mode="adjudication", packet_class=cls, stage=stage,
                          fields_to_review=plan["fields"], candidates_presented=len(items),
                          fields_without_candidates=plan["fields"] if cls == "M" else [],
                          documents=len(doc_metas), packet_chars=size, allowed_tools=[],
                          offered_candidate_keys=offered, chunk=info,
                          local_snippet_fields=plan["fields"] if cls == "M" else [],
                          packet_document_ids=sorted({str(i["candidate"].get("document_id")) for i in items}),
                          candidates_per_field={f: sum(1 for i in items if i["field"] == f) for f in plan["fields"]}
                          if cls != "M" else {},
                          estimated_input_tokens=(len(ADJUDICATION_SYSTEM_PROMPT) + size) // 4)
            messages = [{"role": "system", "content": ADJUDICATION_SYSTEM_PROMPT},
                        {"role": "user", "content": "Adjudication task (JSON):\n"
                                                    + json.dumps(packet, ensure_ascii=False, default=str)}]
            reply = None
            try:
                meta = {"turn": 1, "chunk": index, "adjudication_class": cls}
                message = caller(messages, phase=phase, settings_phase=settings_phase, meta=meta,
                                 max_tokens=max_tokens[cls])
                chunk_calls += 1
                reply, _ = parse_model_output(message.get("content"))
                if not isinstance(reply, dict):
                    # technical repair only: ask once for valid JSON (same phase, same packet)
                    stats["repair_turns"] += 1
                    repair = messages + [_assistant_echo(message), {"role": "user", "content": REPAIR_PROMPT}]
                    message = caller(repair, phase=phase, settings_phase=settings_phase,
                                     meta={**meta, "turn": 2, "repair": True}, max_tokens=max_tokens[cls])
                    chunk_calls += 1
                    reply, _ = parse_model_output(message.get("content"))
                    if not isinstance(reply, dict):
                        error = "unparseable_after_repair"
            except GLMError as exc:
                error = _error_text(exc)
                record = {"timeout": bool(getattr(exc, "timeout", False)), "attempts": getattr(exc, "attempts", None),
                          "api_error": exc.as_dict()}
            except Exception as exc:  # noqa: BLE001 - one packet's problem never stops the others
                error = _error_text(exc)
            model_calls += chunk_calls
            if error is None:
                try:
                    record = _apply_adjudication(session=session, run_log=run_log, cache=cache, reader=reader,
                                                 cls=cls, plan=plan, items=items, ids=ids, reply=reply, info=info,
                                                 offered=offered, stats=stats, phase=phase)
                    # A local failure while applying the packet must leave its candidates fresh for recovery.
                    # Announce them only after the entire packet was handled successfully.
                    if offered:
                        run_log.event("candidates_presented", source="adjudication" if stage == "sweep" else stage,
                                      chunk=info,
                                      presented_candidate_keys=offered)
                    presented += len(offered)
                    replies.append(reply)
                except Exception as exc:  # noqa: BLE001
                    error = f"apply_failed: {_error_text(exc)}"
        if error is not None:
            stats["failed_packets"][cls] += 1
            chunk_errors.append(error)
            failed_fields.extend(f for f in plan["fields"] if f not in failed_fields)
            failed_offered.extend(offered)
            run_log.event(f"{event_prefix}_failed", error=error, api_error=record.get("api_error"), chunk=info,
                          sweep_mode="adjudication", stage=stage)
            run_log.event(f"{event_prefix}_chunk_failed", **info, fields=plan["fields"], error=error,
                          timeout=bool(record.get("timeout")), attempts=record.get("attempts"),
                          note="packet failed; its fields stay open for recovery and the next packet still runs")
        used = {k: caller.usage[group][k] - usage_chunk.get(k, 0) for k in caller.usage[group]}
        chunk_log.append({**info, "fields": plan["fields"], "fields_count": len(plan["fields"]),
                          "candidates": len(items), "packet_chars": size, "model_calls": chunk_calls,
                          "latency_ms": int((time.monotonic() - t_chunk) * 1000),
                          "model_latency_ms": used.get("model_latency_ms", 0),
                          "prompt_tokens": used.get("prompt_tokens", 0),
                          "completion_tokens": used.get("completion_tokens", 0), "error": error,
                          "failed": error is not None,
                          **{k: record.get(k) for k in ("decisions", "accepted", "admitted", "rejected_by_admission",
                                                        "invalid") if k in record}})
    latency_ms = int((time.monotonic() - t_sweep) * 1000)
    events = trace_events(run_log)
    after = current_evaluation(events, specs, market)
    sweep_evidence = [e["evidence"] for e in events if e.get("kind") == "evidence" and (e.get("seq") or 0) > start_seq
                      and isinstance(e.get("evidence"), dict)]
    promoted, missed = promoted_or_missed(sweep_evidence, events)
    for item in missed:
        run_log.event("candidate_missed_by_deterministic_harvest", field=item.get("field"), value=item.get("value"),
                      unit=item.get("unit"), document_id=item.get("document_id"), source_url=item.get("source_url"),
                      quote=item.get("quote"), evidence_id=item.get("evidence_id"))
    summary = sweep_summary(before=before, after=after, sweep_evidence=sweep_evidence, promoted=promoted,
                            missed=missed, presented=presented, model_calls=model_calls, turns=model_calls,
                            blocked=0, external_calls=0, reply=replies or None)
    summary["error"] = (chunk_errors[0] if len(chunk_errors) == 1 else chunk_errors) if chunk_errors else None
    summary["chunk_errors"] = chunk_errors
    summary["failed_chunk_fields"] = failed_fields
    seen = shown_keys(events)
    summary["failed_chunk_candidates_kept_fresh"] = len({k for k in failed_offered if k not in seen})
    summary["follow_up_turn"] = None
    timeouts = sum(1 for e in events if e.get("kind") == "api_error" and (e.get("seq") or 0) > start_seq
                   and e.get("phase") == phase and e.get("timeout"))
    used = {k: caller.usage[group][k] - usage_before.get(k, 0) for k in caller.usage[group]}
    sent = [c for c in chunk_log if c.get("model_calls")]
    summary.update(_sweep_telemetry(
        chunks=chunk_log, packet_chars=sum(c["packet_chars"] for c in sent),
        est_tokens=sum((len(ADJUDICATION_SYSTEM_PROMPT) + c["packet_chars"]) // 4 for c in sent),
        fields=sum(c["fields_count"] for c in sent), candidates=presented, documents=len(doc_metas),
        latency_ms=latency_ms, timeouts=timeouts, usage=used, pre=pre))
    summary.update(sweep_mode="adjudication", candidates_from_unusable_documents=unusable_candidates,
                   adjudication={"version": ADJUDICATION_VERSION, "classes": classes,
                                 "class_counts": {c: sum(1 for v in classes.values() if v == c) for c in "UAM"},
                                 "fields_not_sent": [f for f in open_fields if f not in classes],
                                 "dry_run": dry, "grounded_fields": sorted(grounded_fields), **stats},
                   grounded_candidates=(grounded or {}).get("summary"))
    run_log.event(f"{event_prefix}_finished", **{k: v for k, v in summary.items() if k != "reply"},
                  reply=summary["reply"], stage=stage)
    return summary


def grounded_documents(*, adm, cache, events: list[dict], doc_metas: list[dict], fields: list[str], specs: list[dict],
                       vehicle: dict | None, target_market: str, only: list[str] | None = None,
                       limit: int = 3) -> list[dict]:
    """E2 document selection: usable documents not bound to another variant, ranked by tail_planner.rank_documents
    (source_yield_score) for the fields, keeping the top `limit` that are official or of the target market. `only`
    restricts the pool (e.g. the documents a re-acquisition episode just fetched)."""
    from .candidate_harvest import candidate_matrix
    from .field_recovery import is_target_market
    from .source_authority import OFFICIAL_CLASSES
    from .tail_planner import document_profile_for, rank_documents, usable_candidate_matrix

    metas = [m for m in doc_metas if only is None or str(m.get("document_id")) in set(map(str, only))]
    matrix = usable_candidate_matrix(candidate_matrix(events, specs, vehicle), cache)
    ranked = rank_documents(doc_metas=metas, matrix=matrix, fields=fields, events=events, adm=adm, cache=cache,
                            target_market=target_market, limit=max(1, len(metas)))
    out = []
    for doc in ranked:
        profile = document_profile_for(adm, cache, doc["document_id"])
        if profile.get("variant_match") == "different":
            continue
        if profile.get("source_authority") in OFFICIAL_CLASSES or is_target_market(profile.get("market"),
                                                                                    target_market):
            out.append({**doc, "variant_match": profile.get("variant_match")})
        if len(out) >= limit:
            break
    return out


def run_grounded_candidates(*, session: ToolSession, caller: ModelCaller, specs: list[dict], payload: dict,
                            config: AgentConfig, run_log: RunLog, cache, fields: list[str], documents: list[dict],
                            phase: str = "document_sweep", settings_phase: str | None = None,
                            stage: str = "sweep") -> dict:
    """E3-E6: one no-tool call per selected document (max 3) for `fields` (max 15 per call); the model points at
    statements, code cuts and verifies the quote and dry-runs admission. Returns {admissible: {field: [items]},
    summary}; items are adjudication items (candidate, key, quote, flags) of class A. Nothing is stored. A failed call
    (GLMError, timeout, unparseable after one repair, a local error) costs only its document."""
    from . import grounded as G
    from .adjudication import candidate_request, decision_flags, dry_run
    from .candidate_harvest import dictionary_for, normalize_text
    from .evidence_admission import quote_in_source
    from .field_recovery import vehicle_identity
    from .tail_planner import candidate_key
    from .tools.evidence import admission_context
    from .tools.extract import document_tables

    t0 = time.monotonic()
    by_name = {s["name"]: s for s in specs}
    wanted = [f for f in fields if f in by_name][:G.MAX_FIELDS]
    stats = {"stage": stage, "documents": len(documents[:G.MAX_DOCUMENTS]), "fields": wanted, "model_calls": 0,
             "failed_documents": 0, "items": 0, "invalid": 0, "quote_not_in_document": 0, "admissible": 0,
             "not_admissible": 0, "repair_turns": 0}
    admissible: dict[str, list[dict]] = {}
    usage_group = trace.phase_group(phase)
    usage_before = dict(caller.usage[usage_group])
    run_log.event("grounded_candidates_started", stage=stage, version=G.GROUNDED_VERSION, fields=wanted,
                  documents=[d.get("document_id") for d in documents[:G.MAX_DOCUMENTS]])
    try:
        adm = admission_context(session.ctx)
        alias = dictionary_for(specs).any_alias
    except Exception as exc:  # noqa: BLE001
        run_log.event("grounded_candidates_failed", stage=stage, error=_error_text(exc))
        return {"admissible": {}, "summary": {**stats, "error": _error_text(exc)}}
    identity = vehicle_identity(payload, config.target_market)
    run_documents = list(session.ctx.documents_opened)
    for doc in documents[:G.MAX_DOCUMENTS]:
        doc_id = str(doc.get("document_id"))
        error, reply, invalid, kept, rejected = None, None, [], [], []
        try:
            meta = cache.get(doc_id) or {}
            is_html = meta.get("doc_type") == "html" or meta.get("kind") == "rendered"
            html = cache.read_body(doc_id).decode("utf-8", errors="replace") if is_html else None
            try:
                tables = document_tables(cache, doc_id, meta, html)
            except Exception:  # noqa: BLE001
                tables = []
            blocks = G.document_blocks(cache.read_text(doc_id), is_pdf=meta.get("doc_type") == "pdf", tables=tables,
                                       alias_pattern=alias, normalize=normalize_text)
            url = meta.get("final_url") or meta.get("url")
            packet = G.grounded_packet(identity=identity, target_market=config.target_market,
                                       specs=[by_name[f] for f in wanted], blocks=blocks,
                                       source=re.sub(r"^https?://", "", str(url or ""))[:120] or None)
            messages = [{"role": "system", "content": GROUNDED_SYSTEM_PROMPT},
                        {"role": "user", "content": "Grounded candidate task (JSON):\n"
                                                    + json.dumps(packet, ensure_ascii=False, default=str)}]
            meta_tags = {"turn": 1, "grounded_document": doc_id, "stage": stage}
            message = caller(messages, phase=phase, settings_phase=settings_phase, meta=meta_tags,
                             max_tokens=G.MAX_TOKENS)
            stats["model_calls"] += 1
            reply, _ = parse_model_output(message.get("content"))
            if not isinstance(reply, dict):
                stats["repair_turns"] += 1
                repair = messages + [_assistant_echo(message), {"role": "user", "content": REPAIR_PROMPT}]
                message = caller(repair, phase=phase, settings_phase=settings_phase,
                                 meta={**meta_tags, "turn": 2, "repair": True}, max_tokens=G.MAX_TOKENS)
                stats["model_calls"] += 1
                reply, _ = parse_model_output(message.get("content"))
                if not isinstance(reply, dict):
                    error = "unparseable_after_repair"
            if error is None:
                items, invalid = G.parse_reply(reply, {b["id"]: b for b in blocks}, wanted)
                stats["items"] += len(items)
                material = adm.material(cache, doc_id, None, run_documents)
                for item in items:
                    if material is None or not quote_in_source(material, item["quote"]):
                        stats["quote_not_in_document"] += 1
                        invalid.append({"problem": "quote_not_in_document", "field": item["field"],
                                        "block": item["block"], "quote": item["quote"][:200]})
                        continue
                    cand = G.candidate(item, document_id=doc_id, source_url=url)
                    decision = dry_run(adm, cache, candidate_request(item["field"], cand), run_documents)
                    if decision.get("accepted"):
                        kept.append(cand)
                        admissible.setdefault(item["field"], []).append(
                            {"field": item["field"], "candidate": cand, "key": candidate_key(cand),
                             "quote": cand["quote"], "widened": False, "grounded": True,
                             "flags": decision_flags(decision)})
                    else:
                        rejected.append({"candidate_key": candidate_key(cand), "field": item["field"],
                                         "document_id": doc_id, "value": cand.get("value"), "quote": cand["quote"],
                                         "reasons": list(decision.get("reasons") or [])})
        except GLMError as exc:
            error = _error_text(exc)
        except Exception as exc:  # noqa: BLE001 - one document's problem never stops the others
            error = _error_text(exc)
        stats["invalid"] += len(invalid)
        stats["admissible"] += len(kept)
        stats["not_admissible"] += len(rejected)
        if invalid:
            run_log.event("grounded_candidates_invalid", stage=stage, document_id=doc_id, rows=invalid)
        if rejected:
            run_log.event("grounded_candidates_not_admissible", stage=stage, document_id=doc_id, rows=rejected,
                          note="dropped: never stored, never presented")
        if kept:   # harvest-equivalent: candidate_matrix / presented keys / diagnostics see them (never evidence)
            run_log.event("candidates_harvested", document_id=doc_id, phase=phase, source=G.METHOD, stage=stage,
                          url=(cache.get(doc_id) or {}).get("final_url") or (cache.get(doc_id) or {}).get("url"),
                          cache_hit=False, candidate_count=len(kept), fields=sorted({c["field"] for c in kept}),
                          candidates=kept)
        if error is not None:
            stats["failed_documents"] += 1
            run_log.event("grounded_candidates_document_failed", stage=stage, document_id=doc_id, error=error,
                          note="this document only; the others still run")
    used = {k: caller.usage[usage_group][k] - usage_before.get(k, 0) for k in caller.usage[usage_group]}
    stats.update(input_tokens=used.get("prompt_tokens", 0), output_tokens=used.get("completion_tokens", 0),
                 latency_ms=int((time.monotonic() - t0) * 1000),
                 fields_with_admissible=sorted(admissible))
    run_log.event("grounded_candidates_finished", **stats)
    return {"admissible": admissible, "summary": stats}


def widen_quote_safe(reader, cand: dict) -> str | None:
    from .adjudication import widen_quote

    try:
        return widen_quote(reader, cand)
    except Exception:  # noqa: BLE001 - no widening is a normal outcome
        return None


def _apply_adjudication(*, session: ToolSession, run_log: RunLog, cache, reader, cls: str, plan: dict,
                        items: list[dict], ids: dict, reply: dict, info: dict, offered: list[str],
                        stats: dict, phase: str = "document_sweep") -> dict:
    """B4 + B5 for one packet whose call returned a parsed reply: log the presented candidates, ignore (and log)
    invalid decisions, and turn accepted candidates / located statements into synthetic store_evidence calls that
    run through the normal tool path (admission decides; a rejection is only logged)."""
    from .adjudication import located_arguments, parse_a_reply, parse_m_reply, parse_u_reply, store_arguments, \
        synthetic_call

    calls: list[dict] = []
    if cls == "M":
        decisions, invalid = parse_m_reply(reply, ids, plan["fields"])
        stats["located"] += len(decisions)
        for item in decisions:
            text = reader.text(item["document_id"])
            if not item["quote"].strip() or item["quote"] not in text:
                stats["quote_not_in_document"] += 1
                invalid.append({"problem": "quote_not_in_document", "field": item["field"], "snippet": item["snippet"]})
                continue
            calls.append(synthetic_call(f"adj{info['index']}_{item['snippet']}", located_arguments(item)))
        accepted = len(calls)
    else:
        decisions, invalid = (parse_u_reply if cls == "U" else parse_a_reply)(reply, ids)
        accepted = 0
        for decision in decisions:
            if decision["accept"]:
                accepted += 1
                calls.append(synthetic_call(f"adj{info['index']}_{decision['id']}",
                                            store_arguments(ids[decision["id"]], decision)))
            else:
                stats["rejected_by_model"] += 1
    stats["decisions"] += len(decisions)
    stats["accepted"] += accepted
    stats["invalid_decisions"] += len(invalid)
    if invalid:
        run_log.event("adjudication_invalid_decision", chunk=info, rows=invalid,
                      note="ignored: unknown ids, ids of another packet or field, spans outside the snippet, "
                           "fields not in the packet")
    admitted = rejected = 0
    if calls:
        stats["store_requests"] += len(calls)
        session.execute(calls, [], phase=phase, allowed=("store_evidence",))
        for result in session.turn_results:
            outcome = result.get("result") if isinstance(result.get("result"), dict) else {}
            if outcome.get("stored"):
                admitted += 1
            elif outcome.get("reused"):
                stats["duplicates"] += 1
            elif outcome.get("rejected") or outcome.get("error"):
                rejected += 1
    stats["admitted"] += admitted
    stats["rejected_by_admission"] += rejected
    return {"decisions": len(decisions), "accepted": accepted, "admitted": admitted,
            "rejected_by_admission": rejected, "invalid": len(invalid)}


def _sweep_telemetry(*, chunks: list[dict], packet_chars: int, est_tokens: int, fields: int, candidates: int,
                     documents: int, latency_ms: int, timeouts: int, usage: dict | None, pre: dict | None) -> dict:
    """Document-sweep telemetry (observational). estimated_input_tokens = (system prompt + packet chars) / 4 per
    chunk's first turn, an estimate made before the call; input/output tokens are the provider's usage."""
    usage = usage or {}
    return {"document_sweep_calls": usage.get("model_calls", 0),
            "document_sweep_chunks": len([c for c in chunks if c.get("model_calls")]),
            "document_sweep_packet_chars": packet_chars,
            "document_sweep_estimated_input_tokens": est_tokens,
            "document_sweep_fields": fields,
            "document_sweep_candidates": candidates,
            "document_sweep_documents": documents,
            "document_sweep_latency_ms": latency_ms,
            "document_sweep_model_latency_ms": usage.get("model_latency_ms", 0),
            "document_sweep_timeouts": timeouts,
            "document_sweep_input_tokens": usage.get("prompt_tokens", 0),
            "document_sweep_output_tokens": usage.get("completion_tokens", 0),
            "document_sweep_chunk_details": chunks,
            "pre_sweep_inspection": pre}


def apply_fact_reuse(ctx: ToolContext, memory: ResearchMemory, specs: list[dict], run_log: RunLog,
                     target_market: str) -> dict:
    """Bring verified facts of related variants into this run (deterministic, no model call, no search). Each one is
    re-admitted against this target (tools/evidence.reuse_verified_fact); nothing else is trusted."""
    from .tools.evidence import reuse_verified_fact

    identity = getattr(ctx.admission, "identity", None)
    records, skipped = memory.reusable_facts(specs, identity)
    reused, rejected = [], {}
    for record in records:
        outcome = reuse_verified_fact(ctx, record)
        if outcome["reused"]:
            reused.append({"field": record["field"], "evidence_id": outcome["evidence_id"],
                           "record_id": record.get("record_id"), "scope_type": record.get("scope_type")})
        elif outcome["reasons"] != ["already_stored"]:
            for reason in outcome["reasons"] or ["unknown"]:
                rejected[reason] = rejected.get(reason, 0) + 1
    ok = sorted({e["field"] for e in current_evaluation(trace_events(run_log), specs, target_market)
                 if e["state"] == "ok"} & {r["field"] for r in reused})
    summary = {"candidates": len(records), "reused": len(reused), "items": reused, "fields_ok": ok,
               "skipped": skipped, "not_readmitted": rejected}
    run_log.event("fact_reuse", **summary)
    return summary


def run_vehicle(row: dict, payload: dict, *, client, cache: DocumentCache, run_log: RunLog,
                config: AgentConfig, tool_config: ToolConfig, vehicle_meta: dict | None = None,
                batch_id: str = "", ordinal: int | None = None, session=None,
                pricing: dict | None = None, pricing_finalizer: dict | None = None,
                persist: Callable[[dict], Any] | None = None, cancel_event=None) -> dict:
    """Research one vehicle, retry failed requested fields, finalize from a compact bundle, persist.

    `persist(result)` runs on EVERY exit path (default: write result.json). On
    KeyboardInterrupt (or another BaseException such as a Streamlit stop) the
    partial result is persisted first and the exception is re-raised; no further
    model call is made. `cancel_event` (a threading.Event of the batch) requests the same cooperative
    interruption from another thread: BatchCancelled is raised at the next safe point.

    `client` must not be shared with another vehicle running at the same time: its hooks are attached
    to this run (one GLMClient per vehicle worker; they share only the ConcurrencyController).
    """
    record_id = str(row.get("upstream_record_id"))
    persist = persist or run_log.write_result
    started_at, t0 = utc_now(), time.monotonic()
    evidence = EvidenceStore()
    ctx = ToolContext(cache=cache, evidence=evidence, config=tool_config, glm=client,
                      vehicle=vehicle_meta or {"manufacturer": row.get("tozar")},
                      log=lambda kind, **data: run_log.event(kind, **data))
    if session is not None:
        ctx.session = session
    research_model, finalizer_model = research_model_of(client), finalizer_model_of(client, config)
    if pricing_finalizer is None:
        pricing_finalizer = pricing if finalizer_model == research_model else default_pricing(finalizer_model)
    glm_config = effective_glm_config(client, config, tool_config)
    eff_config = effective_config(client, config, tool_config)
    eff_config["glm"] = glm_config
    specs = resolve_requested_fields(config.requested_fields or None, propulsion=propulsion_of(payload, vehicle_meta))
    requested_fields = {s["name"]: s.get("description") for s in specs if s.get("applicable", True)}
    notes_for_variant = variant_notes(record_id_of(payload) or record_id)
    api_errors: list[dict] = []
    identity = payload.get("identity") or {}
    vehicle_ctx = {**(vehicle_meta or {}), **{k: identity.get(k) for k in ("trim", "model_code", "year")
                                              if identity.get(k)}}
    vehicle_ctx.setdefault("manufacturer", identity.get("manufacturer") or row.get("tozar"))
    # The evidence admission gate of this run: target identity, requested specs, target market.
    ctx.admission = AdmissionContext.for_run(payload, {**(vehicle_meta or {}), **vehicle_ctx}, specs,
                                             config.target_market)
    harvester = RunHarvester(cache, specs, run_log, enabled=config.layered_harvest_enabled)
    tools = ToolSession(ctx, run_log, config, cancel_event=cancel_event,
                        on_documents=harvester.observe if config.layered_harvest_enabled else None, specs=specs)
    tracker = tools.tracker
    run_context: dict = {}
    caller = ModelCaller(client, run_log, config, run_context=run_context, cancel_event=cancel_event)
    phase = {"name": "research"}

    def api_hook(kind: str, **data: Any) -> None:
        if kind == "api_error":
            ctx.counters["api_errors"] += 1
            api_errors.append({k: v for k, v in data.items()})
        run_log.event(kind, phase=phase["name"], **data)

    def activity_hook(kind: str, **data: Any) -> None:
        """Request lifecycle (queue wait / in flight) of THIS vehicle's client, labelled with the
        run's current phase and the field / recovery attempt / turn of the model call it belongs to."""
        extra = {k: v for k, v in run_context.items() if v is not None and k not in data}
        run_log.event(kind, phase=phase["name"], record_id=record_id, **extra, **data)

    if getattr(client, "_tripy_active_run", None):
        raise RuntimeError("This GLM client is already attached to another running vehicle; create one "
                           "GLMClient per vehicle worker (they share only the ConcurrencyController).")
    client._tripy_active_run = record_id
    previous_hook = getattr(client, "hook", None)
    previous_activity = getattr(client, "activity_hook", None)
    client.hook = api_hook
    client.activity_hook = activity_hook
    if cancel_event is not None and hasattr(client, "cancel_event"):
        client.cancel_event = cancel_event
    contract = config.acquisition_mode == "contract"
    if contract:
        task = build_acquisition_message(payload, config.include_level3, config.max_steps, notes_for_variant, specs,
                                         config.primary_research_no_artifact_stop, config.target_market)
    else:
        task = build_user_message(payload, config.include_level3, config.max_steps, notes_for_variant, specs,
                                  config.primary_research_no_artifact_stop)
    messages: list[dict] = [
        {"role": "system", "content": research_system_prompt(config.acquisition_mode,
                                                             config.acquisition_document_card)},
        {"role": "user", "content": task},
    ]
    research_prompt_hash = hashlib.sha256(messages[0]["content"].encode("utf-8")).hexdigest()[:12]
    from .run_profiles import env_overrides as list_env_overrides
    try:       # informational: env values that differ from the code defaults (a named profile ignores them)
        env_overrides = list_env_overrides()
    except Exception:  # noqa: BLE001
        env_overrides = []
    run_log.write_input(payload)
    run_log.event("run_started", model=research_model, research_model=research_model,
                  finalizer_model=finalizer_model, record_id=record_id, prompt_version=PROMPT_VERSION,
                  bundle_version=BUNDLE_VERSION, max_steps=config.max_steps,
                  search_backend=tool_config.search_backend, glm_config=glm_config,
                  agent_config=asdict(config), tool_config=asdict(tool_config), pricing=pricing,
                  pricing_finalizer=pricing_finalizer, variant_notes=notes_for_variant,
                  requested_fields=requested_fields, requested_field_specs=[public_spec(s) for s in specs],
                  target_market=config.target_market, vehicle_label=vehicle_ctx,
                  tools_unavailable=unavailable_tools(), recovery_mode=config.recovery_mode,
                  acquisition_mode=config.acquisition_mode, sweep_mode=config.sweep_mode,
                  final_assembly=config.final_assembly,
                  acquisition_document_card=config.acquisition_document_card, run_profile=config.run_profile,
                  site_map=config.site_map, grounded_candidates=config.grounded_candidates,
                  research_prompt_hash=research_prompt_hash, env_overrides=env_overrides)
    try:
        memory = ResearchMemory.for_cache(cache) if config.research_memory_enabled else None
    except Exception as exc:  # memory problems never cost the run
        memory = None
        run_log.event("research_memory_unavailable", error=_error_text(exc))
    fact_reuse: dict | None = None
    if memory is not None:
        try:
            fact_reuse = apply_fact_reuse(ctx, memory, specs, run_log, config.target_market)
        except Exception as exc:  # memory problems never cost the run
            fact_reuse = {"error": _error_text(exc), "reused": 0}
            run_log.event("fact_reuse_failed", error=fact_reuse["error"])
        if fact_reuse.get("fields_ok") and contract:
            messages[1]["content"] += (
                "\n\nAlready settled by verified facts reused from related variants (no source is needed for them): "
                + ", ".join(fact_reuse["fields_ok"]) + ".")
        elif fact_reuse.get("fields_ok"):
            messages[1]["content"] += (
                "\n\nAlready supported by verified evidence reused from related variants (re-checked against this "
                "exact variant; their evidence ids are in the store): " + ", ".join(fact_reuse["fields_ok"])
                + ". Do not research these fields again.")

    # Part D: real URLs of the official sites (contract acquisition only), before research turn 1. Bounded, never
    # blocks the run: a failure logs site_map_failed and the task goes out without the section.
    site_map_state: dict = {"site": None, "offered": []}
    if contract and config.site_map:
        from .site_map import acquisition_section, run_site_map

        site_map_state = run_site_map(ctx, run_log, payload=payload, vehicle=vehicle_ctx,
                                      target_market=config.target_market)
        section = acquisition_section(site_map_state.get("offered") or [])
        if section:
            messages[1]["content"] += "\n\n" + section

    status: str | None = None
    stop_reason: str | None = None
    final_text: str | None = None
    output, parse_note = None, None
    output_from_code = False
    error, api_error = None, None
    finalization: dict | None = None
    recovery: dict | None = None
    bundle: dict | None = None
    harvest_summary: dict | None = None
    primary_research: dict | None = None
    sweep: dict | None = None
    steps_done = 0
    research_seconds: float | None = None
    interrupted: BaseException | None = None
    documents_dir = run_log.dir / "documents"

    def export_documents() -> None:
        for document_id in ctx.documents_opened:
            try:
                cache.export(document_id, documents_dir)
            except OSError:
                pass

    def assemble(final_status: str | None, *, events: list[dict], research_bundle: dict | None) -> dict:
        """The result record (final, partial or the pre-finalization checkpoint) from the current state."""
        counters = dict(ctx.counters)
        stats = trace.api_stats(events, glm_config.get("chat_path") or "chat/completions")
        usage_research, usage_finalizer = caller.usage["research"], caller.usage["finalization"]
        usage_recovery, usage_sweep = caller.usage["field_recovery"], caller.usage["document_sweep"]
        usage = trace.sum_usage(usage_research, usage_sweep, usage_recovery, usage_finalizer)
        search_calls = counters.get("search_api_calls", 0)
        # a sweep / recovery phase on its own model (src/phase_settings.py) is priced with that model's prices
        phase_models = {g: m for g in ("document_sweep", "field_recovery")
                        if (m := for_phase(config, g)["model"]) and m != research_model}
        cost, cost_details = phase_run_cost(usage_research=usage_research, usage_sweep=usage_sweep,
                                            usage_recovery=usage_recovery, usage_finalizer=usage_finalizer,
                                            search_api_calls=search_calls, pricing=pricing,
                                            pricing_finalizer=pricing_finalizer,
                                            unknown_usage_attempts=stats["unknown_usage_attempts"],
                                            phase_models=phase_models)
        return {
            "record_id": record_id,
            "ordinal": ordinal,
            "batch_id": batch_id,
            "model": research_model,
            "research_model": research_model,
            "finalizer_model": finalizer_model,
            "glm_config": glm_config,
            "effective_config": eff_config,
            "prompt_version": PROMPT_VERSION,
            "bundle_version": BUNDLE_VERSION,
            "requested_fields": requested_fields,
            "target_market": config.target_market,
            "acquisition_mode": config.acquisition_mode,
            "sweep_mode": config.sweep_mode,
            "acquisition_document_card": config.acquisition_document_card,
            "run_profile": config.run_profile,
            "final_assembly": config.final_assembly,
            "recovery_mode": config.recovery_mode,
            "site_map": config.site_map,
            "grounded_candidates": config.grounded_candidates,
            # where the output VALUES came from: code (deterministic assembly) or a model (finalizer / research reply)
            "output_source": None if output is None else ("code" if output_from_code else "model"),
            "research_prompt_hash": research_prompt_hash,
            "env_overrides": env_overrides,
            "status": final_status,
            "stop_reason": stop_reason,
            "research_steps": steps_done,
            "error": error,
            "api_error": api_error,
            "api_errors": api_errors,
            "finalizer_error": finalization.get("error") if finalization else None,
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_s": round(time.monotonic() - t0, 2),
            "research_duration_s": research_seconds,
            "output": output,
            "parse_note": parse_note,
            "raw_final_text": final_text,
            "last_model_content": caller.last_content,
            "evidence": evidence.items,
            "evidence_admission": admission_summary(events),
            "consistency_checks": run_checks(evidence.items, payload, specs),
            "documents": list(ctx.documents_opened),
            "tool_calls": tools.tool_calls,
            "counters": counters,
            "research_tracking": tracker.snapshot(),
            "field_recovery": recovery,
            "primary_research": primary_research,
            "candidate_summary": layered_summary(harvest_summary, sweep, recovery, harvester.stats),
            "document_sweep": sweep,
            "usage": usage,
            "usage_research": usage_research,
            "usage_document_sweep": usage_sweep,
            "usage_field_recovery": usage_recovery,
            "usage_finalizer": usage_finalizer,
            "api_stats": stats,
            "finalization": finalization,
            "search_api_calls": search_calls,
            "pricing": pricing,
            "pricing_finalizer": pricing_finalizer,
            "cost": cost,
            "cost_details": cost_details,
            "cost_note": UNKNOWN_USAGE_NOTE if stats["unknown_usage_attempts"] else None,
            "research_bundle": research_bundle,
            "documents_dir": str(documents_dir) if ctx.documents_opened else None,
            "result_source": "result.json",
            "recovered": False,
            "partial": final_status in PARTIAL_STATUSES,
            "interrupted": interrupted is not None,
            "interrupted_phase": phase["name"] if interrupted is not None else None,
            "interruption_type": type(interrupted).__name__ if interrupted is not None else None,
            "interruption_message": interruption_message(phase["name"]) if interrupted is not None else None,
        }

    try:
        # ---------------- research phase (source acquisition) ----------------
        # Every turn is measured by what it ACQUIRED (src/acquisition.py: a new usable document, official source,
        # candidate for an open field, admitted evidence, better binding), never by operations: re-reading cached
        # documents, repeated searches and failed fetches are no progress. Scheduling only.
        acq = AcquisitionTracker(run_log=run_log, ctx=ctx, specs=specs, vehicle=vehicle_ctx, cache=cache,
                                 config=config)
        # FAIL-SAFE: every scheduler stop (no-artifact streak, optional sufficiency, the legacy idle stop and the normal
        # turn ceiling) needs the MINIMUM ACQUISITION BASE (acquisition.minimum_base). An under-acquired run is told so
        # and keeps acquiring, up to PRIMARY_RESEARCH_HARD_MAX_TURNS; a normally acquired run stops exactly as before.
        hard_ceiling = max(config.max_steps, int(config.primary_research_hard_max_turns or 0))
        # Contract mode: the research model gets ONLY the acquisition tools (schemas sent AND execution allowed), and
        # an under-acquired extension beyond the normal ceiling ends once it stops acquiring (extension futility).
        research_tools = acquisition_tool_specs(tool_specs()) if contract else tool_specs()
        research_allowed = ACQUISITION_TOOLS if contract else None
        done_deferred_last = False            # contract: the previous research turn was a deferred "done"
        try:
            for step in range(1, hard_ceiling + 1):
                message = caller(outgoing_messages(messages, config), phase="research", tools=research_tools,
                                 activity={"turn": step})
                messages.append(_assistant_echo(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    final_text = message.get("content") or ""
                    steps_done = step
                    # contract: an early "done" cannot bypass the MINIMUM ACQUISITION BASE. Before or at the normal
                    # ceiling with the base unmet, the first "done" is deferred once; a second consecutive one, an
                    # extension turn's "done" (the futility rule's signal) or the hard ceiling stop as before.
                    if (contract and step < hard_ceiling and step <= config.max_steps and not done_deferred_last
                            and not acq.base()[0]):
                        acq.defer("model_finished", step)
                        done_deferred_last = True
                        messages.append({"role": "user", "content": early_done_note(
                            acq.base()[1], missing_categories_note(acq.missing_categories()))})
                        continue
                    stop_reason = "model_finished"
                    break
                done_deferred_last = False
                calls_before = len(tools.tool_calls)
                tools.execute(calls, messages, phase="research", allowed=research_allowed)
                steps_done = step
                found = acq.after_turn(step)          # never raises: a telemetry problem counts as progress
                base_met, base = acq.base()
                limit = config.primary_research_no_artifact_stop
                if step >= config.max_steps:          # the normal ceiling (or a turn beyond it)
                    if base_met or step >= hard_ceiling:
                        stop_reason = "max_steps"
                        break
                    if contract and step > config.max_steps:   # an extension turn: is extending still acquiring?
                        executed = [c for c in tools.tool_calls[calls_before:] if c["name"] in DISCOVERY_TOOLS
                                    and not c.get("blocked") and not c.get("reused")]
                        if acq.extension_turn(step, found, len(executed)):
                            stop_reason = "extension_exhausted"
                            break
                    acq.defer("max_turns", step)      # under-acquired: extend, up to the hard ceiling
                    wanted = "max_turns"
                else:
                    wanted = None
                    if limit and acq.streak >= limit:
                        wanted = "no_new_artifact"
                    elif acq.sufficient():
                        wanted = "acquisition_sufficient"
                    elif config.no_new_research_turns and tracker.idle_turns >= config.no_new_research_turns:
                        wanted = "no_new_research"
                    if wanted and base_met:
                        stop_reason = wanted
                        break
                    if wanted:
                        acq.defer(wanted, step)
                remaining = hard_ceiling - step if wanted else config.max_steps - step
                notes = []
                if wanted:
                    notes.append(f"Research would end here, but the acquired source set is still thin "
                                 f"({base['useful_documents']} useful document(s); {base['scoped_coverage_pct']}% of the "
                                 "requested fields have a candidate from a source in their market scope). Acquire NEW "
                                 "sources now (official importer / manufacturer specification pages or PDFs, "
                                 "target-market price lists or brochures, other strong spec sources); re-reading cached "
                                 f"documents does not help. At most {remaining} more turn(s).")
                elif not found and remaining > 0:
                    notes.append(("This turn acquired nothing new (no new usable document, target-market document or "
                                  "candidate); failed fetches and repeated searches are not acquisition." if contract
                                  else "This turn acquired nothing new (no new usable document, official source, "
                                  "candidate or admitted evidence); re-reading cached documents is not acquisition.")
                                 + (f" Research ends after {limit - acq.streak} more turn(s) like this."
                                    if limit and base_met else ""))
                if not wanted and 0 < remaining <= 2:
                    notes.append(f"{remaining} research turn(s) left: fetch the most valuable source still missing "
                                 "(official spec page or PDF, target-market price list or brochure) or finish. Every "
                                 "fetched document is harvested for all fields and reviewed in a later step.")
                if contract:                          # a hint only: computed after every stop decision of this turn
                    missing = missing_categories_note(acq.missing_categories())
                    if missing:
                        notes.append(missing)
                if notes:
                    messages[-1]["content"] += "\n[operational note] " + " ".join(notes)
            else:
                stop_reason = "max_steps"
        except Exception as exc:  # the run log keeps whatever happened before the failure
            stop_reason = "api_failure" if isinstance(exc, GLMError) else "research_exception"
            status, error = "research_failed", _error_text(exc)
            api_error = exc.as_dict() if hasattr(exc, "as_dict") else None
            run_log.event("error", phase="research", message=error, api_error=api_error)
        research_seconds = round(time.monotonic() - t0, 2)
        run_log.event("research_stopped", reason=stop_reason, steps=steps_done, research_s=research_seconds,
                      tracking={k: v for k, v in tracker.snapshot().items() if k != "turns"})
        primary_research = acq.summary(stop_reason=stop_reason, turns=steps_done,
                                       model_calls=caller.usage["research"]["model_calls"],
                                       research_s=research_seconds)
        if contract and config.site_map:
            from .site_map import usage as site_map_usage

            try:      # D4: did research fetch the offered URLs, and did they become useful documents?
                primary_research["site_map"] = site_map_usage(
                    site_map_state.get("offered") or [], tools.tool_calls, cache=cache, adm=ctx.admission,
                    target_market=config.target_market)
            except Exception as exc:  # noqa: BLE001 - telemetry never costs the run
                primary_research["site_map"] = {"error": _error_text(exc)}
        run_log.event("primary_research_summary", **primary_research)

        # ---------------- deterministic harvest + model document sweep ----------------
        if status is None and config.layered_harvest_enabled:
            phase["name"] = "deterministic_harvest"
            harvester.observe(ctx.documents_opened, "deterministic_harvest")
            harvest_summary = harvest_report(trace_events(run_log), specs, vehicle_ctx, harvester, config)
            run_log.event("deterministic_harvest_summary", **harvest_summary)
            # parser-gap telemetry (src/parser_gaps.py): labels with no harvested value; observational, never raises
            log_parser_gaps(run_log, cache=cache, adm=ctx.admission, documents=ctx.documents_opened, specs=specs)
            try:
                sweep = run_document_sweep(session=tools, caller=caller, specs=specs, payload=payload, config=config,
                                           run_log=run_log, cache=cache, documents_dir=documents_dir,
                                           phase_ref=phase, vehicle=vehicle_ctx)
            except Exception as exc:  # a sweep problem never costs the primary research
                sweep = {"error": _error_text(exc), "model_calls": 0}
                run_log.event("document_sweep_failed", error=sweep["error"])

        # ---------------- failed-field detection + targeted field retries ----------------
        if status is None:
            phase["name"] = "field_detection"
            try:
                if config.recovery_mode == "legacy":
                    recovery = run_field_recovery(session=tools, caller=caller, specs=specs, payload=payload,
                                                  config=config, run_log=run_log, cache=cache,
                                                  documents_dir=documents_dir, operator_notes=notes_for_variant,
                                                  phase_ref=phase, vehicle=vehicle_ctx)
                else:
                    recovery = run_cluster_recovery(session=tools, caller=caller, specs=specs, payload=payload,
                                                    config=config, run_log=run_log, cache=cache,
                                                    documents_dir=documents_dir, operator_notes=notes_for_variant,
                                                    phase_ref=phase, vehicle=vehicle_ctx, memory=memory)
            except Exception as exc:  # recovery problems never cost the primary research
                recovery = {"error": _error_text(exc), "attempt_count": 0}
                run_log.event("field_recovery_failed", error=recovery["error"])

        # ---------------- finalization phase ----------------
        if status is None:
            retried = bool(recovery and recovery.get("attempt_count"))
            swept = bool(sweep and sweep.get("model_calls"))
            fin = None
            deterministic = config.final_assembly != "llm"
            # contract mode: the research reply is a {"done": ...} acquisition note, never the run output; the run is
            # always finalized from the compact bundle. Deterministic assembly: values only ever come from code.
            if stop_reason == "model_finished" and not retried and not swept and not contract and not deterministic:
                output, parse_note = parse_model_output(final_text)
                if output is not None:
                    status = "completed"
            if output is None:
                phase["name"] = "finalization"
                export_documents()
                events_now = trace_events(run_log)
                bundle = build_research_bundle(events_now, payload, cache=cache, documents_dir=documents_dir,
                                               include_level3=config.include_level3,
                                               max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason,
                                               target_market=config.target_market)
                # Durable checkpoint BEFORE the paid finalizer request: a hard process death from here on
                # leaves a run that --finalize-existing can finish without repeating any research.
                # FAIL CLOSED: if the checkpoint cannot be persisted, the finalizer is never called.
                finalization = {"status": "started", "model": finalizer_model, "checkpoint_at": utc_now()}
                checkpoint_error = None
                try:
                    persist(assemble("finalization_pending", events=events_now, research_bundle=bundle))
                except Exception as exc:
                    checkpoint_error = _error_text(exc)
                    run_log.event("result_write_failed", error=checkpoint_error, stage="checkpoint")
                if checkpoint_error is not None:
                    finalization = {"status": "not_started", "model": finalizer_model,
                                    "reason": "checkpoint_write_failed", "error": checkpoint_error}
                    status, error = "finalization_failed", f"checkpoint_write_failed: {checkpoint_error}"
                    parse_note = "finalization_not_started"
                    run_log.event("finalization_not_started", reason="checkpoint_write_failed",
                                  error=checkpoint_error, model=finalizer_model)
                    fin = None
                else:
                    run_log.event("finalization_checkpoint_written", status="finalization_pending",
                                  evidence_items=len(evidence.items), bundle_chars=bundle.get("bundle_chars"))
                    if deterministic:      # values from field states + admitted evidence; a model only narrates
                        fin = run_deterministic_finalization(caller, run_log=run_log, events=events_now,
                                                             payload=payload, specs=specs,
                                                             target_market=config.target_market,
                                                             model=finalizer_model, bundle=bundle, record_id=record_id)
                    else:
                        fin = run_finalization(caller, run_log=run_log, payload=payload, config=config, cache=cache,
                                               documents_dir=documents_dir, stop_reason=stop_reason,
                                               model=finalizer_model,
                                               request_path=run_log.dir / "finalizer_request.json", bundle=bundle)
            if output is None and fin is not None:
                finalization, bundle = fin["info"], fin["bundle"]
                if stop_reason != "model_finished" or contract:
                    final_text = fin["text"]
                if fin["error"]:
                    status, error, api_error = "finalization_failed", fin["error"], fin["api_error"]
                    parse_note = fin["parse_note"]
                else:
                    output, parse_note = fin["output"], f"finalizer:{fin['parse_note']}"
                    output_from_code = deterministic
                    if stop_reason == "model_finished":
                        status = "completed" if output is not None else "completed_unparsed"
                    else:
                        status = FINALIZED_STATUS[stop_reason]
    except BaseException as exc:  # KeyboardInterrupt, Streamlit stop/rerun, SystemExit, BatchCancelled
        # A script-control interruption, not a research/API/tool error: stop all model and tool calls (no
        # finalizer), persist everything already completed, rebuild current state from events, re-raise.
        interrupted = exc
        run_log.listener_muted = True
        status = "interrupted"
        stop_reason = stop_reason or "user_cancelled"
        error = None
        if research_seconds is None:
            research_seconds = round(time.monotonic() - t0, 2)
        run_log.event("interrupted", phase=phase["name"], step=steps_done, exception=type(exc).__name__)
    finally:
        client.hook = previous_hook
        client.activity_hook = previous_activity
        client._tripy_active_run = None

    duration = round(time.monotonic() - t0, 2)
    export_documents()
    events = trace_events(run_log)
    if recovery is None and status == "interrupted":
        recovery = trace.field_recovery_summary(events)   # interrupted mid-recovery: history from events
    if bundle is None:
        # Not sent anywhere: the partial research bundle is stored for the UI and for later recovery.
        bundle = build_research_bundle(events, payload, cache=cache, documents_dir=documents_dir,
                                       include_level3=config.include_level3,
                                       max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason,
                                       target_market=config.target_market)
    result = assemble(status, events=events, research_bundle=bundle)
    result["duration_s"] = duration
    result["fact_reuse"] = fact_reuse
    if memory is not None and interrupted is None:
        try:   # verified, exactly bound facts of this run become reusable for related variants
            written = memory.record_facts(evidence.items, specs, getattr(ctx.admission, "identity", None),
                                          {"batch_id": batch_id, "record_id": record_id, "run_started_at": started_at},
                                          current_evaluation(events, specs, config.target_market))
            result["verified_facts_recorded"] = len(written)
        except Exception as exc:
            run_log.event("fact_record_failed", error=_error_text(exc))
    try:   # labelled engineering feedback of this run (deterministic; this worker's own file)
        from .training_feedback import feedback_examples, summarize, write_run_feedback

        examples = feedback_examples(events, payload, specs, config.target_market)
        write_run_feedback(run_log.dir, examples)
        result["training_feedback"] = summarize(examples)
    except Exception as exc:
        run_log.event("training_feedback_failed", error=_error_text(exc))
    trace.apply_current_states(recovery, (bundle or {}).get("field_states"))
    # Persist first, then announce: a UI stop raised by the run_finished callback cannot lose the result.
    try:
        persist(result)
    except Exception as exc:  # disk problems must not hide the in-memory result from the caller
        run_log.event("result_write_failed", error=_error_text(exc))
    run_log.event("run_finished", status=status, stop_reason=stop_reason, duration_s=duration,
                  usage=result["usage"], search_api_calls=result["search_api_calls"], cost=result["cost"],
                  api_stats=result["api_stats"])
    if interrupted is not None:
        raise interrupted
    return result


def harvest_report(events: list[dict], specs: list[dict], vehicle: dict | None, harvester, config) -> dict:
    """Summary of the deterministic harvest for the run (logged once, before the document sweep)."""
    from .candidate_harvest import candidate_matrix

    from .tail_planner import usable_candidate_matrix

    matrix = candidate_matrix(events, specs, vehicle)
    evaluation = current_evaluation(events, specs, config.target_market)
    try:     # telemetry only: the totals below stay the unfiltered matrix's
        unusable = usable_candidate_matrix(matrix, getattr(harvester, "cache", None))["candidates_from_unusable_documents"]
    except Exception:  # noqa: BLE001
        unusable = None
    return {"documents_harvested": harvester.stats["documents_harvested"],
            "candidate_cache_hits": harvester.stats["candidate_cache_hits"],
            "candidate_cache_misses": harvester.stats["candidate_cache_misses"],
            "harvest_errors": harvester.stats["harvest_errors"],
            "candidate_count_total": matrix["candidate_count"],
            "candidate_fields_total": len(matrix["fields_with_candidates"]),
            "candidate_field_coverage_pct": matrix["candidate_field_coverage_pct"],
            "applicable_fields": matrix["applicable_fields"],
            "fields_with_candidates": matrix["fields_with_candidates"],
            "fields_without_candidates": matrix["fields_without_candidates"],
            "fields_unresolved_before_harvest": sum(1 for e in evaluation if e["retry_eligible"]),
            "candidates_from_unusable_documents": unusable,
            "note": "candidate coverage is not factual accuracy; candidates are not evidence"}


def layered_summary(harvest: dict | None, sweep: dict | None, recovery: dict | None, stats: dict) -> dict:
    """Layered-pipeline metrics for result.json (observational; never accuracy)."""
    harvest, sweep, recovery = harvest or {}, sweep or {}, recovery or {}
    return {
        "documents_harvested": harvest.get("documents_harvested", stats.get("documents_harvested", 0)),
        "candidate_count_total": harvest.get("candidate_count_total", stats.get("candidates_total", 0)),
        "candidate_fields_total": harvest.get("candidate_fields_total", 0),
        "candidate_field_coverage_pct": harvest.get("candidate_field_coverage_pct", 0.0),
        "candidate_cache_hits": stats.get("candidate_cache_hits", 0),
        "candidate_cache_misses": stats.get("candidate_cache_misses", 0),
        "fields_with_candidates": harvest.get("fields_with_candidates", []),
        "fields_without_candidates": harvest.get("fields_without_candidates", []),
        "document_sweep_model_calls": sweep.get("model_calls", 0),
        "document_sweep_skipped": sweep.get("skipped"),
        "document_sweep_fields_promoted_to_evidence": len(sweep.get("fields_promoted_to_evidence") or []),
        "document_sweep_evidence_promoted": sweep.get("evidence_stored", 0),
        "document_sweep_fields_resolved": sweep.get("unique_fields_resolved", 0),
        "document_sweep_deterministic_misses_found": sweep.get("deterministic_misses_found", 0),
        "candidate_precision_reviewed": sweep.get("candidate_precision_reviewed"),
        "fields_unresolved_before_harvest": harvest.get("fields_unresolved_before_harvest"),
        "fields_unresolved_after_harvest_review": sweep.get("fields_unresolved_after",
                                                            harvest.get("fields_unresolved_before_harvest")),
        "fields_entering_web_recovery": len(recovery.get("queue") or []),
    }

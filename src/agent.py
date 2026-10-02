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
    FIELD RETRIES       focused, compact-context web recovery for the fields still unresolved,
                        BREADTH-FIRST: every field gets attempt 1 before any field gets attempt 2
                        (src/field_recovery.py); successful fields are never re-researched
          ↓
    COMPACT BUNDLE      identity, targets, evidence, candidate facts, document metadata,
                        excerpts, concise actions, missing targets (src/bundle.py)
          ↓
    CHECKPOINT          result.json with status finalization_pending is written atomically and durably
                        BEFORE the paid finalizer request starts (a hard process death leaves a
                        finalizable run). If it cannot be written, the finalizer is NOT called.
          ↓
    FINALIZATION PHASE  ONE no-tools call with GLM_FINALIZER_MODEL (default GLM_MODEL)
          ↓
    structured JSON

The finalizer never receives the research conversation. Every started run leaves
a result.json, including research failures, finalization failures and
interrupts (Ctrl+C / Streamlit stop), with whatever was collected.

The loop is deliberately permissive: the model chooses tools and sources,
decides how to handle conflicts and shapes its answer. The code only keeps the
conversation within technical limits, notices repeated work and logs everything.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bundle import BUNDLE_VERSION, admission_summary, build_research_bundle
from .consistency_checks import run_checks
from .concurrency import BatchCancelled
from .glm_client import GLMError
from .context import ResearchTracker, call_signature, compact_stub, model_view, replay_result
from .storage.trace import FETCH_TOOLS
from .field_recovery import (current_evaluation, early_resolution_check, parse_retry_reply, retry_packet,
                             retry_queue)
from .fields import grouped, parse_field_list, propulsion_of, public_spec, resolve_requested_fields, semantic_notes
from .pricing import UNKNOWN_USAGE_NOTE, default_pricing, run_cost
from .schemas import LEVEL3_TOPICS, parse_model_output
from .storage import trace
from .storage.cache import DocumentCache
from .storage.atomic import atomic_write_json
from .storage.run_log import RunLog, read_events, utc_now
from .tools import ToolConfig, ToolContext, dispatch, tool_specs
from .tools.evidence import EvidenceStore
from .candidate_harvest import RunHarvester
from .evidence_admission import AdmissionContext
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
extract_html, extract_tables, find_in_document, get_structured_data, get_cached_document and
store_evidence. Use them however you judge best. No source type is forbidden and none is required;
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

Working efficiently (documents are external memory):
- fetch_url / fetch_pdf / render_page store the full document in a document store and return only a
  document_id, metadata and a short preview. The full text is NOT in this conversation.
- Before running another web search, exploit the documents you already fetched:
  1. find_in_document(document_id, query) for specific values;
  2. extract_tables(document_id) for spec tables (HTML and PDF);
  3. get_structured_data(document_id) for embedded JSON on HTML pages;
  4. extract_html(document_id, offset) / get_cached_document to page through text.
  Search again when an important target is still unresolved and your documents cannot answer it.
- Every document you fetch is automatically scanned by the runtime for deterministic candidates of ALL
  requested Level 2 fields (labels, units, spec tables), and a later review step checks those candidates
  against the cached documents. So focus your turns on: the exact vehicle identity and trim/variant,
  Israeli-market sources, authoritative specification sources (importer/manufacturer spec pages, price
  lists, brochures and spec PDFs), obtaining high-value documents, and sources for difficult fields.
  You may still store obvious evidence, but do not spend turns on manual document searches for every
  simple specification field.
- Do not spend turns re-confirming Level 2 values that are already well supported. Spend remaining turns
  on unresolved fields, conflicting fields, missing evidence records and exact Israeli-market provenance.
  Level 3 (reliability, resale, insurance, recalls) should not take the budget away from clean Level 2
  evidence.
- Re-fetching a URL you already fetched returns the same stored document. Older tool results in this
  conversation are shortened, but every document_id stays valid and can be queried again.
- If a requested field does not exist for this vehicle (e.g. a fuel tank on an EV), call
  report_field_status(field, "not_applicable"). If you could not resolve a field, you may report
  "unresolved"; such fields get a separate focused follow-up later, so do not loop on them now.
- When storing evidence, you may pass your variant_match claim (exact | different | unclear); the runtime
  computes the effective binding, and a rejected store_evidence returns its reasons.
- You have a soft budget of research turns. When it runs out, a separate step compiles the final
  answer from your stored evidence and the excerpts you looked at, so call store_evidence for every
  value you intend to use as soon as you find it.

When you are done, reply with ONLY one JSON object (no tool call) shaped like:
""" + OUTPUT_SHAPE

FINALIZER_SYSTEM_PROMPT = """You are the finalization step of a vehicle research benchmark.

A research agent with web tools has already researched ONE specific vehicle variant. You receive a
compact research bundle instead of its conversation: the fixed Level 1.5 identity, the requested
targets, the evidence items it stored (with source URLs and quotes), candidate facts grouped by field,
metadata of the documents it fetched, excerpts it read from them, conflicts it noted, a concise list of
its research actions, targets with no stored evidence, and its last notes.

You cannot browse or call tools. Organize the research into the final answer. Every evidence item carries
runtime-computed provenance: variant_match / binding_level (does the source describe the exact target variant;
"different" and "unbound" items are NOT about the target), market, source_authority and, for time-sensitive
values, valid_as_of. Report a time-sensitive value with its valid_as_of, never as timeless. You decide how to use
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
5. For fields_without_candidates (and fields whose candidates are all rejected), inspect the cached documents
   with find_in_document / extract_tables / get_structured_data / extract_html / get_cached_document and
   store any valid fact the parser missed.
6. If a field does not exist for this vehicle, report_field_status(field, "not_applicable").
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

REPAIR_PROMPT = ("Your last reply could not be parsed as JSON. Return the same content as ONE valid JSON object "
                 "and nothing else.")

PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + FINALIZER_SYSTEM_PROMPT + FIELD_RECOVERY_SYSTEM_PROMPT
                                 + DOCUMENT_SWEEP_SYSTEM_PROMPT + BUNDLE_VERSION).encode("utf-8")).hexdigest()[:12]

STOP_REASONS = ("model_finished", "max_steps", "no_new_research", "user_cancelled", "api_failure",
                "research_exception")
STATUSES = ("completed", "max_steps_finalized", "no_new_research_finalized", "completed_unparsed",
            "finalization_failed", "research_failed", "interrupted", "incomplete", "recovered_finalized",
            "finalization_pending")
FINALIZED_STATUS = {"max_steps": "max_steps_finalized", "no_new_research": "no_new_research_finalized"}
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
    max_steps: int = 12                       # soft research budget (model turns)
    max_tool_output_chars: int = 6000         # per tool result sent to the model this turn
    keep_recent_tool_results: int = 4         # newest tool results kept as sent; older ones are compacted
    compact_tool_output_chars: int = 700      # size of a compacted older tool result
    no_new_research_turns: int = 2            # finalize after N consecutive idle turns; 0 = off
    finalizer_bundle_max_chars: int = 60000   # cap on the compact research bundle
    temperature: float | None = None
    max_tokens: int | None = None
    include_level3: bool = False              # Level 3 open research is opt-in (INCLUDE_LEVEL3 / UI / --level3)
    thinking: str = ""                        # "" = provider default (not sent), "enabled", "disabled"
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
    document_sweep_candidates_per_field: int = 6
    document_sweep_packet_max_chars: int = 40000


AGENT_ENV = {
    "max_steps": "AGENT_MAX_STEPS",
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
    enabled = _env_bool(env("FIELD_RECOVERY_ENABLED"))
    if enabled is not None:
        values["field_recovery_enabled"] = enabled
    level3 = _env_bool(env("INCLUDE_LEVEL3"))
    if level3 is not None:
        values["include_level3"] = level3
    layered = _env_bool(env("LAYERED_HARVEST_ENABLED"))
    if layered is not None:
        values["layered_harvest_enabled"] = layered
    if (env("ENRICHMENT_FIELDS") or "").strip():
        values["requested_fields"] = parse_field_list(env("ENRICHMENT_FIELDS"))
    if (env("TARGET_MARKET") or "").strip():
        values["target_market"] = env("TARGET_MARKET").strip()
    return AgentConfig(**{**values, **overrides})


def tool_config_from_env(env: Callable[[str], str | None] = os.environ.get, **overrides) -> ToolConfig:
    return ToolConfig(**{**_env_ints(TOOL_ENV, env), **overrides})


def request_extra(config: AgentConfig) -> dict:
    """Extra fields merged into every chat request. The explicit thinking setting wins over extra_body."""
    extra = dict(config.extra_body or {})
    if config.thinking:
        extra["thinking"] = {"type": config.thinking}
    return extra


def research_model_of(client) -> str:
    return client.model


def finalizer_model_of(client) -> str:
    settings = getattr(client, "settings", None)
    explicit = getattr(settings, "finalizer_model", "") if settings is not None else ""
    return explicit or getattr(client, "finalizer_model", "") or client.model


def effective_glm_config(client, config: AgentConfig, tool_config: ToolConfig) -> dict:
    """The complete GLM configuration a run used (never includes the API key)."""
    settings = client.settings.public() if hasattr(client, "settings") else {"model": client.model}
    settings.setdefault("research_model", research_model_of(client))
    settings.setdefault("finalizer_model", finalizer_model_of(client))
    extra = request_extra(config)
    return {
        **settings,
        "thinking": extra.get("thinking", "provider_default"),
        "max_tokens": config.max_tokens if config.max_tokens else "provider_default",
        "temperature": config.temperature if config.temperature is not None else "provider_default",
        "tool_choice": "auto",
        "extra_request_body": extra,
        "search_backend": tool_config.search_backend,
        "recorded_at": utc_now(),
    }


def effective_config(client, config: AgentConfig, tool_config: ToolConfig) -> dict:
    return {"glm": effective_glm_config(client, config, tool_config), "agent": asdict(config),
            "tools": asdict(tool_config), "prompt_version": PROMPT_VERSION, "bundle_version": BUNDLE_VERSION}


def build_user_message(payload: dict, include_level3: bool, max_steps: int | None = None,
                       notes: dict | None = None, requested: list[dict] | None = None) -> str:
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
        lines += ["", f"Research budget: about {max_steps} model turns. You may finish earlier."]
    lines += ["", "Start researching."]
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
        self.extra = request_extra(config)
        self.usage = {group: trace.empty_usage() for group in trace.PHASE_GROUPS}
        self.last_content: str | None = None
        self.run_context = run_context if run_context is not None else {}
        self.cancel_event = cancel_event

    def __call__(self, messages: list[dict], *, phase: str, tools: list[dict] | None = None,
                 model: str | None = None, meta: dict | None = None, activity: dict | None = None) -> dict:
        """`meta` adds explicit context to the logged model_response (e.g. the field and attempt of a
        field-recovery turn). It never changes `phase`, which usage accounting groups by. `activity`
        only labels this call's request-lifecycle events (e.g. the research turn); it is not logged
        on the model_response."""
        check_cancelled(self.cancel_event)
        meta = meta or {}
        self.run_context.clear()
        self.run_context.update({"field": meta.get("field"), "field_attempt": meta.get("attempt"),
                                 "turn": meta.get("turn"), **(activity or {})})
        kwargs: dict[str, Any] = {"tools": tools, "temperature": self.config.temperature,
                                  "max_tokens": self.config.max_tokens, "extra": self.extra or None}
        if model:
            kwargs["model"] = model
        response = self.client.chat(messages, **kwargs)
        latency = int(getattr(response, "latency_ms", 0) or 0)
        trace.add_usage(self.usage[trace.phase_group(phase)], response.usage, latency)
        raw = getattr(response, "raw", {}) or {}
        content = response.message.get("content")
        if (content or "").strip():
            self.last_content = content
        self.run_log.event("model_response", phase=phase, model=model or research_model_of(self.client),
                           finish_reason=response.finish_reason, usage=response.usage,
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
                 on_documents: Callable[[list[str], str], None] | None = None):
        self.ctx, self.run_log, self.config = ctx, run_log, config
        self.tracker = ResearchTracker()
        self.tool_calls: list[dict] = []
        self.step = 0                      # global tool-turn counter across phases
        self.cancel_event = cancel_event
        self.on_documents = on_documents   # e.g. the deterministic harvester (every phase, every new document)
        self.blocked = 0
        self.turn_results: list[dict] = []

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
            self.run_log.event("tool_call", step=step, phase=phase, call_id=call.get("id"), name=name,
                               arguments=raw_args, **tags)
            t_tool = time.monotonic()
            result = dispatch(self.ctx, name, raw_args)
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
            self.tool_calls.append({
                "step": step, "phase": phase, **tags, "name": name, "arguments": raw_args, "duration_ms": elapsed,
                "cache_hit": result.get("cache_hit") if isinstance(result, dict) else None,
                "error": result.get("error") if isinstance(result, dict) else None,
                "document_id": result.get("document_id") if isinstance(result, dict) else None,
                "duplicate": duplicate,
            })
            messages.append({
                "role": "tool", "tool_call_id": call_id,
                "content": model_view(result, self.config.max_tool_output_chars, duplicate=duplicate, note=note),
                "_compact": compact_stub(name, raw_args, result, self.config.compact_tool_output_chars),
            })
        return self.tracker.end_turn()


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
        run_log.event("field_recovery_finished", **record)

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
        "stopped": stopped,
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
    """The bounded Model Document Sweep (see src/document_sweep.py). Cached-document tools only: a search
    or fetch call is refused without being executed, so this stage makes 0 searches and 0 fetches.
    Returns the sweep summary; GLMError ends the sweep (recovery still runs), control-flow exceptions
    propagate."""
    from .candidate_harvest import candidate_matrix
    from .document_sweep import (DOCUMENT_SWEEP_TOOLS, EXTERNAL_TOOLS, MAX_SWEEP_TURNS, inspection_has_content,
                                 promoted_or_missed, sweep_packet, sweep_summary, sweep_tool_specs)

    events = trace_events(run_log)
    before = current_evaluation(events, specs, config.target_market)
    max_turns = max(0, min(int(config.document_sweep_max_turns or 0), MAX_SWEEP_TURNS))
    matrix = candidate_matrix(events, specs, vehicle)
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
        run_log.event("document_sweep_skipped", reason=skipped, fields_unresolved=len(eligible))
        return summary
    phase_ref["name"] = "document_sweep"
    packet = sweep_packet(payload=payload, specs=specs, evaluation=before, matrix=matrix, events=events,
                          doc_metas=doc_metas, target_market=config.target_market, max_turns=max_turns,
                          per_field=config.document_sweep_candidates_per_field,
                          max_chars=config.document_sweep_packet_max_chars)
    presented = sum(len(v) for v in packet["deterministic_candidates"].values())
    start_seq = run_log.seq
    run_log.event("document_sweep_started", turn_budget=max_turns, fields_to_review=packet["fields_to_review"],
                  candidates_presented=presented, fields_without_candidates=packet["fields_without_candidates"],
                  documents=len(doc_metas), packet_chars=len(json.dumps(packet, ensure_ascii=False, default=str)),
                  allowed_tools=list(DOCUMENT_SWEEP_TOOLS))
    messages = [{"role": "system", "content": DOCUMENT_SWEEP_SYSTEM_PROMPT},
                {"role": "user", "content": "Document sweep task (JSON):\n"
                                            + json.dumps(packet, ensure_ascii=False, default=str)}]
    specs_for_tools = sweep_tool_specs(tool_specs())
    calls_before = len(session.tool_calls)
    blocked_before = session.blocked
    reply_text, turns, error = None, 0, None
    follow_up: dict | None = None
    try:
        for turn_index in range(1, max_turns + 1):
            message = caller(outgoing_messages(messages, config), phase="document_sweep", tools=specs_for_tools,
                             meta={"turn": turn_index, "turn_budget": max_turns})
            turns += 1
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
            inspected = [r for r in session.turn_results if inspection_has_content(r["name"], r["result"])]
            if not inspected:
                break
            follow_up = {"after_turn": turn_index, "inspection_results": len(inspected),
                         "tools": sorted({r["name"] for r in inspected})}
            run_log.event("document_sweep_follow_up", **follow_up)
            messages[-1]["content"] += ("\n[operational note] Final document-sweep turn: read the inspection "
                                        "results above, then store_evidence / report_field_status. Results of "
                                        "calls made in this turn will not be shown to you.")
    except GLMError as exc:
        error = _error_text(exc)
        run_log.event("document_sweep_failed", error=error, api_error=exc.as_dict())
    events = trace_events(run_log)
    after = current_evaluation(events, specs, config.target_market)
    sweep_evidence = [e["evidence"] for e in events if e.get("kind") == "evidence" and (e.get("seq") or 0) > start_seq
                      and isinstance(e.get("evidence"), dict)]
    promoted, missed = promoted_or_missed(sweep_evidence, events)
    for item in missed:
        run_log.event("candidate_missed_by_deterministic_harvest", field=item.get("field"), value=item.get("value"),
                      unit=item.get("unit"), document_id=item.get("document_id"), source_url=item.get("source_url"),
                      quote=item.get("quote"), evidence_id=item.get("evidence_id"))
    executed = session.tool_calls[calls_before:]
    external = sum(1 for c in executed if c["name"] in EXTERNAL_TOOLS and not c.get("blocked"))
    reply, _ = parse_model_output(reply_text)
    summary = sweep_summary(before=before, after=after, sweep_evidence=sweep_evidence, promoted=promoted,
                            missed=missed, presented=presented, model_calls=turns, turns=turns,
                            blocked=session.blocked - blocked_before, external_calls=external,
                            reply=reply if reply is not None else (reply_text or None))
    summary["error"] = error
    summary["follow_up_turn"] = follow_up
    run_log.event("document_sweep_finished", **{k: v for k, v in summary.items() if k != "reply"},
                  reply=summary["reply"])
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
    research_model, finalizer_model = research_model_of(client), finalizer_model_of(client)
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
                        on_documents=harvester.observe if config.layered_harvest_enabled else None)
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
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(payload, config.include_level3, config.max_steps,
                                                       notes_for_variant, specs)},
    ]
    run_log.write_input(payload)
    run_log.event("run_started", model=research_model, research_model=research_model,
                  finalizer_model=finalizer_model, record_id=record_id, prompt_version=PROMPT_VERSION,
                  bundle_version=BUNDLE_VERSION, max_steps=config.max_steps,
                  search_backend=tool_config.search_backend, glm_config=glm_config,
                  agent_config=asdict(config), tool_config=asdict(tool_config), pricing=pricing,
                  pricing_finalizer=pricing_finalizer, variant_notes=notes_for_variant,
                  requested_fields=requested_fields, requested_field_specs=[public_spec(s) for s in specs],
                  target_market=config.target_market, vehicle_label=vehicle_ctx)

    status: str | None = None
    stop_reason: str | None = None
    final_text: str | None = None
    output, parse_note = None, None
    error, api_error = None, None
    finalization: dict | None = None
    recovery: dict | None = None
    bundle: dict | None = None
    harvest_summary: dict | None = None
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
        cost, cost_details = run_cost(trace.sum_usage(usage_research, usage_sweep, usage_recovery), usage_finalizer,
                                      search_calls, pricing, pricing_finalizer, stats["unknown_usage_attempts"])
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
        try:
            for step in range(1, config.max_steps + 1):
                message = caller(outgoing_messages(messages, config), phase="research", tools=tool_specs(),
                                 activity={"turn": step})
                messages.append(_assistant_echo(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    final_text = message.get("content") or ""
                    stop_reason, steps_done = "model_finished", step
                    break
                novelty = tools.execute(calls, messages, phase="research")
                steps_done = step
                remaining = config.max_steps - step
                notes = []
                if not novelty.total and remaining > 0:
                    notes.append("This turn produced no new document, evidence item or source.")
                if (config.no_new_research_turns and tracker.idle_turns >= config.no_new_research_turns
                        and remaining > 0):
                    stop_reason = "no_new_research"
                    break
                if 0 < remaining <= 2:
                    notes.append(f"{remaining} research turn(s) left in the budget; store_evidence (with market) "
                                 "for values you intend to use, and favour unresolved or conflicting Level 2 "
                                 "fields over Level 3.")
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

        # ---------------- deterministic harvest + model document sweep ----------------
        if status is None and config.layered_harvest_enabled:
            phase["name"] = "deterministic_harvest"
            harvester.observe(ctx.documents_opened, "deterministic_harvest")
            harvest_summary = harvest_report(trace_events(run_log), specs, vehicle_ctx, harvester, config)
            run_log.event("deterministic_harvest_summary", **harvest_summary)
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
                recovery = run_field_recovery(session=tools, caller=caller, specs=specs, payload=payload,
                                              config=config, run_log=run_log, cache=cache,
                                              documents_dir=documents_dir, operator_notes=notes_for_variant,
                                              phase_ref=phase, vehicle=vehicle_ctx)
            except Exception as exc:  # recovery problems never cost the primary research
                recovery = {"error": _error_text(exc), "attempt_count": 0}
                run_log.event("field_recovery_failed", error=recovery["error"])

        # ---------------- finalization phase ----------------
        if status is None:
            retried = bool(recovery and recovery.get("attempt_count"))
            swept = bool(sweep and sweep.get("model_calls"))
            fin = None
            if stop_reason == "model_finished" and not retried and not swept:
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
                    fin = run_finalization(caller, run_log=run_log, payload=payload, config=config, cache=cache,
                                           documents_dir=documents_dir, stop_reason=stop_reason,
                                           model=finalizer_model,
                                           request_path=run_log.dir / "finalizer_request.json", bundle=bundle)
            if output is None and fin is not None:
                finalization, bundle = fin["info"], fin["bundle"]
                if stop_reason != "model_finished":
                    final_text = fin["text"]
                if fin["error"]:
                    status, error, api_error = "finalization_failed", fin["error"], fin["api_error"]
                    parse_note = fin["parse_note"]
                else:
                    output, parse_note = fin["output"], f"finalizer:{fin['parse_note']}"
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

    matrix = candidate_matrix(events, specs, vehicle)
    evaluation = current_evaluation(events, specs, config.target_market)
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

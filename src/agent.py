"""GLM agent for one vehicle: research, targeted field retries, then a compact finalization.

    RESEARCH PHASE      tool-calling turns with GLM_MODEL; raw trace -> events.jsonl,
                        documents -> document cache, evidence -> evidence store
          ↓
    FIELD DETECTION     every REQUESTED enrichment field (schema-driven, src/fields.py) is
                        evaluated from the model's own evidence/declarations
          ↓
    FIELD RETRIES       one focused, compact-context research task per failed field
                        (src/field_recovery.py); successful fields are never re-researched
          ↓
    COMPACT BUNDLE      identity, targets, evidence, candidate facts, document metadata,
                        excerpts, concise actions, missing targets (src/bundle.py)
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

from .bundle import BUNDLE_VERSION, build_research_bundle
from .glm_client import GLMError
from .context import ResearchTracker, call_signature, compact_stub, model_view, replay_result
from .storage.trace import FETCH_TOOLS
from .field_recovery import evaluate_fields, parse_retry_reply, retry_packet, retry_queue
from .fields import grouped, parse_field_list, propulsion_of, resolve_requested_fields
from .pricing import UNKNOWN_USAGE_NOTE, default_pricing, run_cost
from .schemas import LEVEL3_TOPICS, parse_model_output
from .storage import trace
from .storage.cache import DocumentCache
from .storage.run_log import RunLog, read_events, utc_now
from .tools import ToolConfig, ToolContext, dispatch, tool_specs
from .tools.evidence import EvidenceStore
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
  containing the field, exact value, source URL / document_id, a short verbatim quote, market and notes.
  A document_id is not an evidence id."""

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
- Do not spend turns re-confirming Level 2 values that are already well supported. Spend remaining turns
  on unresolved fields, conflicting fields, missing evidence records and exact Israeli-market provenance.
  Level 3 (reliability, resale, insurance, recalls) should not take the budget away from clean Level 2
  evidence.
- Re-fetching a URL you already fetched returns the same stored document. Older tool results in this
  conversation are shortened, but every document_id stays valid and can be queried again.
- If a requested field does not exist for this vehicle (e.g. a fuel tank on an EV), call
  report_field_status(field, "not_applicable"). If you could not resolve a field, you may report
  "unresolved"; such fields get a separate focused follow-up later, so do not loop on them now.
- When storing evidence, pass variant_match: exact | different | unclear.
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

You cannot browse or call tools. Organize the research into the final answer. You decide how to use
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
  which value applies to the exact target variant (store that evidence first); otherwise conflicting.
  A reply of "found" does not resolve a conflict.
- If the field does not exist for this vehicle, say not_applicable. If you cannot resolve it within the
  budget, say unresolved. Never invent a value.

When done, reply with ONLY one JSON object (no tool call):
{"field": "<the requested field name>",
 "status": "found | conflict_resolved | not_applicable | unresolved | conflicting | foreign_market_only | variant_not_exact",
 "value": <any or null>, "unit": "<unit or null>", "market": "<market or null>",
 "evidence_ids": ["e12"], "notes": "..."}"""

REPAIR_PROMPT = ("Your last reply could not be parsed as JSON. Return the same content as ONE valid JSON object "
                 "and nothing else.")

PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + FINALIZER_SYSTEM_PROMPT + FIELD_RECOVERY_SYSTEM_PROMPT
                                 + BUNDLE_VERSION).encode("utf-8")).hexdigest()[:12]

STOP_REASONS = ("model_finished", "max_steps", "no_new_research", "user_cancelled", "api_failure",
                "research_exception")
STATUSES = ("completed", "max_steps_finalized", "no_new_research_finalized", "completed_unparsed",
            "finalization_failed", "research_failed", "interrupted", "incomplete", "recovered_finalized")
FINALIZED_STATUS = {"max_steps": "max_steps_finalized", "no_new_research": "no_new_research_finalized"}
PARTIAL_STATUSES = ("interrupted", "research_failed", "finalization_failed", "incomplete")
PHASE_LABELS = {"research": "Research", "field_detection": "Field Detection", "field_recovery": "Field Recovery",
                "finalization": "Finalization"}


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
    include_level3: bool = True
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


class ModelCaller:
    """Calls GLM, logs every response as a `model_response` event and books usage by phase."""

    def __init__(self, client, run_log: RunLog, config: AgentConfig):
        self.client, self.run_log, self.config = client, run_log, config
        self.extra = request_extra(config)
        self.usage = {group: trace.empty_usage() for group in trace.PHASE_GROUPS}
        self.last_content: str | None = None

    def __call__(self, messages: list[dict], *, phase: str, tools: list[dict] | None = None,
                 model: str | None = None, meta: dict | None = None) -> dict:
        """`meta` adds explicit context to the logged model_response (e.g. the field and attempt of a
        field-recovery turn). It never changes `phase`, which usage accounting groups by."""
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
                     model: str, request_path: Path | None = None) -> dict:
    """Build the compact bundle from the run's events and make the no-tools finalization call(s).

    Never raises for API/parse problems (they are returned in `error`); lets
    KeyboardInterrupt and other BaseExceptions through to the caller.
    """
    events = trace_events(run_log)
    bundle = build_research_bundle(events, payload, cache=cache, documents_dir=documents_dir,
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
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps({"model": model, "phase": phase, "created_at": info["started_at"],
                                            "messages": messages}, ensure_ascii=False, indent=1), "utf-8")
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

    def __init__(self, ctx: ToolContext, run_log: RunLog, config: AgentConfig):
        self.ctx, self.run_log, self.config = ctx, run_log, config
        self.tracker = ResearchTracker()
        self.tool_calls: list[dict] = []
        self.step = 0                      # global tool-turn counter across phases

    def execute(self, calls: list[dict], messages: list[dict], *, phase: str, **tags: Any):
        """Run one model turn's tool calls, append the tool messages, return the turn's novelty."""
        self.step += 1
        step = self.step
        self.tracker.begin_turn(step)
        for call in calls:
            fn = call.get("function") or {}
            name, raw_args = fn.get("name", ""), fn.get("arguments")
            call_id = call.get("id") or f"call_{len(self.tool_calls) + 1}"
            signature = call_signature(name, raw_args)
            prior = self.tracker.lookup(signature)
            if prior is not None:
                result = replay_result(prior)
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
                       operator_notes: dict | None, phase_ref: dict) -> dict:
    """Detect failed requested fields from the research state, then run focused retries for them only.

    Generic over whatever fields were requested. The queue is dynamic: after EVERY attempt all
    requested fields are re-evaluated, and a queued field that another field's recovery resolved
    (e.g. by-product evidence from the same spec table) is skipped without its own model call.
    KeyboardInterrupt propagates; an API failure stops further retries (the run still finalizes).
    """
    events = trace_events(run_log)
    primary = evaluate_fields(specs, events, config.target_market)
    run_log.event("field_evaluation", stage="primary", fields=primary,
                  summary=_state_counts(primary))
    queue = retry_queue(primary, specs, config.field_recovery_max_attempts) if config.field_recovery_enabled else []
    run_log.event("field_retry_queue", fields=[q["field"] for q in queue], queue=queue,
                  enabled=config.field_recovery_enabled)
    by_name = {s["name"]: s for s in specs}
    queued = [q["field"] for q in queue]
    current = {e["field"]: e for e in primary}
    attempts_log: list[dict] = []
    resolved_directly: list[str] = []
    resolved_indirectly: dict[str, dict] = {}
    total_steps, stopped, cut_short = 0, None, None
    turns_saved = 0

    def reevaluate(during_field: str, attempt: int) -> None:
        """Refresh the state of every requested field; record queued fields another recovery resolved."""
        latest = evaluate_fields(specs, trace_events(run_log), config.target_market)
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

    for item in queue:
        name, previous = item["field"], []
        if not current[name]["retry_eligible"]:
            continue  # resolved by an earlier field's recovery: no retry for it
        for attempt in range(1, item["max_attempts"] + 1):
            if config.field_recovery_max_total_steps and total_steps >= config.field_recovery_max_total_steps:
                stopped = "max_total_steps"   # hard per-vehicle cap: no further attempt is started
                cut_short = name if attempt > 1 else None
                break
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
            run_log.event("field_recovery_started", field=name, attempt=attempt, max_attempts=item["max_attempts"],
                          failure_reason=before["state"], turn_budget=config.field_recovery_max_steps,
                          already_attempted_operations=len(packet["already_attempted_operations"]),
                          prior_excerpt_items=packet["prior_excerpt_stats"]["items"],
                          prior_excerpt_chars=packet["prior_excerpt_stats"]["chars"],
                          prior_excerpts=packet["prior_relevant_excerpts"],
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
                    if config.field_recovery_max_total_steps and total_steps >= config.field_recovery_max_total_steps:
                        stopped = "max_total_steps"   # hard per-vehicle cap: no further model turn
                        cut_short = name
                        break
                    meta = {"field": name, "attempt": attempt, "max_attempts": item["max_attempts"],
                            "turn": turn_index, "turn_budget": budget}
                    message = caller(outgoing_messages(messages, config), phase="field_recovery",
                                     tools=tool_specs(), meta=meta)
                    turns += 1
                    total_steps += 1
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
                    now = evaluate_fields([by_name[name]], trace_events(run_log), config.target_market)[0]
                    if now["state"] == "ok":
                        early = True
                        turns_saved += 1
                        run_log.event("field_recovery_early_resolved", field=name, attempt=attempt,
                                      after_turn=turn_index, state="ok", turn_budget=budget,
                                      evidence_ids=now["evidence_ids"])
                        break
                    idle = 0 if novelty.total else idle + 1
                    if config.no_new_research_turns and idle >= config.no_new_research_turns:
                        break
            except GLMError as exc:  # stop spending on retries; finalize with what we have
                error = _error_text(exc)
                stopped = "api_failure"
                run_log.event("field_recovery_failed", field=name, attempt=attempt, error=error,
                              api_error=exc.as_dict())
            reply = parse_retry_reply(reply_text, name)
            if reply and reply.get("status"):
                run_log.event("field_status", field=name, status=str(reply["status"]).lower(),
                              note=reply.get("notes"), source=f"field_recovery_attempt_{attempt}")
            reevaluate(name, attempt)
            after = current[name]
            if not after["retry_eligible"] and name not in resolved_directly:
                resolved_directly.append(name)
            excerpt_docs = {e.get("document_id") for e in packet["prior_relevant_excerpts"] if e.get("document_id")}
            rereads = sum(1 for c in session.tool_calls
                          if c.get("phase") == "field_recovery" and c.get("field") == name
                          and c.get("attempt") == attempt and c["name"] in ("get_cached_document", "extract_html")
                          and _call_document(c) in excerpt_docs)
            record = {"field": name, "attempt": attempt, "state_before": before["state"],
                      "state_after": after["state"], "turns": turns, "reply": reply,
                      "reply_text": None if reply else reply_text, "error": error, "early_resolved": early,
                      "packet_chars": len(json.dumps(packet, ensure_ascii=False, default=str)),
                      "prior_excerpt_items": packet["prior_excerpt_stats"]["items"],
                      "prior_excerpt_chars": packet["prior_excerpt_stats"]["chars"],
                      "document_rereads_after_prior_excerpt": rereads}
            attempts_log.append(record)
            previous.append({k: record[k] for k in ("attempt", "state_after", "reply", "turns")})
            run_log.event("field_recovery_finished", **record)
            if not after["retry_eligible"] or stopped:
                break
        if stopped:
            break
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
    return {
        "enabled": config.field_recovery_enabled,
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
        "turns": total_steps,
        "turns_saved_by_early_resolution": turns_saved,
        "prior_excerpt_items": sum(a.get("prior_excerpt_items") or 0 for a in attempts_log),
        "prior_excerpt_chars": sum(a.get("prior_excerpt_chars") or 0 for a in attempts_log),
        "attempts_with_prior_excerpts": sum(1 for a in attempts_log if a.get("prior_excerpt_items")),
        "document_rereads_after_prior_excerpt": sum(a.get("document_rereads_after_prior_excerpt") or 0
                                                    for a in attempts_log),
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


def run_vehicle(row: dict, payload: dict, *, client, cache: DocumentCache, run_log: RunLog,
                config: AgentConfig, tool_config: ToolConfig, vehicle_meta: dict | None = None,
                batch_id: str = "", ordinal: int | None = None, session=None,
                pricing: dict | None = None, pricing_finalizer: dict | None = None,
                persist: Callable[[dict], Any] | None = None) -> dict:
    """Research one vehicle, retry failed requested fields, finalize from a compact bundle, persist.

    `persist(result)` runs on EVERY exit path (default: write result.json). On
    KeyboardInterrupt (or another BaseException such as a Streamlit stop) the
    partial result is persisted first and the exception is re-raised; no further
    model call is made.
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
    tools = ToolSession(ctx, run_log, config)
    tracker = tools.tracker
    caller = ModelCaller(client, run_log, config)
    phase = {"name": "research"}

    def api_hook(kind: str, **data: Any) -> None:
        if kind == "api_error":
            ctx.counters["api_errors"] += 1
            api_errors.append({k: v for k, v in data.items()})
        run_log.event(kind, phase=phase["name"], **data)

    previous_hook = getattr(client, "hook", None)
    client.hook = api_hook
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
                  requested_fields=requested_fields, requested_field_specs=specs,
                  target_market=config.target_market)

    status: str | None = None
    stop_reason: str | None = None
    final_text: str | None = None
    output, parse_note = None, None
    error, api_error = None, None
    finalization: dict | None = None
    recovery: dict | None = None
    bundle: dict | None = None
    steps_done = 0
    research_seconds: float | None = None
    interrupted: BaseException | None = None

    try:
        # ---------------- research phase ----------------
        try:
            for step in range(1, config.max_steps + 1):
                message = caller(outgoing_messages(messages, config), phase="research", tools=tool_specs())
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

        # ---------------- failed-field detection + targeted field retries ----------------
        if status is None:
            phase["name"] = "field_detection"
            try:
                recovery = run_field_recovery(session=tools, caller=caller, specs=specs, payload=payload,
                                              config=config, run_log=run_log, cache=cache,
                                              documents_dir=run_log.dir / "documents",
                                              operator_notes=notes_for_variant, phase_ref=phase)
            except Exception as exc:  # recovery problems never cost the primary research
                recovery = {"error": _error_text(exc), "attempt_count": 0}
                run_log.event("field_recovery_failed", error=recovery["error"])

        # ---------------- finalization phase ----------------
        if status is None:
            retried = bool(recovery and recovery.get("attempt_count"))
            if stop_reason == "model_finished" and not retried:
                output, parse_note = parse_model_output(final_text)
                if output is not None:
                    status = "completed"
            if output is None:
                phase["name"] = "finalization"
                fin = run_finalization(caller, run_log=run_log, payload=payload, config=config, cache=cache,
                                       documents_dir=run_log.dir / "documents", stop_reason=stop_reason,
                                       model=finalizer_model, request_path=run_log.dir / "finalizer_request.json")
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
    except BaseException as exc:  # KeyboardInterrupt, Streamlit stop/rerun, SystemExit: persist, then re-raise
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

    duration = round(time.monotonic() - t0, 2)
    documents_dir = run_log.dir / "documents"
    for document_id in ctx.documents_opened:
        try:
            cache.export(document_id, documents_dir)
        except OSError:
            pass
    events = trace_events(run_log)
    if recovery is None and status == "interrupted":
        recovery = trace.field_recovery_summary(events)   # interrupted mid-recovery: history from events
    if bundle is None:
        # Not sent anywhere: the partial research bundle is stored for the UI and for later recovery.
        bundle = build_research_bundle(events, payload, cache=cache, documents_dir=documents_dir,
                                       include_level3=config.include_level3,
                                       max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason,
                                       target_market=config.target_market)
    counters = dict(ctx.counters)
    stats = trace.api_stats(events, glm_config.get("chat_path") or "chat/completions")
    usage_research, usage_finalizer = caller.usage["research"], caller.usage["finalization"]
    usage_recovery = caller.usage["field_recovery"]
    usage = trace.sum_usage(usage_research, usage_recovery, usage_finalizer)
    search_calls = counters.get("search_api_calls", 0)
    cost, cost_details = run_cost(trace.sum_usage(usage_research, usage_recovery), usage_finalizer, search_calls,
                                  pricing, pricing_finalizer, stats["unknown_usage_attempts"])
    result = {
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
        "status": status,
        "stop_reason": stop_reason,
        "research_steps": steps_done,
        "error": error,
        "api_error": api_error,
        "api_errors": api_errors,
        "finalizer_error": finalization.get("error") if finalization else None,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_s": duration,
        "research_duration_s": research_seconds,
        "output": output,
        "parse_note": parse_note,
        "raw_final_text": final_text,
        "last_model_content": caller.last_content,
        "evidence": evidence.items,
        "documents": list(ctx.documents_opened),
        "tool_calls": tools.tool_calls,
        "counters": counters,
        "research_tracking": tracker.snapshot(),
        "field_recovery": recovery,
        "usage": usage,
        "usage_research": usage_research,
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
        "research_bundle": bundle,
        "documents_dir": str(documents_dir) if ctx.documents_opened else None,
        "result_source": "result.json",
        "recovered": False,
        "partial": status in PARTIAL_STATUSES,
        "interrupted": interrupted is not None,
        "interrupted_phase": phase["name"] if interrupted is not None else None,
        "interruption_type": type(interrupted).__name__ if interrupted is not None else None,
        "interruption_message": interruption_message(phase["name"]) if interrupted is not None else None,
    }
    trace.apply_current_states(recovery, (bundle or {}).get("field_states"))
    # Persist first, then announce: a UI stop raised by the run_finished callback cannot lose the result.
    try:
        persist(result)
    except Exception as exc:  # disk problems must not hide the in-memory result from the caller
        run_log.event("result_write_failed", error=_error_text(exc))
    run_log.event("run_finished", status=status, stop_reason=stop_reason, duration_s=duration, usage=usage,
                  search_api_calls=search_calls, cost=cost, api_stats=stats)
    if interrupted is not None:
        raise interrupted
    return result

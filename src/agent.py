"""GLM agent for one vehicle: a research phase, then a compact finalization phase.

    RESEARCH PHASE      tool-calling turns with GLM_MODEL; raw trace -> events.jsonl,
                        documents -> document cache, evidence -> evidence store
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
from .context import ResearchTracker, compact_stub, model_view
from .pricing import UNKNOWN_USAGE_NOTE, default_pricing, run_cost
from .schemas import LEVEL2_TARGET_FIELDS, LEVEL3_TOPICS, parse_model_output
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

PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + FINALIZER_SYSTEM_PROMPT + BUNDLE_VERSION).encode("utf-8")
                                ).hexdigest()[:12]

REPAIR_PROMPT = ("Your last reply could not be parsed as JSON. Return the same content as ONE valid JSON object "
                 "and nothing else.")

STOP_REASONS = ("model_finished", "max_steps", "no_new_research", "user_cancelled", "api_failure",
                "research_exception")
STATUSES = ("completed", "max_steps_finalized", "no_new_research_finalized", "completed_unparsed",
            "finalization_failed", "research_failed", "interrupted", "incomplete", "recovered_finalized")
FINALIZED_STATUS = {"max_steps": "max_steps_finalized", "no_new_research": "no_new_research_finalized"}


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


AGENT_ENV = {
    "max_steps": "AGENT_MAX_STEPS",
    "max_tool_output_chars": "AGENT_MAX_TOOL_OUTPUT_CHARS",
    "keep_recent_tool_results": "AGENT_KEEP_RECENT_TOOL_RESULTS",
    "compact_tool_output_chars": "AGENT_COMPACT_TOOL_OUTPUT_CHARS",
    "no_new_research_turns": "AGENT_NO_NEW_RESEARCH_TURNS",
    "finalizer_bundle_max_chars": "AGENT_FINALIZER_BUNDLE_MAX_CHARS",
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


def agent_config_from_env(env: Callable[[str], str | None] = os.environ.get, **overrides) -> AgentConfig:
    return AgentConfig(**{**_env_ints(AGENT_ENV, env), **overrides})


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
                       notes: dict | None = None) -> str:
    targets = {group: list(fields.items()) for group, fields in LEVEL2_TARGET_FIELDS.items()}
    lines = [
        "Level 1.5 record (fixed context):",
        json.dumps(payload, ensure_ascii=False, indent=1),
        "",
    ]
    if notes:
        lines += ["Operator notes for this variant (context and inferences, not verified facts):",
                  json.dumps(notes, ensure_ascii=False, indent=1), ""]
    lines += [
        "Level 2 target fields (suggested keys and meaning; skip what does not apply, add what you find useful):",
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
        self.usage = {"research": trace.empty_usage(), "finalization": trace.empty_usage()}
        self.last_content: str | None = None

    def __call__(self, messages: list[dict], *, phase: str, tools: list[dict] | None = None,
                 model: str | None = None) -> dict:
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
                           tool_calls=response.message.get("tool_calls"))
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
                                   max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason)
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


def run_vehicle(row: dict, payload: dict, *, client, cache: DocumentCache, run_log: RunLog,
                config: AgentConfig, tool_config: ToolConfig, vehicle_meta: dict | None = None,
                batch_id: str = "", ordinal: int | None = None, session=None,
                pricing: dict | None = None, pricing_finalizer: dict | None = None,
                persist: Callable[[dict], Any] | None = None) -> dict:
    """Research one vehicle, finalize from a compact bundle, persist and return the result record.

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
    api_errors: list[dict] = []
    tracker = ResearchTracker()
    caller = ModelCaller(client, run_log, config)
    phase = {"name": "research"}

    def api_hook(kind: str, **data: Any) -> None:
        if kind == "api_error":
            ctx.counters["api_errors"] += 1
            api_errors.append({k: v for k, v in data.items()})
        run_log.event(kind, phase=phase["name"], **data)

    previous_hook = getattr(client, "hook", None)
    client.hook = api_hook
    tool_calls: list[dict] = []
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(payload, config.include_level3, config.max_steps,
                                                       variant_notes(record_id_of(payload) or record_id))},
    ]
    run_log.write_input(payload)
    run_log.event("run_started", model=research_model, research_model=research_model,
                  finalizer_model=finalizer_model, record_id=record_id, prompt_version=PROMPT_VERSION,
                  bundle_version=BUNDLE_VERSION, max_steps=config.max_steps,
                  search_backend=tool_config.search_backend, glm_config=glm_config,
                  agent_config=asdict(config), tool_config=asdict(tool_config), pricing=pricing,
                  pricing_finalizer=pricing_finalizer,
                  variant_notes=variant_notes(record_id_of(payload) or record_id))

    status: str | None = None
    stop_reason: str | None = None
    final_text: str | None = None
    output, parse_note = None, None
    error, api_error = None, None
    finalization: dict | None = None
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
                tracker.begin_turn(step)
                for call in calls:
                    fn = call.get("function") or {}
                    name, raw_args = fn.get("name", ""), fn.get("arguments")
                    run_log.event("tool_call", step=step, call_id=call.get("id"), name=name, arguments=raw_args)
                    t_tool = time.monotonic()
                    result = dispatch(ctx, name, raw_args)
                    elapsed = int((time.monotonic() - t_tool) * 1000)
                    run_log.event("tool_result", step=step, call_id=call.get("id"), name=name, result=result)
                    duplicate, note = tracker.observe(name, raw_args, result)
                    if duplicate:
                        run_log.event("duplicate_work", step=step, name=name, arguments=raw_args, note=note)
                    tool_calls.append({
                        "step": step, "name": name, "arguments": raw_args, "duration_ms": elapsed,
                        "cache_hit": result.get("cache_hit") if isinstance(result, dict) else None,
                        "error": result.get("error") if isinstance(result, dict) else None,
                        "document_id": result.get("document_id") if isinstance(result, dict) else None,
                        "duplicate": duplicate,
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id") or f"call_{len(tool_calls)}",
                        "content": model_view(result, config.max_tool_output_chars, duplicate=duplicate, note=note),
                        "_compact": compact_stub(name, raw_args, result, config.compact_tool_output_chars),
                    })
                novelty = tracker.end_turn()
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

        # ---------------- finalization phase ----------------
        if status is None:
            if stop_reason == "model_finished":
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
    except BaseException as exc:  # KeyboardInterrupt, Streamlit stop, SystemExit: persist, then re-raise
        interrupted = exc
        status = "interrupted"
        stop_reason = stop_reason or "user_cancelled"
        error = f"{type(exc).__name__}: run interrupted during {phase['name']}"
        if research_seconds is None:
            research_seconds = round(time.monotonic() - t0, 2)
        try:
            run_log.event("interrupted", phase=phase["name"], step=steps_done, exception=type(exc).__name__)
        except BaseException:
            pass
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
    if bundle is None:
        # Not sent anywhere: the partial research bundle is stored for the UI and for later recovery.
        bundle = build_research_bundle(events, payload, cache=cache, documents_dir=documents_dir,
                                       include_level3=config.include_level3,
                                       max_chars=config.finalizer_bundle_max_chars, stop_reason=stop_reason)
    counters = dict(ctx.counters)
    stats = trace.api_stats(events, glm_config.get("chat_path") or "chat/completions")
    usage_research, usage_finalizer = caller.usage["research"], caller.usage["finalization"]
    usage = trace.sum_usage(usage_research, usage_finalizer)
    search_calls = counters.get("search_api_calls", 0)
    cost, cost_details = run_cost(usage_research, usage_finalizer, search_calls, pricing, pricing_finalizer,
                                  stats["unknown_usage_attempts"])
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
        "tool_calls": tool_calls,
        "counters": counters,
        "research_tracking": tracker.snapshot(),
        "usage": usage,
        "usage_research": usage_research,
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
    }
    try:
        run_log.event("run_finished", status=status, stop_reason=stop_reason, duration_s=duration, usage=usage,
                      search_api_calls=search_calls, cost=cost, api_stats=stats)
    except BaseException:
        pass
    try:
        persist(result)
    except Exception as exc:  # disk problems must not hide the in-memory result from the caller
        run_log.event("result_write_failed", error=_error_text(exc))
    if interrupted is not None:
        raise interrupted
    return result


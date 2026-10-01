"""GLM agent loop for one vehicle.

The loop is deliberately permissive: the model chooses tools and sources,
decides how to handle conflicts and shapes its answer. The code only keeps the
conversation within technical limits and logs everything.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .schemas import LEVEL2_TARGET_FIELDS, LEVEL3_TOPICS, parse_model_output
from .storage.cache import DocumentCache
from .storage.run_log import RunLog, utc_now
from .tools import ToolConfig, ToolContext, dispatch, tool_specs
from .tools.evidence import EvidenceStore

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
- Large documents: use find_in_document / extract_tables / extract_html with offset instead of
  re-fetching. get_cached_document returns documents already downloaded in this benchmark.

When you are done, reply with ONLY one JSON object (no tool call) shaped like:
{
  "vehicle_id": "<government record id>",
  "summary": "short description of what you found and how",
  "fields": {
    "<field_name>": {"value": <any>, "unit": "<unit or null>", "notes": "<optional>", "evidence_ids": ["e1"]}
  },
  "conflicts": [{"field": "<name>", "values": [<a>, <b>], "model_comment": "..."}],
  "additional_findings": [{"topic": "...", "finding": "...", "evidence_ids": []}],
  "level3": {"<topic>": {"finding": "...", "evidence_ids": []}},
  "research_trace": ["short step descriptions"]
}"""

PROMPT_VERSION = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]

FINALIZE_PROMPT = ("Your research step budget is exhausted. Do not call tools. Return your final answer now "
                   "as ONE JSON object in the agreed shape, including everything you found so far.")
REPAIR_PROMPT = ("Your last reply could not be parsed as JSON. Return the same content as ONE valid JSON object "
                 "and nothing else.")


@dataclass
class AgentConfig:
    max_steps: int = 30
    max_tool_output_chars: int = 12000
    keep_recent_tool_results: int = 8
    compact_tool_output_chars: int = 1200
    temperature: float | None = None
    max_tokens: int | None = None
    include_level3: bool = True
    extra_body: dict = field(default_factory=dict)


def build_user_message(payload: dict, include_level3: bool) -> str:
    targets = {group: list(fields.items()) for group, fields in LEVEL2_TARGET_FIELDS.items()}
    lines = [
        "Level 1.5 record (fixed context):",
        json.dumps(payload, ensure_ascii=False, indent=1),
        "",
        "Level 2 target fields (suggested keys and meaning; skip what does not apply, add what you find useful):",
    ]
    for group, fields in targets.items():
        lines.append(f"- {group}: " + "; ".join(f"{key} = {label}" for key, label in fields))
    if include_level3:
        lines += ["", "Level 3 open research (put results under `level3`):"]
        lines += [f"- {key}: {label}" for key, label in LEVEL3_TOPICS.items()]
    else:
        lines += ["", "Level 3 research is disabled for this run; leave `level3` empty."]
    lines += ["", "Start researching."]
    return "\n".join(lines)


def _compact_history(messages: list[dict], cfg: AgentConfig) -> list[dict]:
    """Shorten older tool results so long runs stay inside the context window.

    The full results remain in the run log; the model can re-open documents with
    get_cached_document.
    """
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = set(tool_positions[: -cfg.keep_recent_tool_results]) if cfg.keep_recent_tool_results else set(tool_positions)
    out = []
    for i, message in enumerate(messages):
        content = message.get("content") or ""
        if i in old and len(content) > cfg.compact_tool_output_chars:
            message = {**message, "content": content[: cfg.compact_tool_output_chars]
                       + " …[older tool output shortened; use get_cached_document/find_in_document to re-read]"}
        out.append(message)
    return out


def _tool_message_content(result: dict, limit: int) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + f" …[truncated {len(text) - limit} chars; page with offset or find_in_document]"
    return text


def _assistant_echo(message: dict) -> dict:
    echo: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        echo["tool_calls"] = message["tool_calls"]
    return echo


def run_vehicle(row: dict, payload: dict, *, client, cache: DocumentCache, run_log: RunLog,
                config: AgentConfig, tool_config: ToolConfig, vehicle_meta: dict | None = None,
                batch_id: str = "", ordinal: int | None = None, session=None) -> dict:
    """Research one vehicle and return the result record (also written to result.json)."""
    record_id = str(row.get("upstream_record_id"))
    started_at, t0 = utc_now(), time.monotonic()
    evidence = EvidenceStore()
    ctx = ToolContext(cache=cache, evidence=evidence, config=tool_config, glm=client,
                      vehicle=vehicle_meta or {"manufacturer": row.get("tozar")},
                      log=lambda kind, **data: run_log.event(kind, **data))
    if session is not None:
        ctx.session = session
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model_calls": 0}
    tool_calls: list[dict] = []
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(payload, config.include_level3)},
    ]
    run_log.write_input(payload)
    run_log.event("run_started", model=client.model, record_id=record_id, prompt_version=PROMPT_VERSION,
                  max_steps=config.max_steps, search_backend=tool_config.search_backend)

    def call_model(with_tools: bool) -> dict:
        response = client.chat(_compact_history(messages, config), tools=tool_specs() if with_tools else None,
                               temperature=config.temperature, max_tokens=config.max_tokens,
                               extra=config.extra_body or None)
        usage["model_calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[key] += int(response.usage.get(key) or 0)
        run_log.event("model_response", finish_reason=response.finish_reason, usage=response.usage,
                      content=response.message.get("content"),
                      reasoning_content=response.message.get("reasoning_content"),
                      tool_calls=response.message.get("tool_calls"))
        return response.message

    status, final_text, error = "completed", None, None
    try:
        for step in range(1, config.max_steps + 1):
            message = call_model(with_tools=True)
            messages.append(_assistant_echo(message))
            calls = message.get("tool_calls") or []
            if not calls:
                final_text = message.get("content") or ""
                break
            for call in calls:
                fn = call.get("function") or {}
                name, raw_args = fn.get("name", ""), fn.get("arguments")
                run_log.event("tool_call", step=step, call_id=call.get("id"), name=name, arguments=raw_args)
                t_tool = time.monotonic()
                result = dispatch(ctx, name, raw_args)
                elapsed = int((time.monotonic() - t_tool) * 1000)
                run_log.event("tool_result", step=step, call_id=call.get("id"), name=name, result=result)
                tool_calls.append({
                    "step": step, "name": name, "arguments": raw_args, "duration_ms": elapsed,
                    "cache_hit": result.get("cache_hit") if isinstance(result, dict) else None,
                    "error": result.get("error") if isinstance(result, dict) else None,
                    "document_id": result.get("document_id") if isinstance(result, dict) else None,
                })
                messages.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{len(tool_calls)}",
                                 "content": _tool_message_content(result, config.max_tool_output_chars)})
        else:
            status = "max_steps_finalized"
            messages.append({"role": "user", "content": FINALIZE_PROMPT})
            message = call_model(with_tools=False)
            messages.append(_assistant_echo(message))
            final_text = message.get("content") or ""

        output, parse_note = parse_model_output(final_text)
        if output is None and final_text:
            # Technical repair only: ask once for valid JSON; the raw text is kept either way.
            messages.append({"role": "user", "content": REPAIR_PROMPT})
            message = call_model(with_tools=False)
            output, parse_note = parse_model_output(message.get("content"))
            parse_note = f"repaired:{parse_note}"
            if output is None:
                status = "completed_unparsed" if status == "completed" else status
    except Exception as exc:  # the run log keeps whatever happened before the failure
        status, error, output, parse_note = "error", f"{type(exc).__name__}: {str(exc)[:500]}", None, "error"
        run_log.event("error", message=error)

    duration = round(time.monotonic() - t0, 2)
    result = {
        "record_id": record_id,
        "ordinal": ordinal,
        "batch_id": batch_id,
        "model": client.model,
        "prompt_version": PROMPT_VERSION,
        "status": status,
        "error": error,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_s": duration,
        "output": output,
        "parse_note": parse_note,
        "raw_final_text": final_text,
        "evidence": evidence.items,
        "documents": list(ctx.documents_opened),
        "tool_calls": tool_calls,
        "counters": dict(ctx.counters),
        "usage": usage,
    }
    run_log.event("run_finished", status=status, duration_s=duration, usage=usage)
    return result

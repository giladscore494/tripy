"""Compact research bundle: the ONLY research context the finalizer receives.

Built from events.jsonl (plus document metadata), so a live run and a recovery
of an old run produce the same bundle. The full raw trace stays on disk; the
bundle holds identity, targets, evidence, candidate facts, document metadata,
relevant excerpts the model already looked at, a concise action list, missing
targets and the last useful model text, capped at a configurable size.

Nothing here judges truth: candidate facts are grouped as stored, no source is
ranked or dropped for being unofficial, and fields with several stored values
are listed without picking a winner.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schemas import LEVEL3_TOPICS
from .storage import trace
from .variant_notes import record_id_of, variant_notes

BUNDLE_VERSION = "bundle-v2"
EXCERPT_CHARS = 1500
EXCERPTS_PER_DOCUMENT_CHARS = 5000
ACTION_LINE_CHARS = 220
MAX_ACTIONS = 80
MAX_MODEL_NOTES = 6
MODEL_NOTE_CHARS = 1500
CONFLICT_NOTE_CHARS = 300
MAX_CONFLICT_NOTES = 12
# Purely lexical: surfaces sentences where the model itself talked about a disagreement.
CONFLICT_WORDS = re.compile(r"conflict|disagree|discrepan|inconsisten|differ|mismatch|contradict|whereas|"
                            r"סתיר|שונה|לעומת", re.I)
DOC_META_KEYS = ("document_id", "kind", "url", "final_url", "status", "content_type", "doc_type", "title", "pages",
                 "text_chars", "bytes", "truncated", "extraction_path")


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def document_meta(document_id: str, cache=None, documents_dir: Path | None = None,
                  event_meta: dict | None = None) -> dict:
    """Metadata from the shared cache, else the run's exported copy, else the logged `document` event."""
    meta = cache.get(document_id) if cache is not None else None
    if not meta and documents_dir is not None:
        path = Path(documents_dir) / document_id / "meta.json"
        if path.is_file():
            try:
                meta = json.loads(path.read_text("utf-8"))
            except ValueError:
                meta = None
    if not meta:
        meta = dict((event_meta or {}).get(document_id) or {})
        meta["document_id"] = document_id
        meta["_meta_source"] = "events" if event_meta and document_id in event_meta else "unknown"
    return meta


def _identity(payload: dict) -> dict:
    identity = dict(payload.get("identity") or {})
    engine = payload.get("engine_drivetrain")
    out = {"identity": identity}
    if engine:
        out["engine_drivetrain"] = engine
    structure = payload.get("structure") or {}
    if structure:
        out["structure"] = {k: structure.get(k) for k in ("body", "doors", "seats", "country_of_manufacture")
                            if structure.get(k) is not None}
    return out


def _is_electrified(payload: dict) -> bool:
    engine = payload.get("engine_drivetrain") or {}
    text = " ".join(str(engine.get(k) or "") for k in ("propulsion_normalized", "fuel_normalized",
                                                        "propulsion_technology", "fuel")).lower()
    if not text.strip():
        return True
    return any(word in text for word in ("electric", "hybrid", "plug", "חשמל", "היבריד", "bev", "phev"))


def _candidate_facts(evidence: list[dict]) -> tuple[dict, list[str]]:
    facts: dict[str, list[dict]] = {}
    for item in evidence:
        name = str(item.get("field") or "unspecified")
        facts.setdefault(name, []).append({k: item.get(k) for k in ("value", "unit", "market", "variant",
                                                                     "evidence_id", "source_url", "document_id",
                                                                     "note") if item.get(k) is not None})
    multi = sorted(name for name, values in facts.items()
                   if len({json.dumps(v.get("value"), ensure_ascii=False, default=str) for v in values}) > 1)
    return facts, multi


def _conflict_notes(evidence: list[dict], responses: list[dict]) -> list[dict]:
    notes: list[dict] = []
    for item in evidence:
        note = item.get("note") or ""
        if note and CONFLICT_WORDS.search(note):
            notes.append({"source": f"evidence {item.get('evidence_id')}", "field": item.get("field"),
                          "text": _short(note, CONFLICT_NOTE_CHARS)})
    for response in responses:
        for key in ("content", "reasoning_content"):
            text = response.get(key) or ""
            for sentence in re.split(r"(?<=[.!?\n])\s+", text):
                if CONFLICT_WORDS.search(sentence) and len(sentence.strip()) > 20:
                    notes.append({"source": f"model {key} (seq {response.get('seq')})",
                                  "text": _short(sentence, CONFLICT_NOTE_CHARS)})
    unique, seen = [], set()
    for note in notes:
        if note["text"] not in seen:
            seen.add(note["text"])
            unique.append(note)
    return unique[-MAX_CONFLICT_NOTES:]


def _action_line(pair: dict) -> str:
    args = trace.parse_args(pair["arguments"])
    result = pair["result"] if isinstance(pair["result"], dict) else {}
    name, step = pair["name"], pair["step"]
    if pair["result"] is None:
        outcome = "no result logged"
    elif result.get("error"):
        outcome = f"error {result.get('error')}: {_short(result.get('message') or '', 80)}"
    elif name in trace.SEARCH_TOOLS:
        outcome = f"{len(result.get('results') or [])} results" + (" (cached)" if result.get("cache_hit") else "")
    elif name in trace.FETCH_TOOLS:
        kind = result.get("content_type") or ""
        pages = f", {result.get('pages')} pages" if result.get("pages") else ""
        outcome = (f"{result.get('document_id')} status {result.get('status')} {kind}{pages}, "
                   f"{result.get('text_chars')} chars" + (" (cached)" if result.get("cache_hit") else ""))
    elif name == "find_in_document":
        outcome = f"{result.get('hit_count', 0)} hits"
    elif name == "extract_tables":
        outcome = f"{result.get('tables_total', 0)} tables"
    elif name == "store_evidence":
        outcome = str(result.get("evidence_id"))
    else:
        outcome = "ok"
    if name in trace.SEARCH_TOOLS:
        target = repr(args.get("query", ""))
        if args.get("domain") or args.get("domains"):
            target += f" [{args.get('domain') or ','.join(args.get('domains') or [])}]"
    elif name in trace.FETCH_TOOLS:
        target = args.get("url", "")
    elif name == "store_evidence":
        target = f"{args.get('field')}={_short(args.get('value'), 60)}"
    elif name == "find_in_document":
        target = f"{args.get('document_id')} {args.get('query')!r}"
    else:
        target = args.get("document_id") or args.get("key") or ""
    return _short(f"s{step} {name} {target} → {outcome}", ACTION_LINE_CHARS)


def _table_text(table: dict, max_rows: int = 25) -> str:
    rows = table.get("rows") or []
    lines = [" | ".join(str(c) for c in row) for row in rows[:max_rows]]
    head = f"[table {table.get('index')}{' p' + str(table['page']) if table.get('page') else ''}] "
    return head + "\n".join(lines)


def _excerpts(pairs: list[dict]) -> list[dict]:
    """Content the research model already pulled out of documents, most targeted first."""
    priority = {"find_in_document": 0, "extract_tables": 1, "get_structured_data": 2, "extract_html": 3,
                "get_cached_document": 3, "fetch_url": 4, "fetch_pdf": 4, "render_page": 4}
    found = []
    for order, pair in enumerate(pairs):
        result = pair["result"] if isinstance(pair["result"], dict) else None
        if not result or result.get("error") or pair["name"] not in priority:
            continue
        args = trace.parse_args(pair["arguments"])
        doc = result.get("document_id") or args.get("document_id")
        name = pair["name"]
        if name == "find_in_document":
            text = "\n…\n".join(h.get("snippet", "") for h in result.get("hits") or [])
            via = f"find_in_document {args.get('query')!r}"
        elif name == "extract_tables":
            text = "\n\n".join(_table_text(t) for t in result.get("tables") or [])
            via = "extract_tables"
        elif name == "get_structured_data":
            text = json.dumps(result.get("data"), ensure_ascii=False) if result.get("data") is not None else \
                result.get("data_preview") or ""
            via = "get_structured_data"
        elif name in ("extract_html", "get_cached_document"):
            text = result.get("text") or ""
            via = f"{name} offset {result.get('offset', 0)}"
        else:
            text = result.get("text_preview") or ""
            via = f"{name} preview"
        text = (text or "").strip()
        if text:
            found.append({"priority": priority[name], "order": order, "document_id": doc, "step": pair["step"],
                          "via": via, "text": text[:EXCERPT_CHARS]})
    found.sort(key=lambda e: (e["priority"], -e["order"]))
    return found


def _requested(events: list[dict], payload: dict, include_electric: bool) -> list[dict]:
    """The run's requested field specs (from run_started), else the default schema."""
    from .fields import propulsion_of, resolve_requested_fields

    started = trace.first_event(events, "run_started") or {}
    if started.get("requested_field_specs"):
        return list(started["requested_field_specs"])
    if started.get("requested_fields"):
        return [{"name": k, "description": v, "applicable": True} for k, v in started["requested_fields"].items()]
    propulsion = propulsion_of(payload) or (None if include_electric else "conventional")
    return resolve_requested_fields(None, propulsion=propulsion)


def _primary_output(events: list[dict]) -> Any:
    from .field_recovery import primary_output

    output = primary_output(events)
    return output if output is None or _size(output) <= 20000 else {"_truncated": _short(output, 20000)}


def build_research_bundle(events: list[dict], payload: dict | None, *, cache=None,
                          documents_dir: Path | str | None = None, include_level3: bool = True,
                          max_chars: int = 60000, stop_reason: str | None = None,
                          target_market: str | None = None) -> dict:
    payload = payload or {}
    documents_dir = Path(documents_dir) if documents_dir else None
    pairs = trace.tool_pairs(events)
    evidence = trace.evidence_items(events)
    responses = trace.model_responses(events)
    facts, multi = _candidate_facts(evidence)
    event_meta = trace.document_event_meta(events)
    doc_ids = trace.document_ids(events)
    documents = []
    for doc_id in doc_ids:
        meta = document_meta(doc_id, cache, documents_dir, event_meta)
        documents.append({k: meta.get(k) for k in DOC_META_KEYS if meta.get(k) not in (None, "", [], {})})
    include_electric = _is_electrified(payload)
    specs = _requested(events, payload, include_electric)
    requested = {s["name"]: s.get("description") or s["name"] for s in specs if s.get("applicable", True)}
    targets = {"requested_fields": requested, "level3": dict(LEVEL3_TOPICS) if include_level3 else None}
    recovery = trace.field_recovery_summary(events)
    field_states = None
    if recovery and recovery.get("evaluation_final"):
        field_states = {f["field"]: {k: f.get(k) for k in ("state", "info", "evidence_ids", "markets")
                                     if f.get(k) not in (None, [], {})}
                        for f in recovery["evaluation_final"]}
        unresolved = [n for n, f in field_states.items() if f.get("state") not in ("ok", "not_applicable")]
    else:
        with_evidence = {str(item.get("field")) for item in evidence}
        unresolved = [name for name in requested if name not in with_evidence]
    if include_level3:
        with_evidence = {str(item.get("field")) for item in evidence}
        unresolved += [f"level3:{key}" for key in LEVEL3_TOPICS if key not in with_evidence]
    queries = []
    for pair in pairs:
        if pair["name"] in trace.SEARCH_TOOLS:
            q = trace.parse_args(pair["arguments"]).get("query")
            if q and q not in queries:
                queries.append(q)
    notes = [{"seq": r["seq"], "phase": r["phase"], "text": _short(r["content"], MODEL_NOTE_CHARS)}
             for r in responses if (r.get("content") or "").strip()][-MAX_MODEL_NOTES:]
    actions = [_action_line(pair) for pair in pairs]

    markets: dict[str, int] = {}
    for item in evidence:
        market = str(item.get("market") or "not_recorded")
        markets[market] = markets.get(market, 0) + 1
    vehicle = _identity(payload)
    notes_for_variant = variant_notes(record_id_of(payload))
    if notes_for_variant:
        vehicle["operator_variant_notes"] = notes_for_variant
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "vehicle": vehicle,
        "targets": targets,
        "research_summary": {
            "stop_reason": stop_reason,
            "research_model_turns": trace.research_steps(events),
            "tool_calls": len(pairs),
            "searches": len(queries),
            "documents_touched": len(doc_ids),
            "evidence_items": len(evidence),
        },
        "target_market": target_market,
        "field_states": field_states,
        "field_recovery": [{k: a.get(k) for k in ("field", "attempt", "state_before", "state_after", "reply", "error")
                            if a.get(k) is not None} for a in (recovery or {}).get("attempts") or []],
        "primary_output": _primary_output(events),
        "evidence": evidence,
        "evidence_by_market": markets,
        "candidate_facts": facts,
        "fields_with_multiple_stored_values": multi,
        "model_noted_conflicts": _conflict_notes(evidence, responses),
        "documents": documents,
        "search_queries": queries,
        "research_actions": actions[-MAX_ACTIONS:],
        "unresolved_targets": unresolved,
        "model_notes": notes,
        "last_model_content": _short(responses[-1]["content"], 4000)
        if responses and (responses[-1].get("content") or "").strip() else (notes[-1]["text"] if notes else None),
        "document_excerpts": [],
        "truncation": {"actions_dropped": max(0, len(actions) - MAX_ACTIONS)},
    }

    # Fit the core under the cap first (older actions, then model notes), then fill excerpts.
    while _size(bundle) > max_chars and bundle["research_actions"]:
        dropped = max(1, len(bundle["research_actions"]) // 4)
        bundle["research_actions"] = bundle["research_actions"][dropped:]
        bundle["truncation"]["actions_dropped"] += dropped
    while _size(bundle) > max_chars and bundle["model_notes"]:
        bundle["model_notes"] = bundle["model_notes"][1:]
        bundle["truncation"]["model_notes_dropped"] = bundle["truncation"].get("model_notes_dropped", 0) + 1
    if _size(bundle) > max_chars:
        bundle["truncation"]["core_over_budget"] = True

    budget = max_chars - _size(bundle)
    per_doc: dict[str, int] = {}
    seen_text: set[str] = set()
    candidates = _excerpts(pairs)
    for excerpt in candidates:
        key = excerpt["text"][:200]
        doc = excerpt["document_id"] or ""
        if key in seen_text or per_doc.get(doc, 0) + len(excerpt["text"]) > EXCERPTS_PER_DOCUMENT_CHARS:
            continue
        item = {k: excerpt[k] for k in ("document_id", "step", "via", "text")}
        cost = _size(item) + 2
        if cost > budget:
            continue
        budget -= cost
        seen_text.add(key)
        per_doc[doc] = per_doc.get(doc, 0) + len(excerpt["text"])
        bundle["document_excerpts"].append(item)
    bundle["truncation"]["excerpts_available"] = len(candidates)
    bundle["truncation"]["excerpts_included"] = len(bundle["document_excerpts"])
    bundle["bundle_chars"] = _size(bundle)
    return bundle

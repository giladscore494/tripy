"""Model Document Sweep: "use what we already downloaded" before any targeted web recovery.

    deterministic candidates (src/candidate_harvest.py) + current field states + stored evidence
            ↓
    ONE compact packet for ALL applicable requested fields together
            ↓
    the model reviews candidates (market, variant, conflicts), inspects cached documents for what the
    parser missed, and promotes valid facts with store_evidence. It cannot search or fetch: only the
    cached-document tools below are offered, and any other call is refused without being executed.
            ↓
    current_evaluation() decides what is still unresolved (the sweep never sets a state itself)

Pure helpers only (packet, tool filter, after-sweep accounting); the loop lives in src/agent.py.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from .candidate_harvest import candidates_from_events
from .field_recovery import RETRY_STATES, material_key, vehicle_identity
from .fields import normalize_field_name, semantic_notes
from .storage import trace

DOCUMENT_SWEEP_TOOLS = ("inspect_document_for_fields", "find_in_document", "extract_tables", "extract_html",
                        "get_structured_data", "get_cached_document", "store_evidence", "report_field_status")
EXTERNAL_TOOLS = trace.SEARCH_TOOLS + trace.FETCH_TOOLS
MAX_SWEEP_TURNS = 2        # absolute maximum; turn 2 only after a turn-1 cached-document inspection with content
INSPECTION_TOOLS = ("inspect_document_for_fields", "find_in_document", "extract_tables", "extract_html",
                    "get_structured_data", "get_cached_document")
CANDIDATE_KEYS = ("value", "unit", "raw_value", "raw_unit", "document_id", "market_hint", "variant_hint",
                  "variant_hints", "year_hint", "year_hint_differs", "trim_mentioned", "official_domain",
                  "parser_confidence", "extraction_method", "position", "ambiguity", "availability",
                  "price_type_hint", "warranty_type", "converted_from", "range", "page", "occurrences")


def inspection_has_content(name: str, result: dict | None) -> bool:
    """Did a cached-document inspection return something the model still has to read?
    Errors, zero hits, no tables, empty structured data or empty text do not justify a follow-up turn."""
    if name not in INSPECTION_TOOLS or not isinstance(result, dict) or result.get("error"):
        return False
    if name == "find_in_document":
        return bool(result.get("hit_count") or result.get("hits"))
    if name == "inspect_document_for_fields":
        return any((result.get("matches") or {}).values()) or any((result.get("candidates") or {}).values())
    if name == "extract_tables":
        return bool(result.get("tables_total") or result.get("tables"))
    if name == "get_structured_data":
        return any((result.get("found") or {}).values()) or bool(result.get("data") or result.get("data_preview"))
    if name == "get_cached_document" and not result.get("found", True):
        return False
    return bool((result.get("text") or "").strip())


def sweep_tool_specs(all_specs: list[dict]) -> list[dict]:
    return [s for s in all_specs if s["function"]["name"] in DOCUMENT_SWEEP_TOOLS]


def _domain(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    return (host[4:] if host.startswith("www.") else host) or None


def compact_candidate(cand: dict, quote_chars: int = 200) -> dict:
    out = {k: cand.get(k) for k in CANDIDATE_KEYS if cand.get(k) not in (None, "", [], {})}
    if cand.get("quote"):
        out["quote"] = str(cand["quote"])[:quote_chars]
    if cand.get("source_url"):
        out["source"] = _domain(cand["source_url"])
    return out


def compact_evidence(item: dict) -> dict:
    out = {k: item.get(k) for k in ("evidence_id", "field", "value", "unit", "market", "variant", "variant_match",
                                    "binding_level", "source_authority", "document_id") if item.get(k) not in (None, "")}
    if item.get("source_url"):
        out["source"] = _domain(item["source_url"])
    return out


# --- candidate routing (presentation priority only) ------------------------------------------------------------

ROUTING_AUTHORITY = {"official_importer": 3.0, "official_manufacturer": 3.0, "government": 2.5, "official_media": 2.0,
                     "publisher": 0.5, "aggregator": 0.5, "marketplace": 0.0, "unknown": 0.0}
TABLE_METHODS = ("table_row", "structured_data")


def routing_score(cand: dict, profile: dict | None, target_market: str, market_sensitive: bool = False) -> float:
    """Which candidates the model should inspect FIRST. A presentation priority, never a truth confidence: it never
    changes evidence, a field state, a binding, a market or a conflict, and it never counts agreeing sources, votes,
    model confidence or historical yield. Signals: the source document's authority and server-side binding, the
    target market, a same-row / same-cell label-value pairing, parser confidence (label/value pairing), a more
    specific matched alias, an official technical document already retrieved, the target trim named next to it."""
    from .document_binding import LEVELS
    from .field_recovery import is_target_market

    profile = profile or {}
    authority = str(profile.get("source_authority") or "unknown")
    score = ROUTING_AUTHORITY.get(authority, 0.0)
    level = profile.get("binding_level")
    score += 0.5 * (LEVELS.index(level) if level in LEVELS else 0)
    if profile.get("variant_match") == "different":
        score -= 3.0                                   # still shown when nothing better exists, just later
    market = profile.get("market") or cand.get("market_hint")
    if is_target_market(market, target_market):
        score += 1.5 if market_sensitive else 0.5
    if cand.get("extraction_method") in TABLE_METHODS:
        score += 1.0
    score += 2.0 * max(0.0, min(1.0, float(cand.get("parser_confidence") or 0)))
    score += min(len(str(cand.get("matched_alias") or "")), 24) / 24.0 * 0.5
    official_pdf = profile.get("doc_type") == "pdf"
    if authority in ("official_manufacturer", "official_importer", "official_media") and official_pdf:
        score += 0.5
    if cand.get("trim_mentioned"):
        score += 0.5
    if cand.get("year_hint_differs"):
        score -= 1.0
    return round(score, 3)


def route_candidates(cands: list[dict], profiles: dict[str, dict], target_market: str,
                     market_sensitive: bool = False) -> list[dict]:
    """Candidates of one field in presentation order. Distinct values first (the best-routed candidate of each value),
    so two competing technical values are both in front of the model; then the remaining ones. Ties keep the stored
    order. Nothing is dropped here: the caller only limits how many go into the packet."""
    scored = sorted(enumerate(cands), key=lambda ic: (-routing_score(ic[1], profiles.get(str(ic[1].get("document_id"))),
                                                                      target_market, market_sensitive), ic[0]))
    first, rest, seen = [], [], set()
    for _, cand in scored:
        key = material_key(cand.get("value"))
        (rest if key in seen else first).append(cand)
        seen.add(key)
    return first + rest


# --- the sweep packet -------------------------------------------------------------------------------------------

def sweep_packet(*, payload: dict, specs: list[dict], evaluation: list[dict], matrix: dict, events: list[dict],
                 doc_metas: list[dict], target_market: str, max_turns: int, per_field: int = 3,
                 max_chars: int = 40000, fields: list[str] | None = None, routed: dict[str, list[dict]] | None = None,
                 snippets: dict[str, list[dict]] | None = None, profiles: dict[str, dict] | None = None,
                 chunk: dict | None = None, not_found_locally: list[str] | None = None) -> dict:
    """The compact context of one sweep call. Only OPEN fields (settled ones are removed before the sweep); for each,
    its best-routed candidates up to `per_field` (storage keeps every candidate), local snippets for fields without
    candidates, the field's stored evidence and the identity of every cached document. No document is sent whole."""
    states = {e["field"]: e["state"] for e in evaluation}
    open_names = [e["field"] for e in evaluation if e.get("retry_eligible", e["state"] in RETRY_STATES)]
    wanted = [n for n in (fields if fields is not None else open_names) if n in set(open_names)]
    by_name = {s["name"]: s for s in specs if s.get("applicable", True)}
    review = [n for n in wanted if n in by_name]
    pool = routed if routed is not None else matrix["fields"]
    candidates: dict[str, list[dict]] = {}
    hidden: dict[str, int] = {}
    for name in review:
        cands = pool.get(name) or []
        if cands:
            candidates[name] = [compact_candidate(c) for c in cands[:per_field]]
            if len(cands) > per_field:
                hidden[name] = len(cands) - per_field
    profiles = profiles or {}
    packet: dict[str, Any] = {
        "vehicle_identity": vehicle_identity(payload, target_market),
        "turn_budget": max_turns,
        "turn_rule": ("Promote valid candidates in turn 1 and finish. A second turn (if turn_budget allows) is given "
                      "only when turn 1 inspected cached documents and got content back."),
        "requested_fields": {n: by_name[n].get("description") or n for n in review},
        "field_semantics": semantic_notes([by_name[n] for n in review]),
        "current_field_states": {n: states.get(n) for n in review},
        "fields_to_review": review,
        "deterministic_candidates": candidates,
        "candidate_order": "presentation priority (a scheduling order, never a truth ranking)",
        "fields_without_candidates": [n for n in review if not candidates.get(n)],
        "stored_evidence": [compact_evidence(e) for e in trace.evidence_items(events)
                            if normalize_field_name(e.get("field")) in set(review)],
        "cached_documents": [{**{k: m.get(k) for k in ("document_id", "url", "final_url", "title", "doc_type", "pages",
                                                       "text_chars") if m.get(k) not in (None, "")},
                              **{k: v for k, v in (profiles.get(str(m.get("document_id"))) or {}).items()
                                 if k in ("source_authority", "market", "binding_level") and v}}
                             for m in doc_metas],
        "candidate_summary": {k: matrix.get(k) for k in ("candidate_count", "documents", "applicable_fields",
                                                         "candidate_field_coverage_pct")},
    }
    if hidden:
        packet["candidates_not_shown"] = hidden            # still stored; inspect_document_for_fields reaches them
    local = {n: v for n, v in (snippets or {}).items() if n in packet["fields_without_candidates"] and v}
    if local:
        packet["local_snippets"] = local                    # deterministic label locations; not evidence
    absent = [n for n in not_found_locally or [] if n in packet["fields_without_candidates"] and n not in local]
    if absent:
        # the deterministic inspection found none of these fields' dictionary labels in the text of any usable cached
        # document (fields without a dictionary entry are never listed): a plain re-search of the cache is unlikely
        # to help; they go to targeted web recovery. Nothing is implied about the value.
        packet["fields_not_found_locally"] = absent
    if chunk:
        packet["chunk"] = chunk
    # Fit the size budget: drop the lowest-priority candidate of the longest list (never a field's first), then
    # local snippets beyond the first per field.
    while max_chars and packet_chars(packet) > max_chars:      # 0 = no limit (as in within())
        longest = max(candidates, key=lambda n: len(candidates[n]), default=None)
        if longest is not None and len(candidates[longest]) > 1:
            candidates[longest] = candidates[longest][:-1]
            hidden[longest] = hidden.get(longest, 0) + 1
            packet["candidates_not_shown"] = hidden
            continue
        many = max(local, key=lambda n: len(local[n]), default=None)
        if many is not None and len(local[many]) > 1:
            local[many] = local[many][:-1]
            continue
        break
    return packet


def packet_chars(packet: dict) -> int:
    return len(json.dumps(packet, ensure_ascii=False, default=str))


def packet_size(packet: dict) -> dict:
    """{chars, fields, candidates} of a sweep packet (what the adaptive limits are checked against)."""
    return {"chars": packet_chars(packet), "fields": len(packet["fields_to_review"]),
            "candidates": sum(len(v) for v in packet["deterministic_candidates"].values())}


def within(size: dict, limits: dict) -> bool:
    return all(not limits.get(k) or size[k] <= limits[k] for k in ("chars", "fields", "candidates"))


def plan_chunks(fields: list[str], specs: list[dict], build, limits: dict) -> list[dict]:
    """Deterministic sweep chunks. One chunk when the whole packet fits the limits (one compact sweep beats several
    tiny calls). Otherwise the schema's recovery clusters, in schema order, are packed greedily into as few chunks as
    fit; a cluster that alone exceeds the limits is split by fields, in schema order. Every field lands in exactly one
    chunk. `build(fields)` returns the packet for a field subset."""
    from .tail_planner import cluster_of

    if not fields:
        return []
    if within(packet_size(build(fields)), limits):
        names = set(fields)
        return [{"clusters": sorted({cluster_of(s) for s in specs if s["name"] in names}), "fields": list(fields)}]
    by_name = {s["name"]: s for s in specs}
    order: list[str] = []
    members: dict[str, list[str]] = {}
    for name in fields:
        cluster = cluster_of(by_name.get(name) or {"name": name})
        if cluster not in members:
            order.append(cluster)
            members[cluster] = []
        members[cluster].append(name)
    units: list[tuple[list[str], list[str]]] = []          # (clusters, fields) that fit alone
    for cluster in order:
        names = members[cluster]
        if within(packet_size(build(names)), limits):
            units.append(([cluster], names))
            continue
        part: list[str] = []
        for name in names:                                  # an oversized cluster: split by fields
            if part and not within(packet_size(build(part + [name])), limits):
                units.append(([cluster], part))
                part = []
            part.append(name)
        if part:
            units.append(([cluster], part))
    chunks: list[dict] = []
    for clusters, names in units:
        if chunks and within(packet_size(build(chunks[-1]["fields"] + names)), limits):
            chunks[-1]["fields"] += names
            chunks[-1]["clusters"] += [c for c in clusters if c not in chunks[-1]["clusters"]]
        else:
            chunks.append({"clusters": list(clusters), "fields": list(names)})
    return chunks


def promoted_or_missed(sweep_evidence: list[dict], events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split evidence stored by the sweep into (promoted deterministic candidates, facts the deterministic
    harvest missed: no candidate for that field and value from that document)."""
    by_doc: dict[tuple[str, str], set[str]] = {}
    for cand in candidates_from_events(events):
        key = (normalize_field_name(cand.get("field")), str(cand.get("document_id") or cand.get("source_url")))
        by_doc.setdefault(key, set()).add(material_key(cand.get("value")))
    url_to_doc = {}
    for cand in candidates_from_events(events):
        if cand.get("source_url") and cand.get("document_id"):
            url_to_doc[cand["source_url"]] = cand["document_id"]
    promoted, missed = [], []
    for item in sweep_evidence:
        doc = item.get("document_id") or url_to_doc.get(item.get("source_url"))
        key = (normalize_field_name(item.get("field")), str(doc or item.get("source_url")))
        values = by_doc.get(key)
        if values is not None and material_key(item.get("value")) in values:
            promoted.append(item)
        else:
            missed.append(item)
    return promoted, missed


def sweep_summary(*, before: list[dict], after: list[dict], sweep_evidence: list[dict], promoted: list[dict],
                  missed: list[dict], presented: int, model_calls: int, turns: int, blocked: int,
                  external_calls: int, reply: Any, skipped: str | None = None) -> dict:
    eligible_before = {e["field"] for e in before if e["retry_eligible"]}
    eligible_after = {e["field"] for e in after if e["retry_eligible"]}
    resolved = sorted(eligible_before - eligible_after)
    return {
        "skipped": skipped,
        "model_calls": model_calls,
        "turns": turns,
        "fields_unresolved_before": len(eligible_before),
        "fields_unresolved_after": len(eligible_after),
        "fields_resolved": resolved,
        "unique_fields_resolved": len(resolved),
        "evidence_stored": len(sweep_evidence),
        "fields_promoted_to_evidence": sorted({normalize_field_name(e.get("field")) for e in sweep_evidence}),
        "evidence_promoted_from_candidates": len(promoted),
        "deterministic_misses_found": len(missed),
        "candidates_presented": presented,
        # share of presented candidates the model promoted to evidence; a review rate, never accuracy
        "candidate_precision_reviewed": round(100 * len(promoted) / presented, 1) if presented else None,
        "tool_calls_blocked": blocked,
        "external_calls": external_calls,
        "reply": reply,
    }

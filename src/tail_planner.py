"""Deterministic tail planning for clustered field recovery (no model call).

After the document sweep, `current_evaluation()` says which requested fields are still retry-eligible. This
module decides HOW to spend the remaining recovery budget on them; it never decides what is true:

    triage            a scheduling category per remaining field (never a field state):
                        candidate_rich_local  harvested candidates no model has reviewed yet are in the cache
                        conflicting           target-scope values differ (see src/conflict_normalizer.py)
                        foreign_only          only other-market evidence (and the field is not portable)
                        low_yield             an earlier cluster attempt on it produced no novelty
                        policy_blocked        the schema gives it no recovery attempts, or its own target evidence
                                              contradicts a schema rule (a person must look)
                        true_missing          nothing usable locally
    recovery_cluster  the schema's grouping of fields one kind of source answers together (technical_spec,
                      performance, ...). The recovery unit is a cluster, not a single field.
    local_first       a cluster with unreviewed local material gets a cached-documents-only pass first; with none,
                      that pointless local pass is skipped and web research starts directly.
    source_yield_score  ranks cached documents for a cluster (fields with candidates, binding level, authority,
                      target market, tables, evidence already yielded, identity, parser confidence). It is a
                      scheduling metric, never a truth confidence.
    search_hints      already-paid search results (title / snippet / URL) ranked for fetching. Routing metadata:
                      never evidence, never promoted.
    novelty           what a recovery turn really added (new official document, new candidate for an open field,
                      new admitted evidence, a binding or state improvement, a narrower conflict). Errors, repeated
                      queries, cached re-reads, rejected stores and model commentary are not novelty.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .document_binding import LEVELS, bind
from .field_recovery import (NON_TARGET_VARIANTS, attempted_operations, material_key,
                             max_attempts_for, related_searches, vehicle_identity)
from .fields import normalize_field_name, public_spec, semantic_notes
from .source_authority import OFFICIAL_CLASSES, classify_source, host_of
from .storage import trace

PLANNER_VERSION = "tail-planner-v1"
TRIAGE_STATES = ("candidate_rich_local", "true_missing", "foreign_only", "conflicting", "policy_blocked",
                 "low_yield")
DEFAULT_CLUSTER = "other"
LOCAL_TOOLS = ("find_in_document", "extract_tables", "extract_html", "get_structured_data", "get_cached_document",
               "store_evidence", "report_field_status")
# field state -> rank: a higher rank after a turn is a state improvement (scheduling only)
STATE_RANK = {"missing": 0, "unresolved": 0, "weak_provenance": 0, "variant_not_exact": 1,
              "foreign_market_only": 2, "conflicting": 3, "ok": 5, "not_applicable": 5}
AUTHORITY_WEIGHT = {"government": 3, "official_manufacturer": 3, "official_importer": 3, "official_media": 2,
                    "aggregator": 1, "publisher": 0.5, "marketplace": 0, "unknown": 0}


def cluster_of(spec: dict) -> str:
    return str(spec.get("recovery_cluster") or spec.get("group") or DEFAULT_CLUSTER)


def _level(value: Any) -> int:
    return LEVELS.index(value) if value in LEVELS else -1


# --- candidates the models have / have not seen ------------------------------------------------------

def candidate_key(cand: dict) -> str:
    return "|".join((normalize_field_name(cand.get("field")), str(cand.get("document_id") or cand.get("source_url")),
                     material_key(cand.get("value"))))


def presented_keys(events: Iterable[dict]) -> set[str]:
    """Candidates already put in front of a model (document sweep or an earlier cluster pass)."""
    out: set[str] = set()
    for event in events:
        if event.get("kind") in ("document_sweep_started", "cluster_recovery_started"):
            out.update(event.get("presented_candidate_keys") or [])
    return out


def rejected_keys(events: Iterable[dict]) -> set[str]:
    """(field, document, value) a store_evidence request already failed admission for."""
    return {candidate_key(e) for e in events if e.get("kind") == "evidence_rejected" and e.get("field")}


def stored_keys(events: Iterable[dict]) -> set[str]:
    return {candidate_key(item) for item in trace.evidence_items(events)}


def fresh_candidates(matrix: dict, events: list[dict], fields: Iterable[str]) -> dict[str, list[dict]]:
    """Per field: harvested candidates no model has seen, not already stored and not already rejected."""
    seen = presented_keys(events) | rejected_keys(events) | stored_keys(events)
    out = {}
    for name in fields:
        cands = [c for c in matrix["fields"].get(name) or [] if candidate_key(c) not in seen]
        if cands:
            out[name] = cands
    return out


# --- triage --------------------------------------------------------------------------------------------

def triage(evaluation: list[dict], specs: list[dict], matrix: dict, events: list[dict], default_attempts: int,
           low_yield: Iterable[str] = ()) -> dict[str, dict]:
    """{field: {triage, state, cluster, candidates, fresh_candidates, max_attempts}} for retry-eligible fields."""
    by_name = {s["name"]: s for s in specs}
    eligible = [e for e in evaluation if e["retry_eligible"]]
    fresh = fresh_candidates(matrix, events, [e["field"] for e in eligible])
    low = set(low_yield)
    out = {}
    for entry in eligible:
        name = entry["field"]
        spec = by_name.get(name) or {"name": name}
        attempts = max_attempts_for(spec, default_attempts)
        info = entry.get("info") or []
        if attempts == 0 or any(str(i).startswith("schema_rule_conflict") for i in info):
            category = "policy_blocked"
        elif entry["state"] == "conflicting":
            category = "conflicting"
        elif name in low:
            category = "low_yield"
        elif fresh.get(name):
            category = "candidate_rich_local"
        elif entry["state"] == "foreign_market_only":
            category = "foreign_only"
        else:
            category = "true_missing"
        out[name] = {"triage": category, "state": entry["state"], "cluster": cluster_of(spec),
                     "candidates": len(matrix["fields"].get(name) or []), "fresh_candidates": len(fresh.get(name) or []),
                     "max_attempts": attempts}
    return out


def plan_clusters(triaged: dict[str, dict], specs: list[dict]) -> list[dict]:
    """Clusters in schema order with their recoverable fields (policy_blocked fields are never scheduled)."""
    order: list[str] = []
    members: dict[str, list[str]] = {}
    for spec in specs:
        item = triaged.get(spec["name"])
        if not item or item["triage"] == "policy_blocked":
            continue
        cluster = item["cluster"]
        if cluster not in members:
            order.append(cluster)
            members[cluster] = []
        members[cluster].append(spec["name"])
    return [{"cluster": c, "fields": members[c],
             "local_material": any(triaged[f]["triage"] == "candidate_rich_local" for f in members[c])}
            for c in order]


# --- documents -------------------------------------------------------------------------------------------

def document_profile_for(adm, cache, document_id: str) -> dict:
    """Binding level (document-level), authority and market of a cached document for this run's target."""
    if adm is None:
        return {}
    try:
        material = adm.material(cache, document_id, None)
    except Exception:   # a broken cache entry never blocks planning
        material = None
    if material is None:
        return {}
    binding = bind(adm.identity, material.profile["statuses"], market=material.market)
    return {"binding_level": binding["binding_level"], "variant_match": binding["variant_match"],
            "source_authority": material.authority.get("source_authority"), "market": material.market,
            "identity_named": material.profile.get("zone_statuses", {}).get("model") == "match"}


def source_yield_score(*, unresolved_with_candidates: int, binding_level: str | None, source_authority: str | None,
                       target_market: bool, has_tables: bool, evidence_yielded: int, identity_named: bool,
                       parser_quality: float) -> float:
    """Deterministic document ranking for recovery scheduling. NOT a truth confidence: a high score says this
    document is worth reading first for the open fields, nothing about whether its values are right."""
    score = 3.0 * unresolved_with_candidates
    score += max(0, _level(binding_level))                       # 0 (unknown) .. 5 (exact market trim)
    score += AUTHORITY_WEIGHT.get(str(source_authority), 0)
    score += 1.0 if target_market else 0.0
    score += 1.0 if has_tables else 0.0
    score += 0.5 * min(evidence_yielded, 6)
    score += 1.0 if identity_named else 0.0
    score += round(max(0.0, min(1.0, parser_quality)), 2)
    return round(score, 2)


def rank_documents(*, doc_metas: list[dict], matrix: dict, fields: list[str], events: list[dict], adm, cache,
                   target_market: str, limit: int = 8) -> list[dict]:
    """The run's cached documents ranked by source_yield_score for these fields."""
    from .field_recovery import is_target_market

    wanted = set(fields)
    by_doc: dict[str, list[dict]] = {}
    for name in fields:
        for cand in matrix["fields"].get(name) or []:
            by_doc.setdefault(str(cand.get("document_id")), []).append(cand)
    yielded: dict[str, int] = {}
    for item in trace.evidence_items(events):
        yielded[str(item.get("document_id"))] = yielded.get(str(item.get("document_id")), 0) + 1
    tables = {str(c.get("document_id")) for name in matrix["fields"] for c in matrix["fields"][name]
              if "table" in str(c.get("extraction_method") or "")}
    ranked = []
    for meta in doc_metas:
        doc_id = str(meta.get("document_id"))
        cands = by_doc.get(doc_id, [])
        profile = document_profile_for(adm, cache, doc_id)
        quality = (sum(float(c.get("parser_confidence") or 0) for c in cands) / len(cands)) if cands else 0.0
        score = source_yield_score(
            unresolved_with_candidates=len({normalize_field_name(c.get("field")) for c in cands} & wanted),
            binding_level=profile.get("binding_level"), source_authority=profile.get("source_authority"),
            target_market=is_target_market(profile.get("market"), target_market), has_tables=doc_id in tables,
            evidence_yielded=yielded.get(doc_id, 0), identity_named=bool(profile.get("identity_named")),
            parser_quality=quality)
        ranked.append({"document_id": doc_id, "url": meta.get("final_url") or meta.get("url"),
                       "title": meta.get("title"), "source_yield_score": score,
                       "candidate_fields": sorted({normalize_field_name(c.get("field")) for c in cands} & wanted),
                       **{k: profile.get(k) for k in ("binding_level", "source_authority", "market")
                          if profile.get(k)}})
    ranked.sort(key=lambda d: -d["source_yield_score"])
    return ranked[:limit]


# --- search hints ----------------------------------------------------------------------------------------

def _words(text: Any) -> set[str]:
    return {w for w in re.split(r"[^\w.]+", str(text or "").lower()) if len(w) >= 2}


def search_hints(*, events: list[dict], specs: list[dict], identity: dict, manufacturer: str | None,
                 opened_urls: Iterable[str], limit: int = 6) -> list[dict]:
    """Results of searches the run already paid for, not yet fetched, ranked for fetching by how well title,
    snippet and URL match the target identity and the cluster's field labels. Routing metadata only."""
    opened = {str(u).split("#")[0].rstrip("/") for u in opened_urls if u}
    ident = _words(" ".join(str(identity.get(k) or "") for k in ("manufacturer", "model", "year", "government_trim",
                                                                    "model_code")))
    labels = set()
    for spec in specs:
        for alias in (spec.get("aliases_en") or [])[:3] + (spec.get("aliases_he") or [])[:3]:
            labels |= _words(alias)
    labels -= {"of", "the", "and"}
    seen: set[str] = set()
    hints = []
    for pair in trace.tool_pairs(events):
        if pair["name"] not in trace.SEARCH_TOOLS or not isinstance(pair.get("result"), dict):
            continue
        for result in pair["result"].get("results") or []:
            url = str(result.get("url") or result.get("link") or "").split("#")[0]
            if not url or url.rstrip("/") in opened or url in seen:
                continue
            seen.add(url)
            text = " ".join(str(result.get(k) or "") for k in ("title", "snippet", "content", "description"))
            words = _words(text) | _words(url.replace("/", " ").replace("-", " "))
            authority = classify_source(url, manufacturer).get("source_authority")
            score = (2.0 * len(ident & words) + 1.5 * len(labels & words)
                     + AUTHORITY_WEIGHT.get(str(authority), 0))
            hints.append({"url": url, "title": str(result.get("title") or "")[:140],
                          "snippet": str(result.get("snippet") or result.get("content") or "")[:220],
                          "domain": host_of(url), "source_authority": authority, "hint_score": round(score, 2),
                          "from_query": trace.parse_args(pair["arguments"]).get("query")})
    hints.sort(key=lambda h: -h["hint_score"])
    return hints[:limit]


# --- novelty -----------------------------------------------------------------------------------------------

def snapshot(*, events: list[dict], evaluation: list[dict], documents: list[str], open_fields: Iterable[str],
             matrix: dict) -> dict:
    """What a turn could change: documents, candidates for open fields, admitted evidence, best binding,
    field states and conflict sizes."""
    open_fields = set(open_fields)
    best: dict[str, int] = {}
    for item in trace.evidence_items(events):
        name = normalize_field_name(item.get("field"))
        if str(item.get("variant_match") or "").lower() in NON_TARGET_VARIANTS:
            continue
        best[name] = max(best.get(name, -1), _level(item.get("binding_level")))
    return {
        "documents": list(documents),
        "candidates": {candidate_key(c) for n in open_fields for c in matrix["fields"].get(n) or []},
        "evidence": {str(i.get("evidence_id")) for i in trace.evidence_items(events)},
        "best_binding": best,
        "states": {e["field"]: e["state"] for e in evaluation},
        "conflict_sizes": {e["field"]: len({str(x) for x in e.get("conflict_evidence_ids") or []})
                           for e in evaluation if e["state"] == "conflicting"},
    }


def novelty(before: dict, after: dict, adm=None, cache=None) -> list[str]:
    """Reasons a turn produced real novelty (empty list: none)."""
    reasons = []
    new_docs = [d for d in after["documents"] if d not in set(before["documents"])]
    official = [d for d in new_docs
                if document_profile_for(adm, cache, d).get("source_authority") in OFFICIAL_CLASSES]
    if official:
        reasons.append(f"new_authoritative_document:{len(official)}")
    new_cands = after["candidates"] - before["candidates"]
    if new_cands:
        reasons.append(f"new_usable_candidate:{len(new_cands)}")
    new_ev = after["evidence"] - before["evidence"]
    if new_ev:
        reasons.append(f"new_accepted_evidence:{len(new_ev)}")
    better = [f for f, lvl in after["best_binding"].items() if lvl > before["best_binding"].get(f, -1)
              and f in before["states"]]
    if better:
        reasons.append(f"binding_improvement:{len(better)}")
    improved = [f for f, s in after["states"].items()
                if STATE_RANK.get(s, 0) > STATE_RANK.get(before["states"].get(f), 0)]
    if improved:
        reasons.append(f"field_state_improvement:{len(improved)}")
    narrowed = [f for f, n in before["conflict_sizes"].items()
                if after["states"].get(f) == "conflicting" and after["conflict_sizes"].get(f, n) < n]
    if narrowed:
        reasons.append(f"conflict_narrowing:{len(narrowed)}")
    return reasons


# --- the cluster packet --------------------------------------------------------------------------------------

def cluster_packet(*, cluster: str, fields: list[str], specs: list[dict], evaluation: list[dict],
                   triaged: dict[str, dict], events: list[dict], payload: dict, target_market: str,
                   matrix: dict, ranked_documents: list[dict], hints: list[dict], mode: str, attempt: int,
                   max_attempts: int, turn_budget: dict, search_budget: int | None, previous_attempts: list[dict],
                   other_open_fields: list[str], conflicts: dict[str, dict], candidates_per_field: int,
                   operator_notes: dict | None = None) -> dict:
    """The compact context of ONE cluster attempt: only this cluster's open fields, their candidates, the best
    cached documents for them, already-paid search hints, compact conflict packets and the operational memory.
    Never the full research bundle."""
    from .document_sweep import compact_candidate, compact_evidence

    by_name = {s["name"]: s for s in specs}
    states = {e["field"]: e for e in evaluation}
    keys = ("description", "unit", "normalized_unit", "semantic_definition", "value_type", "binding_requirement",
            "market_sensitivity", "portability_scope")
    open_specs = [by_name[f] for f in fields]
    queries: list[str] = []
    for spec in open_specs:
        for q in related_searches(events, spec, limit=6):
            if q not in queries:
                queries.append(q)
    tokens = set()
    for spec in open_specs:
        tokens |= _words(spec["name"].replace("_", " ")) | _words(spec.get("description"))
    operations = [op for op in attempted_operations(events, None, limit=60)
                  if op.get("tool") not in trace.SEARCH_TOOLS or tokens & _words(op.get("query"))][-20:]
    packet = {
        "vehicle_identity": vehicle_identity(payload, target_market),
        "cluster": cluster,
        "mode": mode,
        "fields": [{"field": f, **{k: by_name[f].get(k) for k in keys if by_name[f].get(k) not in (None, "", [])},
                    "state": states[f]["state"], "triage": triaged.get(f, {}).get("triage"),
                    "info": states[f].get("info") or []} for f in fields],
        "field_semantics": semantic_notes(open_specs),
        "deterministic_candidates": {f: [compact_candidate(c) for c in (matrix["fields"].get(f) or [])
                                         [:candidates_per_field]] for f in fields if matrix["fields"].get(f)},
        "ranked_documents": ranked_documents,
        "existing_evidence": [compact_evidence(e) for e in trace.evidence_items(events)
                              if normalize_field_name(e.get("field")) in set(fields)],
        "conflicts": conflicts,
        "previous_queries": queries[-10:],
        "already_attempted_operations": operations,
        "previous_attempts": previous_attempts,
        "other_open_fields": other_open_fields,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "turn_budget": turn_budget,
    }
    if mode == "web":
        packet["search_hints"] = hints
        packet["search_budget_provider_calls"] = search_budget
    if operator_notes:
        packet["operator_variant_notes"] = operator_notes
    return packet


def cluster_requested_specs(specs: list[dict], fields: list[str]) -> list[dict]:
    names = set(fields)
    return [public_spec(s) for s in specs if s["name"] in names]


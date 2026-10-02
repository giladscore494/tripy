"""Primary research as SOURCE ACQUISITION: deterministic progress tracking, stopping and source priority.

Primary research exists to obtain a compact, high-value source set (official importer / manufacturer spec pages,
brochures and technical PDFs, target-market commercial pages). Every fetched document is harvested for ALL fields by
code (src/candidate_harvest.py) and inspected locally (src/document_inspection.py), so the research model does not
need to Ctrl+F every field through documents it already has. This module decides, with no model call, when that
acquisition has stopped making progress:

    acquisition artifact   what a research turn really acquired:
                             new_usable_document       a newly retrieved page/PDF with content (2xx), not a re-read,
                                                       not a document server-side bound to ANOTHER variant
                             new_official_source       an official (importer / manufacturer / media / government) URL
                                                       not seen before, retrieved or returned by a search
                             new_target_market_document a new usable document of the target market
                             new_candidate             a harvested candidate for an open applicable field, from a
                                                       usable document not bound to another variant
                             new_admitted_evidence     evidence admitted for the target (not different / unbound)
                             binding_improvement       a field's best admitted binding got more exact
                           NOT artifacts: re-reading a cached document, a repeated search, a 403/404/429 or failed
                           fetch, commentary, rejected evidence, candidates of another variant.
    no-artifact stop       PRIMARY_RESEARCH_NO_ARTIFACT_STOP consecutive turns without any artifact end research.
    acquisition sufficiency  optional: at least PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS useful documents AND at least
                           PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD % of the applicable fields covered (a
                           candidate from a useful document, or already settled). Disabled when either is 0.
    source priority        where to try first (importer, manufacturer, official documents, media, publisher,
                           aggregator, marketplace/community); for commercial fields target-market sources first.

All of it is SCHEDULING. Nothing here creates evidence, admits or rejects anything, changes a field state, a binding,
a market or a conflict; current_evaluation() remains the only field-state authority.
"""

from __future__ import annotations

from typing import Any, Iterable

from .source_authority import OFFICIAL_CLASSES, classify_source, url_market
from .storage import trace
from .tail_planner import _level, candidate_key, document_profile_for, usable_document

STOP_REASONS = ("model_finished", "max_turns", "no_new_artifact", "acquisition_sufficient", "no_new_research",
                "user_cancelled", "error")
# run stop_reason -> primary_research_stop_reason
STOP_REASON_MAP = {"model_finished": "model_finished", "max_steps": "max_turns", "no_new_artifact": "no_new_artifact",
                   "acquisition_sufficient": "acquisition_sufficient", "no_new_research": "no_new_research",
                   "user_cancelled": "user_cancelled", "api_failure": "error", "research_exception": "error"}

# --- source priority (scheduling only) ---------------------------------------------------------------------------

TECHNICAL_PRIORITY = {"official_importer": 1, "official_manufacturer": 2, "government": 3, "official_media": 4,
                      "publisher": 5, "aggregator": 6, "marketplace": 7, "unknown": 7}


def source_priority(url: str | None, manufacturer: str | None, target_market: str = "IL") -> dict:
    """Deterministic acquisition order of a URL: 1 = try first. `technical` for specification fields (official importer,
    official manufacturer, official documents (government / an official PDF), official media, publisher, aggregator,
    marketplace / community), `commercial` for market-bound fields (price, licence fee, trim, warranty), where a
    target-market source outranks a foreign manufacturer page. Never an allowlist and never an admission rule: a
    lower-priority source stays fully usable."""
    from .field_recovery import is_target_market

    authority = classify_source(url, manufacturer).get("source_authority") or "unknown"
    market, _ = url_market(url)
    technical = TECHNICAL_PRIORITY.get(authority, 7)
    is_pdf = str(url or "").lower().split("?")[0].endswith(".pdf")
    if authority in ("official_manufacturer", "official_media") and is_pdf:
        technical = 3                         # an official technical PDF / document
    local = is_target_market(market, target_market)
    if authority == "official_importer":
        commercial = 1
    elif local and authority in OFFICIAL_CLASSES:
        commercial = 2
    elif local:
        commercial = 3
    elif authority in OFFICIAL_CLASSES:
        commercial = 4
    else:
        commercial = 5
    return {"source_class": authority, "market": market, "priority_technical": technical,
            "priority_commercial": commercial}


def annotate_search_result(result: Any, manufacturer: str | None, target_market: str = "IL") -> Any:
    """Add the acquisition priority to every result of a search (order unchanged; nothing removed)."""
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return result
    # copies: a result list may be shared with the search cache, which must keep the provider's results untouched
    result["results"] = [{**item, "acquisition_priority": source_priority(item["url"], manufacturer, target_market)}
                         if isinstance(item, dict) and item.get("url") else item for item in result["results"]]
    return result


# --- acquisition progress ----------------------------------------------------------------------------------------

def _url(cache, doc_id: str) -> str:
    meta = (cache.get(str(doc_id)) if cache is not None else None) or {}
    return str(meta.get("final_url") or meta.get("url") or doc_id).split("#")[0].rstrip("/")


def _search_urls(events: Iterable[dict]) -> set[str]:
    out = set()
    for pair in trace.tool_pairs(events):
        if pair["name"] in trace.SEARCH_TOOLS and isinstance(pair.get("result"), dict):
            for item in pair["result"].get("results") or []:
                if isinstance(item, dict) and item.get("url"):
                    out.add(str(item["url"]).split("#")[0].rstrip("/"))
    return out


def snapshot(*, events: list[dict], documents: Iterable[str], evaluation: list[dict], matrix: dict, adm, cache,
             target_market: str, manufacturer: str | None) -> dict:
    """What the run has acquired so far (for one before/after comparison around a research turn)."""
    from .field_recovery import is_target_market

    open_fields = {e["field"] for e in evaluation if e["retry_eligible"]}
    applicable = set(matrix["fields"])
    profiles: dict[str, dict] = {}
    useful_docs, other_variant, target_docs = [], set(), []
    for doc in documents:
        if not usable_document(cache, doc):
            continue
        profile = profiles[doc] = document_profile_for(adm, cache, doc)
        if profile.get("variant_match") == "different":
            other_variant.add(doc)
            continue
        useful_docs.append(doc)
        if is_target_market(profile.get("market"), target_market):
            target_docs.append(doc)
    good = set(useful_docs)
    candidates = {candidate_key(c) for name in open_fields for c in matrix["fields"].get(name) or []
                  if str(c.get("document_id")) in good}
    covered = {name for name in applicable if any(str(c.get("document_id")) in good
                                                  for c in matrix["fields"].get(name) or [])}
    # an evidence-backed ok counts as covered; a self-reported not_applicable never ends acquisition early
    settled = {e["field"] for e in evaluation if e["state"] == "ok" and e["field"] in applicable}
    official = {_url(cache, d) for d in useful_docs if profiles[d].get("source_authority") in OFFICIAL_CLASSES}
    official |= {u for u in _search_urls(events)
                 if classify_source(u, manufacturer).get("source_authority") in OFFICIAL_CLASSES}
    evidence, best = set(), {}
    for item in trace.evidence_items(events):
        if str(item.get("variant_match") or "").lower() in ("different", "unbound"):
            continue                      # bound to another variant / unbound: the evaluator ignores it too
        evidence.add(str(item.get("evidence_id")))
        name = str(item.get("field"))
        best[name] = max(best.get(name, -1), _level(item.get("binding_level")))
    return {"useful_documents": useful_docs, "useful_urls": {_url(cache, d) for d in useful_docs},
            "target_market_documents": target_docs, "target_market_urls": {_url(cache, d) for d in target_docs},
            "other_variant_documents": sorted(other_variant),
            "official_sources": official, "candidates": candidates, "evidence": evidence, "best_binding": best,
            "covered_fields": covered | settled, "applicable_fields": len(applicable),
            "fields_with_candidates": len(covered)}


def artifacts(before: dict, after: dict) -> list[str]:
    """Meaningful acquisition artifacts of one turn (empty: none)."""
    reasons = []
    fresh = after["useful_urls"] - before["useful_urls"]      # by URL: another fetch kind of a known page is no news
    if fresh:
        reasons.append(f"new_usable_document:{len(fresh)}")
    target = after["target_market_urls"] - before["target_market_urls"]
    if target:
        reasons.append(f"new_target_market_document:{len(target)}")
    official = after["official_sources"] - before["official_sources"]
    if official:
        reasons.append(f"new_official_source:{len(official)}")
    cands = after["candidates"] - before["candidates"]
    if cands:
        reasons.append(f"new_candidate:{len(cands)}")
    evidence = after["evidence"] - before["evidence"]
    if evidence:
        reasons.append(f"new_admitted_evidence:{len(evidence)}")
    better = [f for f, lvl in after["best_binding"].items() if lvl > before["best_binding"].get(f, -1)
              and f in before["best_binding"]]
    if better:
        reasons.append(f"binding_improvement:{len(better)}")
    return reasons


def coverage_pct(snap: dict) -> float:
    total = snap["applicable_fields"]
    return round(100.0 * len(snap["covered_fields"]) / total, 1) if total else 0.0


def threshold_pct(value: Any) -> float:
    """A coverage threshold given as a fraction (0.6) or a percentage (60). 0 / empty = disabled."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number * 100 if 0 < number <= 1 else number


def sufficient(snap: dict, min_documents: int, threshold: Any) -> bool:
    """Optional acquisition-sufficiency transition (scheduling only; disabled when either setting is 0)."""
    pct = threshold_pct(threshold)
    if not min_documents or not pct:
        return False
    return len(snap["useful_documents"]) >= int(min_documents) and coverage_pct(snap) >= pct


class AcquisitionTracker:
    """Per-run bookkeeping of primary research acquisition (one before/after snapshot per research turn).
    Logs `primary_research_turn` events; decides nothing about any field."""

    def __init__(self, *, run_log, ctx, specs: list[dict], vehicle: dict | None, cache, config):
        self.run_log, self.ctx, self.specs, self.vehicle, self.cache, self.config = run_log, ctx, specs, vehicle, \
            cache, config
        self.market = config.target_market
        self.manufacturer = getattr(ctx.admission, "manufacturer", None) or (vehicle or {}).get("manufacturer")
        self.turns: list[dict] = []
        self.streak = 0
        self.max_streak = 0
        try:
            self.start = self.last = self.take()
        except Exception as exc:     # never costs the run; an empty baseline only makes turn 1 look productive
            run_log.event("primary_research_turn_unmeasured", turn=0, error=f"{type(exc).__name__}: {exc}"[:300])
            self.start = self.last = {"useful_documents": [], "useful_urls": set(), "target_market_documents": [],
                                      "target_market_urls": set(), "other_variant_documents": [],
                                      "official_sources": set(), "candidates": set(), "evidence": set(),
                                      "best_binding": {}, "covered_fields": set(), "applicable_fields": 0,
                                      "fields_with_candidates": 0}

    def take(self) -> dict:
        from .candidate_harvest import candidate_matrix
        from .field_recovery import current_evaluation
        from .storage.run_log import read_events

        events = read_events(self.run_log.events_path)
        return snapshot(events=events, documents=list(self.ctx.documents_opened),
                        evaluation=current_evaluation(events, self.specs, self.market),
                        matrix=candidate_matrix(events, self.specs, self.vehicle), adm=self.ctx.admission,
                        cache=self.cache, target_market=self.market, manufacturer=self.manufacturer)

    def after_turn(self, step: int) -> list[str]:
        try:
            now = self.take()
        except Exception as exc:     # a scheduling snapshot must never cost the run: treat the turn as progress
            self.run_log.event("primary_research_turn_unmeasured", turn=step, error=f"{type(exc).__name__}: {exc}"[:300])
            self.streak = 0
            return ["unmeasured"]
        found = artifacts(self.last, now)
        self.last = now
        self.streak = 0 if found else self.streak + 1
        self.max_streak = max(self.max_streak, self.streak)
        row = {"turn": step, "artifacts": found, "no_artifact_streak": self.streak,
               "useful_documents": len(now["useful_documents"]),
               "fields_with_candidates": now["fields_with_candidates"],
               "candidate_field_coverage_pct": coverage_pct(now)}
        self.turns.append(row)
        self.run_log.event("primary_research_turn", **row)
        return found

    def sufficient(self) -> bool:
        return sufficient(self.last, self.config.primary_research_min_useful_documents,
                          self.config.primary_research_candidate_field_coverage_threshold)

    def summary(self, *, stop_reason: str | None, turns: int, model_calls: int, research_s: float | None) -> dict:
        end = self.last
        return {
            "turns": turns,
            "model_calls": model_calls,
            "max_turns": self.config.max_steps,
            "stop_reason": STOP_REASON_MAP.get(stop_reason or "", stop_reason),
            "run_stop_reason": stop_reason,
            "documents_added": len(end["useful_urls"] - self.start["useful_urls"]),
            "useful_documents": len(end["useful_documents"]),
            "target_market_documents": len(end["target_market_documents"]),
            "other_variant_documents": len(end["other_variant_documents"]),
            "official_sources": len(end["official_sources"]),
            "candidate_fields": end["fields_with_candidates"],
            "candidate_field_coverage_pct": coverage_pct(end),
            "no_artifact_turns": sum(1 for t in self.turns if not t["artifacts"]),
            "final_no_artifact_streak": self.streak,
            "max_no_artifact_streak": self.max_streak,
            "turn_artifacts": [{"turn": t["turn"], "artifacts": t["artifacts"]} for t in self.turns],
            "research_s": research_s,
            "settings": {"max_turns": self.config.max_steps,
                         "no_artifact_stop": self.config.primary_research_no_artifact_stop,
                         "min_useful_documents": self.config.primary_research_min_useful_documents,
                         "candidate_field_coverage_threshold":
                             threshold_pct(self.config.primary_research_candidate_field_coverage_threshold)},
            "note": "scheduling telemetry: acquisition progress, never field truth",
        }

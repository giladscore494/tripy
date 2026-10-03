"""Parser-gap telemetry: a field LABEL is in a fetched document, but the deterministic harvest produced NO value for it
from that document. Observational only.

    usable cached document of the run (tail_planner.usable_document; not bound to another variant)
            ↓
    field labels located by the field dictionary's aliases (document_inspection.inspect_document, local)
            +
    the document's cached harvest (candidate_harvest.harvest_document; a cache hit, never a re-parse)
            ↓
    a gap row per (field, document) with a label hit and no harvested candidate:
    {field, document_id, source_url, source_authority, market, matched_alias, offset, snippet}

Zero model calls, zero network; it creates no evidence, changes no field state and decides nothing. One
`parser_gaps` event per run (right after the harvest report, before the document sweep); on any error a
`parser_gaps_failed` event and the run continues. src/diagnostics.py joins it with what the sweep found
(`candidate_missed_by_deterministic_harvest`) into a catalog-wide parser backlog.
"""

from __future__ import annotations

import time
from typing import Iterable

MAX_ROWS = 200
SNIPPET_CHARS = 200
SNIPPET_LEAD = 60          # characters of context kept before the label


def _snippet(match: dict) -> str:
    text = str(match.get("snippet") or "")
    lead = max(0, int(match.get("label_offset", match.get("offset", 0))) - int(match.get("offset", 0)) - SNIPPET_LEAD)
    return " ".join(text[lead:lead + SNIPPET_CHARS].split())[:SNIPPET_CHARS]


def parser_gaps(*, cache, adm, documents: Iterable[str], specs: list[dict], max_rows: int = MAX_ROWS) -> dict:
    """Gap rows of one run, in field schema order then document order (capped at `max_rows`; `truncated` counts the
    rows left out). Pure over the document cache."""
    from .candidate_harvest import dictionary_for, harvest_document
    from .document_inspection import inspect_document
    from .fields import normalize_field_name
    from .tail_planner import document_profile_for, usable_document

    t0 = time.monotonic()
    rules = {r.name for r in dictionary_for(specs).rules}
    fields = [s["name"] for s in specs if s.get("applicable", True) and s["name"] in rules]
    per_field: dict[str, list[dict]] = {f: [] for f in fields}
    scanned = 0
    for doc in dict.fromkeys(str(d) for d in documents):
        if not usable_document(cache, doc):
            continue
        profile = document_profile_for(adm, cache, doc)
        if profile.get("variant_match") == "different":
            continue
        scanned += 1
        harvested, _ = harvest_document(cache, doc, specs)        # cached by the run's harvester: no re-parse
        with_value = {normalize_field_name(c.get("field")) for c in harvested}
        missing = [f for f in fields if f not in with_value]
        if not missing:
            continue
        found = inspect_document(cache, doc, specs, missing, max_matches_per_field=1, candidates_per_field=0)
        meta = cache.get(doc) or {}
        for name in missing:
            match = ((found.get("matches") or {}).get(name) or [None])[0]
            if match is None:
                continue
            per_field[name].append({"field": name, "document_id": doc,
                                    "source_url": meta.get("final_url") or meta.get("url"),
                                    "source_authority": profile.get("source_authority"),
                                    "market": profile.get("market"), "matched_alias": match.get("matched_alias"),
                                    "offset": match.get("label_offset", match.get("offset")),
                                    "snippet": _snippet(match)})
    rows = [row for name in fields for row in per_field[name]]
    return {"rows": rows[:max_rows], "gaps_total": len(rows), "truncated": max(0, len(rows) - max_rows),
            "fields_with_gaps": sum(1 for name in fields if per_field[name]), "documents_scanned": scanned,
            "fields_checked": len(fields), "model_calls": 0,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "note": "label in the document, no harvested value: parser backlog telemetry, never evidence"}


def log_parser_gaps(run_log, *, cache, adm, documents: Iterable[str], specs: list[dict]) -> dict | None:
    """Compute and log the run's `parser_gaps` event. Never raises (logs `parser_gaps_failed` instead)."""
    try:
        report = parser_gaps(cache=cache, adm=adm, documents=list(documents), specs=specs)
        run_log.event("parser_gaps", **report)
    except Exception as exc:  # noqa: BLE001 - telemetry must never cost the run
        try:
            run_log.event("parser_gaps_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        except Exception:  # noqa: BLE001
            pass
        return None
    return report

"""Structured engineering feedback from real runs (deterministic: no model call, no API call).

Every run's events are turned into labelled examples that say where the deterministic layers (harvester, admission,
binding, portability) agreed or disagreed with what was finally admitted:

    candidate_accepted           a parser candidate that became admitted evidence (same field, document, value)
    candidate_rejected           a parser candidate whose promotion the admission gate refused (reason codes kept)
    candidate_false_positive     a proposed value (parser candidate or model claim) with an EXPLICIT contradiction:
                                 the gate refused it for a contradiction reason AND the same document states another
                                 value for the field (an admitted item or a parser candidate), or the gate found the
                                 value stated for another field / another quantity. Never merely "not promoted".
    deterministic_miss           admitted (non-reused) evidence exists AND the parser produced no candidate for that
                                 field and value from that document
    evidence_admission_rejected  a store_evidence request the gate refused (reason codes)
    variant_binding_rejected     admitted evidence the server bound to another variant / no variant while the model
                                 did not say "different" (e.g. a 2.0 Hybrid fact cited for a 1.8 target)
    conflict_example             a field that ended conflicting, with its competing values and conflict class
    portability_accepted / portability_rejected
                                 a foreign item the portability policy counted / refused for the target market
                                 (fields whose policy never allows portability are not examples)

Per run: `<run dir>/training_feedback.jsonl` (written by the run's own worker, atomically: no shared append).
Export: `export_feedback(runs_dir, out_dir)` aggregates every run deterministically (sorted, de-duplicated by
example_id; a run without the file is derived from its events.jsonl) into training_feedback.jsonl / .csv and
training_feedback_summary.json (counts by type x field, source family and pattern: which rule to fix next).
No document is copied: quotes are cut to QUOTE_CHARS.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .field_recovery import NON_TARGET_VARIANTS, current_evaluation, material_key
from .fields import normalize_field_name
from .storage import trace
from .storage.atomic import atomic_write_json, atomic_write_text

FEEDBACK_VERSION = "feedback-v1"
FEEDBACK_FILE = "training_feedback.jsonl"
EXAMPLE_TYPES = ("deterministic_miss", "candidate_false_positive", "candidate_rejected", "candidate_accepted",
                 "evidence_admission_rejected", "variant_binding_rejected", "conflict_example",
                 "portability_accepted", "portability_rejected")
# admission reasons that say the proposed value is NOT what the source states (vs. a quote / unit / format problem)
CONTRADICTION_REASONS = ("value_not_stated", "value_belongs_to_other_field", "semantic_mismatch")
QUOTE_CHARS = 300
COLUMNS = ("example_id", "example_type", "record_id", "manufacturer", "model", "year", "trim", "propulsion", "field",
           "candidate_value", "accepted_value", "rejected_value", "contradicting_value", "unit", "quote", "document_id", "source_url",
           "source_domain", "source_authority", "source_market", "binding_level", "variant_match", "matched_alias",
           "extraction_method", "parser_confidence", "reason_code", "pattern", "schema_hash", "harvester_version",
           "phase", "timestamp", "feedback_version")


def _short(text: Any, limit: int = QUOTE_CHARS) -> str | None:
    if text in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pattern_of(quote: Any, alias: str | None = None) -> str | None:
    """A reusable shape of the source wording: the label part of the quote with numbers masked ('גובה # ס"מ')."""
    text = _short(quote, 120)
    if not text:
        return alias
    label = text.split(":", 1)[0] if ":" in text[:60] else text[:60]
    masked = re.sub(r"\d[\d,.]*", "#", label + (":" + text.split(":", 1)[1][:30] if ":" in text[:60] else ""))
    return masked.strip()


def _domain(url: Any) -> str | None:
    from .source_authority import host_of

    return host_of(url) or None


def _bool_value(value: Any) -> bool | None:
    from .typed_values import as_boolean

    return as_boolean(value)


def _same_value(a: Any, b: Any) -> bool:
    ba, bb = _bool_value(a), _bool_value(b)
    if ba is not None and bb is not None:
        return ba == bb
    return material_key(a) == material_key(b)


def _phase_finder(events: list[dict]):
    responses = [(e.get("seq") or 0, e.get("phase")) for e in events if e.get("kind") == "model_response"]

    def phase_at(seq: int | None) -> str | None:
        phase = None
        for s, p in responses:
            if seq is not None and s > seq:
                break
            phase = p
        return phase
    return phase_at


def _vehicle(payload: dict | None, events: list[dict]) -> dict:
    started = trace.first_event(events, "run_started") or {}
    ident = (payload or {}).get("identity") or {}
    engine = (payload or {}).get("engine_drivetrain") or {}
    label = started.get("vehicle_label") or {}
    return {"record_id": str(started.get("record_id") or ident.get("government_record_id") or ""),
            "manufacturer": ident.get("manufacturer") or label.get("manufacturer"),
            "model": ident.get("commercial_name") or label.get("model"),
            "year": ident.get("year") or label.get("year"), "trim": ident.get("trim") or label.get("trim"),
            "propulsion": engine.get("propulsion_normalized") or label.get("propulsion")}


def _example(kind: str, vehicle: dict, versions: dict, **data: Any) -> dict:
    out = {"example_type": kind, **vehicle, **versions, "feedback_version": FEEDBACK_VERSION}
    for key, value in data.items():
        if value not in (None, "", [], {}):
            out[key] = value
    if "quote" in out:
        out["quote"] = _short(out["quote"])
    if out.get("source_url") and not out.get("source_domain"):
        out["source_domain"] = _domain(out["source_url"])
    identity = [kind, out.get("record_id"), out.get("field"), out.get("document_id") or out.get("source_url"),
                json.dumps(out.get("candidate_value"), default=str), json.dumps(out.get("accepted_value"), default=str),
                json.dumps(out.get("rejected_value"), default=str), json.dumps(out.get("contradicting_value"),
                                                                                default=str),
                out.get("reason_code"), out.get("quote"), out.get("extraction_method")]
    out["example_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, default=str).encode("utf-8")) \
        .hexdigest()[:20]
    return out


def feedback_examples(events: list[dict], payload: dict | None = None, specs: list[dict] | None = None,
                      target_market: str | None = None) -> list[dict]:
    """Every labelled example of one run, from its events (deterministic order)."""
    vehicle = _vehicle(payload, events)
    harvested = [e for e in events if e.get("kind") == "candidates_harvested"]
    versions = {"schema_hash": next((e.get("schema_hash") for e in harvested if e.get("schema_hash")), None),
                "harvester_version": next((e.get("harvester_version") for e in harvested
                                           if e.get("harvester_version")), None)}
    phase_at = _phase_finder(events)
    started = trace.first_event(events, "run_started") or {}
    specs = specs if specs is not None else list(started.get("requested_field_specs") or [])
    by_doc_field: dict[tuple[str, str], list[dict]] = {}
    candidate_events: list[tuple[dict, dict]] = []
    seen_docs: set[str] = set()
    for event in harvested:
        if event.get("document_id") in seen_docs:
            continue
        seen_docs.add(event.get("document_id"))
        for cand in event.get("candidates") or []:
            key = (normalize_field_name(cand.get("field")), str(cand.get("document_id") or event.get("document_id")))
            by_doc_field.setdefault(key, []).append(cand)
            candidate_events.append((cand, event))
    evidence_events = [e for e in events if e.get("kind") == "evidence" and isinstance(e.get("evidence"), dict)]
    admitted = [(e["evidence"], e) for e in evidence_events]
    rejected = [e for e in events if e.get("kind") == "evidence_rejected"]
    out: list[dict] = []

    def common_candidate(cand: dict, event: dict) -> dict:
        return {"field": normalize_field_name(cand.get("field")), "candidate_value": cand.get("value"),
                "unit": cand.get("unit"), "quote": cand.get("quote"),
                "document_id": cand.get("document_id") or event.get("document_id"),
                "source_url": cand.get("source_url") or event.get("url"), "matched_alias": cand.get("matched_alias"),
                "extraction_method": cand.get("extraction_method"), "parser_confidence": cand.get("parser_confidence"),
                "pattern": pattern_of(cand.get("quote"), cand.get("matched_alias")), "phase": event.get("phase"),
                "timestamp": event.get("ts")}

    def doc_states_other(field: str, doc: str, value: Any) -> Any:
        """Another value the same document states for this field (admitted evidence first, then the parser)."""
        for item, _ in admitted:
            if normalize_field_name(item.get("field")) == field and str(item.get("document_id")) == doc \
                    and not _same_value(item.get("value"), value):
                return item.get("value")
        for cand in by_doc_field.get((field, doc), []):
            if not _same_value(cand.get("value"), value):
                return cand.get("value")
        return None

    # --- candidates: accepted / rejected / false positive -------------------------------------------------------
    for cand, event in candidate_events:
        base = common_candidate(cand, event)
        field, doc = base["field"], str(base["document_id"])
        match = next((item for item, _ in admitted if normalize_field_name(item.get("field")) == field
                      and str(item.get("document_id")) == doc and _same_value(item.get("value"), cand.get("value"))),
                     None)
        refusal = next((r for r in rejected if normalize_field_name(r.get("field")) == field
                        and str(r.get("document_id")) == doc and _same_value(r.get("value"), cand.get("value"))), None)
        if match is not None:
            out.append(_example("candidate_accepted", vehicle, versions, **base, accepted_value=match.get("value"),
                                binding_level=match.get("binding_level"), variant_match=match.get("variant_match"),
                                source_authority=match.get("source_authority"), source_market=match.get("market")))
        elif refusal is not None:
            reasons = refusal.get("reasons") or []
            other = doc_states_other(field, doc, cand.get("value"))
            contradicted = any(r in CONTRADICTION_REASONS for r in reasons) and (
                other is not None or any(r in ("value_belongs_to_other_field", "semantic_mismatch") for r in reasons))
            kind = "candidate_false_positive" if contradicted else "candidate_rejected"
            out.append(_example(kind, vehicle, versions, **base, rejected_value=cand.get("value"),
                                contradicting_value=other if contradicted else None, reason_code=",".join(reasons)))
        # a candidate nobody tried to promote is NOT labelled (not promoted != false)

    # --- model store requests the gate refused --------------------------------------------------------------------
    for event in rejected:
        request = event.get("request") or {}
        field = normalize_field_name(event.get("field"))
        doc = str(event.get("document_id") or request.get("document_id") or "")
        reasons = event.get("reasons") or []
        base = {"field": field, "rejected_value": event.get("value"), "unit": request.get("unit"),
                "quote": request.get("quote"), "document_id": doc or None,
                "source_url": event.get("source_url") or request.get("source_url"),
                "reason_code": ",".join(reasons), "pattern": pattern_of(request.get("quote")),
                "phase": phase_at(event.get("seq")), "timestamp": event.get("ts")}
        out.append(_example("evidence_admission_rejected", vehicle, versions, **base))
        is_candidate = any(_same_value(c.get("value"), event.get("value")) for c in by_doc_field.get((field, doc), []))
        other = doc_states_other(field, doc, event.get("value")) if doc else None
        if not is_candidate and any(r in CONTRADICTION_REASONS for r in reasons) and (
                other is not None or any(r in ("value_belongs_to_other_field", "semantic_mismatch") for r in reasons)):
            # the model's own reading was contradicted by the source (e.g. "מושבים חשמליים: אין" claimed as true)
            out.append(_example("candidate_false_positive", vehicle, versions, **base, contradicting_value=other,
                                candidate_value=event.get("value"), extraction_method="model_claim"))

    # --- admitted evidence: deterministic misses and binding rejections -----------------------------------------
    for item, event in admitted:
        field, doc = normalize_field_name(item.get("field")), str(item.get("document_id") or "")
        base = {"field": field, "accepted_value": item.get("value"), "unit": item.get("unit"),
                "quote": item.get("quote"), "document_id": item.get("document_id"),
                "source_url": item.get("source_url"), "source_domain": item.get("source_domain"),
                "source_authority": item.get("source_authority"), "source_market": item.get("market"),
                "binding_level": item.get("binding_level"), "variant_match": item.get("variant_match"),
                "phase": event.get("phase") or phase_at(event.get("seq")), "timestamp": event.get("ts")}
        variant = str(item.get("variant_match") or "").lower()
        if variant in NON_TARGET_VARIANTS and str(item.get("model_variant_claim") or "").lower() != "different":
            veto = item.get("binding_veto")
            out.append(_example("variant_binding_rejected", vehicle, versions, **base,
                                reason_code=",".join(veto) if isinstance(veto, list) else (veto or variant),
                                pattern=pattern_of(item.get("quote"))))
        if item.get("reused_from") or not doc or not item.get("admission_status"):
            continue          # a reused fact was not read from a document in this run; legacy items have no gate data
        if not any(_same_value(c.get("value"), item.get("value")) for c in by_doc_field.get((field, doc), [])):
            out.append(_example("deterministic_miss", vehicle, versions, **base, reason_code=item.get("entailment"),
                                pattern=pattern_of(item.get("quote"))))

    # --- final field states: conflicts and portability ----------------------------------------------------------
    if specs:
        items = {str(i.get("evidence_id")): i for i, _ in admitted}
        for entry in current_evaluation(events, specs, target_market):
            if entry["state"] == "conflicting":
                values = [items[str(i)].get("value") for i in entry.get("conflict_evidence_ids") or []
                          if str(i) in items]
                cls = entry.get("conflict_class") or {}
                quotes = [items[str(i)].get("quote") for i in entry.get("conflict_evidence_ids") or []
                          if str(i) in items]
                out.append(_example("conflict_example", vehicle, versions, field=entry["field"],
                                    candidate_value=values, reason_code=cls.get("class"),
                                    quote=" | ".join(str(q) for q in quotes if q),
                                    pattern=cls.get("class"), phase="final_state"))
            for eid, verdict in (entry.get("portability") or {}).items():
                if verdict.get("portability_basis") == "field_policy_not_portable":
                    continue
                item = items.get(str(eid)) or {}
                kind = "portability_accepted" if verdict.get("portable_to_target_market") else "portability_rejected"
                out.append(_example(kind, vehicle, versions, field=entry["field"], accepted_value=item.get("value"),
                                    unit=item.get("unit"), quote=item.get("quote"), document_id=item.get("document_id"),
                                    source_url=item.get("source_url"), source_authority=item.get("source_authority"),
                                    source_market=item.get("market"), binding_level=item.get("binding_level"),
                                    reason_code=verdict.get("portability_basis"),
                                    pattern=verdict.get("portability_policy"), phase="final_state"))
    unique: dict[str, dict] = {}
    for example in out:
        unique.setdefault(example["example_id"], example)
    return list(unique.values())


def write_run_feedback(run_dir: Path | str, examples: list[dict]) -> Path:
    """The run's own feedback file (atomic; one writer per run directory)."""
    path = Path(run_dir) / FEEDBACK_FILE
    atomic_write_text(path, "".join(json.dumps(e, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                                    for e in examples))
    return path


def summarize(examples: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    for e in examples:
        by_type[e["example_type"]] = by_type.get(e["example_type"], 0) + 1
    return {"examples": len(examples), "by_type": dict(sorted(by_type.items()))}


def _read_run(run_dir: Path) -> list[dict]:
    path = run_dir / FEEDBACK_FILE
    if path.is_file():
        rows = []
        for line in path.read_text("utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    from .storage.run_log import read_events

    events = read_events(events_path)
    payload = None
    if (run_dir / "input.json").is_file():
        try:
            payload = json.loads((run_dir / "input.json").read_text("utf-8"))
        except ValueError:
            payload = None
    return feedback_examples(events, payload)


def improvement_report(examples: Iterable[dict]) -> dict:
    """Deterministic improvement metrics: misses, false positives, admission and binding rejections grouped by
    field, source family and pattern (which parser / admission rule to fix next)."""
    from .source_authority import split_host

    tracked = ("deterministic_miss", "candidate_false_positive", "evidence_admission_rejected",
               "variant_binding_rejected")
    report: dict[str, dict] = {k: {"total": 0, "by_field": {}, "by_source_family": {}, "by_pattern": {}}
                               for k in tracked}
    for e in examples:
        slot = report.get(e.get("example_type"))
        if slot is None:
            continue
        slot["total"] += 1
        family = split_host(e.get("source_domain") or _domain(e.get("source_url")) or "")[1] or "unknown"
        for key, value in (("by_field", e.get("field")), ("by_source_family", family),
                           ("by_pattern", e.get("pattern") or e.get("reason_code"))):
            name = str(value or "unknown")
            slot[key][name] = slot[key].get(name, 0) + 1
    for slot in report.values():
        for key in ("by_field", "by_source_family", "by_pattern"):
            slot[key] = dict(sorted(slot[key].items(), key=lambda kv: (-kv[1], kv[0])))
    return {"feedback_version": FEEDBACK_VERSION, "metrics": report}


def export_feedback(runs_dir: Path | str, out_dir: Path | str | None = None) -> dict:
    """Aggregate every run's feedback into training_feedback.jsonl / .csv and a summary (deterministic order,
    de-duplicated by example_id, written atomically so a concurrent reader never sees a partial file)."""
    runs_dir = Path(runs_dir)
    out_dir = Path(out_dir) if out_dir else runs_dir / "_feedback"
    examples: dict[str, dict] = {}
    for batch in sorted(p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")) \
            if runs_dir.is_dir() else []:
        for run_dir in sorted(p for p in batch.iterdir() if p.is_dir()):
            for example in _read_run(run_dir):
                example = {**example, "batch_id": batch.name}
                examples.setdefault(example.get("example_id") or json.dumps(example, sort_keys=True), example)
    rows = sorted(examples.values(), key=lambda e: (str(e.get("batch_id")), str(e.get("record_id")),
                                                    str(e.get("example_type")), str(e.get("field")),
                                                    str(e.get("example_id"))))
    jsonl = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=("batch_id",) + COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else v)
                         for k, v in r.items()})
    paths = {"jsonl": out_dir / "training_feedback.jsonl", "csv": out_dir / "training_feedback.csv",
             "summary": out_dir / "training_feedback_summary.json"}
    atomic_write_text(paths["jsonl"], jsonl)
    atomic_write_text(paths["csv"], buffer.getvalue())
    atomic_write_json(paths["summary"], {**summarize(rows), **improvement_report(rows)})
    return {"examples": len(rows), **{k: str(v) for k, v in paths.items()}}

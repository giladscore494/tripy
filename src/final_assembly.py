"""Deterministic Final Assembly (FINAL_ASSEMBLY=deterministic, the default): the run's final output is built in CODE
from the current field states (bundle.current_field_states, i.e. current_evaluation()) and the admitted evidence items
(trace.evidence_items). No value ever comes from a candidate, an excerpt, a model note, the research reply or the
finalizer. A model may only NARRATE: one small no-tool call may write `summary` and `research_trace` from a compact
digest that carries no values; every other key of its reply is ignored, a narration stating any number its digest
does not state is discarded (`narration_rejected`), and a failed or rejected narration falls back to a code-written
summary.

The output keeps the finalizer's OUTPUT_SHAPE (src/agent.py), so the UI, result.json and benchmark readers keep
working. Per field state:

    ok                    value / unit / market / valid_as_of of the evidence that carries it (see _carriers),
                          provenance israel_direct (target-market evidence) or foreign_direct (a portable foreign item,
                          its portability_basis in notes), evidence_ids = the carriers
    conflicting           value null, every distinct admitted value in alternatives, the field in `conflicts` with its
                          conflict_class
    foreign_market_only / variant_not_exact / weak_provenance
                          value null, the admitted items as alternatives, provenance unresolved, a note naming the state
    not_applicable        value null, provenance unresolved, note not_applicable
    missing / unresolved  value null, provenance unresolved

The carriers of an `ok` value are the field's admitted items in the evaluator's server-side target scope
(field_recovery.in_server_scope: binding reaches the field's requirement, target market or portable), narrowed to the
evidence ids of a backed conflict_resolved declaration when the evaluator accepted one. Field states do not record
these ids themselves, so they are recomputed here with the evaluator's own predicates (never a different rule, never a
majority). Carriers that disagree in value are not picked from: unless the evaluator's conflict classification already
says the values are identical (unit_equivalent) or a trim-bound scalar inside a model-line range (scalar_inside_range),
the field is logged as `final_assembly_inconsistent` and emitted as conflicting. So is an `ok` field with NO carrier in
that scope (no fallback to other evidence ids: a non-portable item is never emitted as foreign_direct).
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .storage import trace

FINAL_ASSEMBLY_VERSION = "final-assembly-v1"
NARRATION_MAX_TOKENS = 2000      # reasoning shares this budget with the reply (glm-5.3 always reasons)
OPEN_STATES = ("foreign_market_only", "variant_not_exact", "weak_provenance")
NARRATION_KEYS = ("summary", "research_trace")

NARRATION_SYSTEM_PROMPT = """You write the narrative part of a finished vehicle research run. The field values were
already assembled by code from verified evidence; you never see them and you must not state any value. From the digest
(the vehicle identity, field-state counts, which fields are resolved or open and the sources used) write:

- summary: 2-4 sentences on what was found and what remains open, and why (state names), without values;
- research_trace: up to 6 short step descriptions of how the research proceeded.

Reply with ONLY one JSON object: {"summary": "...", "research_trace": ["...", "..."]}"""


def _norm(name: Any) -> str:
    from .fields import normalize_field_name

    return normalize_field_name(name)


def _alternative(items: list[dict]) -> list[dict]:
    """Every distinct admitted (value, market, variant) with the evidence ids that state it, in evidence order."""
    from .field_recovery import _has_value, material_key

    groups: dict[tuple, dict] = {}
    for item in items:
        if not _has_value(item.get("value")):
            continue
        key = (material_key(item.get("value")), str(item.get("market") or ""), str(item.get("variant") or ""))
        group = groups.setdefault(key, {"value": item.get("value"), "market": item.get("market"),
                                        "variant": item.get("variant"), "evidence_ids": []})
        group["evidence_ids"].append(str(item.get("evidence_id")))
    return list(groups.values())


def _entry(state: str, **values: Any) -> dict:
    out = {"value": None, "unit": None, "market": None, "provenance": "unresolved", "alternatives": [],
           "valid_as_of": None, "notes": None, "evidence_ids": [], "state": state}
    out.update(values)
    return out


def _carriers(spec: dict, items: list[dict], state: dict, declared: dict | None, target_market: str) -> list[dict]:
    """The admitted items that carry an `ok` value (module docstring): server-side target scope, target-market items
    first, narrowed to a backed conflict_resolved declaration's evidence ids when the evaluator accepted one."""
    from .field_recovery import _has_value, field_requirement, in_server_scope, is_target_market

    requirement = field_requirement(spec)
    superseded = {str(i) for i in state.get("superseded_evidence_ids") or []}     # B2: alternatives, never carriers
    scoped = [i for i in items if _has_value(i.get("value")) and in_server_scope(i, target_market, requirement)
              and str(i.get("evidence_id")) not in superseded]
    if "conflict_resolved_by_model" in (state.get("info") or []):
        cited = {str(i) for i in (declared or {}).get("evidence_ids") or []}
        narrowed = [i for i in scoped if str(i.get("evidence_id")) in cited]
        scoped = narrowed or scoped
    return sorted(scoped, key=lambda i: 0 if is_target_market(i.get("market"), target_market) else 1)


def _value_identity(spec: dict, carriers: list[dict], state: dict) -> tuple[list[dict], str | None] | None:
    """(value carriers, the conflict class that makes differing carriers one value or None) when the carriers state
    ONE value, else None. Identity only: the same material value, or what the evaluator's conflict classification
    already established (unit_equivalent; scalar_inside_range: the trim-bound scalar carries the value)."""
    from .conflict_normalizer import interval
    from .field_recovery import material_key
    from .fields import with_dictionary

    if len({material_key(i.get("value")) for i in carriers}) <= 1:
        return carriers, None
    cls = state.get("conflict_class") or {}
    if not cls.get("normalized"):
        return None
    if cls.get("class") == "unit_equivalent":
        return carriers, "unit_equivalent"
    if cls.get("class") == "rounding_equivalent":
        # D1: one value; the most precise statement carries it, every carrier stays its evidence
        rep = str(cls.get("representative") or "")
        lead = [i for i in carriers if str(i.get("evidence_id")) == rep]
        if lead:
            return lead + [i for i in carriers if i is not lead[0]], "rounding_equivalent"
        return None
    if cls.get("class") == "scalar_inside_range":
        spec = with_dictionary(spec)
        spans = [(i, interval(i, spec)) for i in carriers]
        scalars = [i for i, s in spans if s is not None and s[0] == s[1]]
        if scalars and len({interval(i, spec) for i in scalars}) == 1:
            return scalars, "scalar_inside_range"
    return None


def _ok_entry(spec: dict, items: list[dict], state: dict, declared: dict | None, target_market: str,
              inconsistent: list[dict]) -> dict:
    from .field_recovery import is_target_market

    name = spec["name"]
    carriers = _carriers(spec, items, state, declared, target_market)
    # No carrier in the evaluator's server-side scope: never fall back to the state's own evidence ids (they may name a
    # non-portable foreign item that would be emitted as foreign_direct). The field is inconsistent: conflicting, with
    # every admitted item as an alternative.
    identity = _value_identity(spec, carriers, state) if carriers else None
    if identity is None:
        inconsistent.append({"field": name, "evidence_ids": [str(i.get("evidence_id")) for i in carriers],
                             "values": [i.get("value") for i in carriers][:10],
                             "reason": "carriers_disagree" if carriers else "no_carrier_in_scope"})
        return _conflicting_entry(name, items, state, reason="final_assembly_inconsistent")
    value_items, normalized = identity
    rep = value_items[0]
    target = is_target_market(rep.get("market"), target_market)
    ids = [str(i.get("evidence_id")) for i in value_items]
    others = [i for i in items if str(i.get("evidence_id")) not in ids]
    notes = None
    if not target:
        notes = f"portable foreign-market fact ({rep.get('portability_basis') or 'portability policy'})"
    if normalized:
        notes = "; ".join(n for n in (notes, f"values not in conflict: {normalized}") if n)
    if state.get("superseded_evidence_ids"):
        notes = "; ".join(n for n in (notes, "other values superseded by the importer spec sheet "
                                      f"(superseded_by_spec_sheet: {len(state['superseded_evidence_ids'])})") if n)
    return _entry("ok", value=rep.get("value"), unit=rep.get("unit"), market=rep.get("market"),
                  provenance="israel_direct" if target else "foreign_direct", alternatives=_alternative(others),
                  valid_as_of=rep.get("valid_as_of"), notes=notes, evidence_ids=ids)


def _conflicting_entry(name: str, items: list[dict], state: dict, reason: str | None = None) -> dict:
    return _entry("conflicting", alternatives=_alternative(items),
                  notes=f"conflicting: {reason or ((state.get('conflict_class') or {}).get('class') or 'conflict')}")


def _conflict(name: str, items: list[dict], state: dict, reason: str | None = None) -> dict:
    ids = {str(i) for i in state.get("conflict_evidence_ids") or []}
    in_conflict = [i for i in items if str(i.get("evidence_id")) in ids] or items
    cls = state.get("conflict_class") or {}
    return {"field": name,
            "values": [{k: a[k] for k in ("value", "market", "evidence_ids")} for a in _alternative(in_conflict)],
            "conflict_class": reason or cls.get("class") or "unclassified",
            "conflict_detail": cls.get("detail"),
            "model_comment": None}


def assemble_output(events: list[dict], payload: dict | None, specs: list[dict], target_market: str | None = None,
                    record_id: str | None = None) -> tuple[dict, dict]:
    """(output in OUTPUT_SHAPE, report {states, inconsistent, admitted_evidence, ...}). Pure: no model call, no
    logging; values only from the field states and admitted evidence items."""
    from .bundle import current_field_states
    from .field_recovery import DEFAULT_TARGET_MARKET, declarations

    started = trace.first_event(events, "run_started") or {}
    market = target_market or started.get("target_market") or DEFAULT_TARGET_MARKET
    payload = payload or {}
    states = current_field_states(events, specs, market)
    evidence = trace.evidence_items(events)
    by_field: dict[str, list[dict]] = {}
    for item in evidence:
        by_field.setdefault(_norm(item.get("field")), []).append(item)
    declared = declarations(events)
    fields: dict[str, dict] = {}
    conflicts: list[dict] = []
    inconsistent: list[dict] = []
    for spec in specs:
        name = spec["name"]
        state = states.get(name) or {"state": "missing"}
        verdicts = state.get("portability") or {}
        items = [{**i, **verdicts.get(str(i.get("evidence_id")), {})} for i in by_field.get(name, [])]
        kind = state.get("state") or "missing"
        if kind == "ok":
            entry = _ok_entry(spec, items, state, declared.get(name), market, inconsistent)
            if entry["state"] == "conflicting":
                conflicts.append(_conflict(name, items, state, "final_assembly_inconsistent"))
        elif kind == "conflicting":
            entry = _conflicting_entry(name, items, state)
            conflicts.append(_conflict(name, items, state))
        elif kind in OPEN_STATES:
            info = [i for i in state.get("info") or [] if i == "market_not_established"]
            entry = _entry(kind, alternatives=_alternative(items), notes=", ".join([kind] + info))
        elif kind == "not_applicable":
            staggered = next((i for i in state.get("info") or [] if str(i).startswith("staggered_axles:")), None)
            entry = _entry(kind, notes="not_applicable" + (
                f" (staggered set {staggered.split(':', 1)[1]}: see the per-axle fields)" if staggered else ""))
        else:                                       # missing / unresolved
            entry = _entry(kind, alternatives=_alternative(items), notes=kind)
        fields[name] = entry
    applicable = [s["name"] for s in specs if fields[s["name"]]["state"] != "not_applicable"]
    identity = payload.get("identity") or {}
    government = " / ".join(str(identity[k]) for k in ("trim", "model_code") if identity.get(k)) or None
    trim = fields.get("local_trim_name") or {}
    explicit = trim.get("state") == "ok" and trim.get("value") not in (None, "")
    output = {
        "vehicle_id": str(identity.get("government_record_id") or record_id or "") or None,
        "summary": "",
        "variant_identity": {
            "government": government,
            "local_commercial_name": trim.get("value") if explicit else None,
            "mapping_basis": "explicit_source" if explicit else "inference",
            "evidence_ids": list(trim.get("evidence_ids") or []) if explicit else [],
            "notes": "local trim name from admitted local_trim_name evidence" if explicit else
            "no admitted local_trim_name evidence: no local commercial name is stated"},
        "fields": fields,
        "conflicts": conflicts,
        "provenance_summary": {
            # identity anchors PR: the exact attribution string of every licence that requires one and carries a value
            "licence_attributions": licence_attributions(fields, evidence),
            "israeli_market_values": [n for n, e in fields.items() if e["provenance"] == "israel_direct"],
            "foreign_market_values": [n for n, e in fields.items() if e["provenance"] == "foreign_direct"],
            "inferred_variant_mappings": [],
            "conflicts": [c["field"] for c in conflicts],
            "unresolved_fields": [n for n in applicable if fields[n]["value"] is None]},
        "additional_findings": [],
        "level3": {},
        "research_trace": [],
    }
    counts: dict[str, int] = {}
    for entry in fields.values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    report = {"version": FINAL_ASSEMBLY_VERSION, "states": counts, "inconsistent": inconsistent,
              "admitted_evidence": len(evidence), "applicable_fields": len(applicable),
              "ok_fields": [n for n, e in fields.items() if e["state"] == "ok"],
              "open_fields": {n: fields[n]["state"] for n in applicable if fields[n]["value"] is None},
              "sources": _sources(fields, evidence), "stop_reason": None}
    output["summary"] = code_summary(report)
    output["research_trace"] = code_trace(events, report)
    return output, report


def licence_attributions(fields: dict, evidence: list[dict]) -> list[str]:
    """The attribution strings (open datasets: EEA CC-BY-4.0, ADEME Licence Ouverte, OGL-Canada) of the evidence that
    carries a final value, in first-use order."""
    used = {i for e in fields.values() if e.get("value") is not None for i in e.get("evidence_ids") or []}
    out: list[str] = []
    for item in evidence:
        text = item.get("attribution")
        if text and str(item.get("evidence_id")) in used and text not in out:
            out.append(text)
    return out


def _sources(fields: dict, evidence: list[dict]) -> list[str]:
    """Domains of the evidence that carries a final value (first-use order)."""
    from urllib.parse import urlparse

    used = {i for e in fields.values() for i in e.get("evidence_ids") or []}
    out: list[str] = []
    for item in evidence:
        if str(item.get("evidence_id")) in used:
            domain = item.get("source_domain") or urlparse(str(item.get("source_url") or "")).netloc
            if domain and domain not in out:
                out.append(domain)
    return out


def code_summary(report: dict) -> str:
    counts = report["states"]
    ok = counts.get("ok", 0)
    parts = [f"Assembled in code from {report['admitted_evidence']} admitted evidence item(s): {ok} of "
             f"{report['applicable_fields']} applicable field(s) resolved."]
    open_counts = {k: v for k, v in counts.items() if k not in ("ok", "not_applicable") and v}
    if open_counts:
        parts.append("Open: " + ", ".join(f"{v} {k}" for k, v in sorted(open_counts.items())) + ".")
    if counts.get("not_applicable"):
        parts.append(f"{counts['not_applicable']} not applicable.")
    if report["inconsistent"]:
        parts.append(f"{len(report['inconsistent'])} field(s) emitted as conflicting by the assembly check.")
    return " ".join(parts)


def code_trace(events: list[dict], report: dict) -> list[str]:
    stopped = next((e for e in reversed(events) if e.get("kind") == "research_stopped"), {}) or {}
    lines = [f"research: {trace.research_steps(events)} model turn(s), stop reason {stopped.get('reason') or 'n/a'}, "
             f"{len(trace.document_ids(events))} document(s) touched",
             f"evidence admission: {report['admitted_evidence']} item(s) admitted",
             "field states: " + ", ".join(f"{k}={v}" for k, v in sorted(report["states"].items()))]
    if report["sources"]:
        lines.append("sources of final values: " + ", ".join(report["sources"][:8]))
    return lines


def narration_digest(output: dict, report: dict, payload: dict | None) -> dict:
    """What the narration call sees: identity, state counts, ok / open field NAMES and source domains. No values."""
    identity = (payload or {}).get("identity") or {}
    return {"vehicle": {k: identity.get(k) for k in ("manufacturer", "commercial_name", "year", "trim", "model_code")
                        if identity.get(k)},
            "field_state_counts": report["states"], "ok_fields": report["ok_fields"],
            "open_fields": report["open_fields"], "conflicts": output["provenance_summary"]["conflicts"],
            "admitted_evidence_items": report["admitted_evidence"], "sources_used": report["sources"][:12]}


def parse_narration(text: str | None) -> dict:
    """ONLY `summary` (a string) and `research_trace` (a list of strings) from a narration reply; anything else,
    `fields` included, is ignored."""
    from .schemas import parse_model_output

    parsed, _ = parse_model_output(text)
    out: dict[str, Any] = {}
    if isinstance(parsed, dict):
        if isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
            out["summary"] = parsed["summary"].strip()[:2000]
        steps = parsed.get("research_trace")
        if isinstance(steps, list):
            lines = [str(s).strip()[:300] for s in steps if isinstance(s, (str, int, float)) and str(s).strip()]
            if lines:
                out["research_trace"] = lines[:12]
    return out


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# a number with an optional unit right after it (only for reporting the offending token)
_NUMBER_TOKEN = re.compile(r"\d+(?:[.,]\d+)*(?:\s?(?:%|[^\W\d_]{1,8}))?")


def _canonical_number(raw: str) -> str:
    """One spelling per number: "4,650" / "4650" -> "4650", "1.80" / "1,8" -> "1.8", "08" -> "8"; a dotted run that is
    no single number ("1.2.3", "01.02.2024") stays as written."""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        raw = raw.replace(",", "")
    raw = raw.replace(",", ".")
    if raw.count(".") > 1:
        return raw
    try:
        return format(Decimal(raw).normalize(), "f")
    except InvalidOperation:
        return raw


def digest_numbers(digest: dict) -> set[str]:
    """Every number the narration digest states (counts, digits inside field names, the identity, source domains)."""
    text = json.dumps(digest, ensure_ascii=False, default=str)
    return {_canonical_number(m.group(0)) for m in _NUMBER.finditer(text)}


def unsupported_numbers(narration: dict, digest: dict) -> list[str]:
    """The number tokens (with an optional unit) of a parsed narration (summary and research_trace) whose number the
    digest sent to the model does not state. The digest carries no field values, so a narration that states a value,
    a measurement or any other number of its own is caught here."""
    allowed = digest_numbers(digest)
    texts = [narration.get("summary") or ""] + list(narration.get("research_trace") or [])
    bad: list[str] = []
    for text in texts:
        for m in _NUMBER_TOKEN.finditer(str(text)):
            number = _NUMBER.match(m.group(0)).group(0)
            if _canonical_number(number) not in allowed and m.group(0).strip() not in bad:
                bad.append(m.group(0).strip())
    return bad


def run_deterministic_finalization(caller, *, run_log, events: list[dict], payload: dict, specs: list[dict],
                                   target_market: str | None, model: str, phase: str = "finalization",
                                   bundle: dict | None = None, record_id: str | None = None,
                                   narrate: bool = True,
                                   now: Callable[[], str] | None = None) -> dict:
    """Assemble the output in code, then (optionally) ONE narration call for summary / research_trace. Same return
    shape as agent.run_finalization ({output, parse_note, text, error, api_error, bundle, info}). Never raises for
    assembly, API or parse problems: an assembly failure is returned in `error`; a narration failure only means the
    code-written summary is kept."""
    from .storage.run_log import utc_now

    now = now or utc_now
    t0 = time.monotonic()
    info: dict[str, Any] = {"phase": phase, "model": model, "mode": "deterministic", "version": FINAL_ASSEMBLY_VERSION,
                            "started_at": now(), "evidence_items": None}
    run_log.event("finalization_started", phase=phase, model=model, mode="deterministic",
                  version=FINAL_ASSEMBLY_VERSION)
    try:
        output, report = assemble_output(events, payload, specs, target_market, record_id)
    except Exception as exc:  # noqa: BLE001 - never raises into the run
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        run_log.event("final_assembly_failed", phase=phase, error=error)
        run_log.event("finalization_failed", phase=phase, model=model, error=error, api_error=None)
        info.update({"status": "failed", "error": error, "finished_at": now(),
                     "latency_ms": int((time.monotonic() - t0) * 1000)})
        return {"output": None, "parse_note": "final_assembly_failed", "text": None, "error": error,
                "api_error": None, "bundle": bundle, "info": info}
    for bad in report["inconsistent"]:
        run_log.event("final_assembly_inconsistent", **bad,
                      note="an ok field's carrying evidence disagrees in value, or none is in its server-side scope: "
                           "emitted as conflicting, no value picked")
    info.update({"evidence_items": report["admitted_evidence"], "states": report["states"],
                 "inconsistent_fields": [b["field"] for b in report["inconsistent"]]})
    narration: dict[str, Any] = {"status": "skipped"}
    text = None
    if narrate:
        group = trace.phase_group(phase)
        before = dict(caller.usage[group])
        truncation_before = dict((getattr(caller, "truncation", None) or {}).get(group) or {})
        digest = narration_digest(output, report, payload)
        messages = [{"role": "system", "content": NARRATION_SYSTEM_PROMPT},
                    {"role": "user", "content": "Run digest (JSON):\n" + json.dumps(
                        digest, ensure_ascii=False, default=str)}]
        try:
            from .agent import call_with_truncation_retry
            from .phase_settings import for_phase

            limit = for_phase(getattr(caller, "config", None), phase).get("max_tokens")
            max_tokens = min(NARRATION_MAX_TOKENS, int(limit)) if limit else NARRATION_MAX_TOKENS
            # a truncated narration is retried once with a doubled max_tokens
            message = call_with_truncation_retry(caller, messages, phase=phase, model=model, max_tokens=max_tokens)
            text = message.get("content") or ""
            parsed = parse_narration(text)
            # the narration may only restate numbers of its digest: anything else is discarded as a whole
            bad = unsupported_numbers(parsed, digest) if parsed else []
            if bad:
                narration = {"status": "rejected", "keys_used": [], "unsupported_numbers": bad[:20]}
                run_log.event("narration_rejected", phase=phase, model=model, unsupported_numbers=bad[:20],
                              note="the narration states numbers its digest does not; the code-written summary is "
                                   "kept")
            else:
                output.update({k: parsed[k] for k in NARRATION_KEYS if k in parsed})
                narration = {"status": "ok" if parsed else "unparsed", "keys_used": sorted(parsed)}
        except Exception as exc:  # noqa: BLE001 - narration never blocks the result (timeouts included)
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            narration = {"status": "failed", "error": error,
                         "api_error": exc.as_dict() if hasattr(exc, "as_dict") else None}
            run_log.event("narration_failed", phase=phase, model=model, error=error,
                          note="the code-written summary is kept; the result is not affected")
        after = caller.usage[group]
        used = {k: after[k] - before.get(k, 0) for k in after}
        narration.update({k: used[k] for k in ("model_calls", "prompt_tokens", "completion_tokens", "total_tokens")})
        now_trunc = (getattr(caller, "truncation", None) or {}).get(group) or {}
        narration.update({k: int(now_trunc.get(k, 0)) - int(truncation_before.get(k, 0))
                          for k in ("truncated_calls", "truncation_retries")})
    info.update({"status": "assembled", "error": None, "api_error": None, "parse_note": "deterministic",
                 "narration": narration, "raw_text": text, "finished_at": now(),
                 "latency_ms": int((time.monotonic() - t0) * 1000),
                 "model_calls": narration.get("model_calls", 0), "prompt_tokens": narration.get("prompt_tokens", 0),
                 "completion_tokens": narration.get("completion_tokens", 0),
                 "total_tokens": narration.get("total_tokens", 0)})
    run_log.event("finalization_finished", phase=phase, model=model, status="assembled", mode="deterministic",
                  parse_note="deterministic", narration=narration.get("status"), states=report["states"],
                  latency_ms=info["latency_ms"])
    return {"output": output, "parse_note": "deterministic", "text": text, "error": None, "api_error": None,
            "bundle": bundle, "info": info}

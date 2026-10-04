"""Binding Replay: today's server-side binding of a FINISHED run's admitted evidence and open-field candidates.

Read-only, deterministic, no model, no network. For one vehicle run folder (<runs>/<run_id>/<record_id>) it reads
events.jsonl, the run's Level 1.5 input (input.json) and the shared document cache (the run's own exported copies in
documents/ when the shared cache lost a document), and recomputes binding with the CURRENT code through the same helper
Evidence Admission uses (evidence_admission.fact_binding), so the two cannot drift:

    admitted evidence          every evidence item: recorded vs now (level, variant_match, basis, veto, gap), the
                               document / identity-zone / per-layer dimension statuses, the year context, and the
                               deterministic candidates of the same field, document and material value from the run's
                               own `candidates_harvested` events (records carry no table / row / column provenance;
                               row-level fields are shown only when exactly one candidate matches; never invented)
    open-field candidates      the same binding fields for every harvested candidate of a field still open (no
                               admission re-run: the candidate is not evidence)
    per field                  the best variant_match now and `would_be_ok`: current_evaluation() over an IN-MEMORY
                               copy of the events whose evidence items carry the replayed binding; an item today's
                               admission sanity rules reject (`rejected_now`, evidence_admission.sanity_rejection) is
                               removed from that copy
    binding-v4 proof           per item the rules applied (`binding_rules_now`, `binding_basis_now`) and the Document
                               Variant Map decision recomputed from the cached document (`variant_map_region_now`);
                               per vehicle the rule that raised each newly ok field (`fields_newly_ok_by_rule`)
    per vehicle                ok fields recorded vs now, counts by binding_gap_now, items that rose because a recorded
                               year mismatch is gone (the model-year rules of binding-v3)

Outputs next to the run: binding_replay.jsonl (one row per item) and binding_replay_summary.json. Nothing is written
back to the run's events or evidence, and nothing into the shared cache (derived extractions are computed in memory).
A document missing from the cache is reported (`missing_documents`), never fatal.

    python -m src.binding_replay --run-dir <runs>/<run_id>/<record_id> [--cache-dir <cache>]
    python -m src.binding_replay --runs-dir <runs> --all [--cache-dir <cache>]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .document_binding import (BINDING_VERSION, LEVELS, NON_TARGET_MATCHES, binding_gaps, fact_layer_statuses,
                               level_index)
from .evidence_admission import (ADMISSION_VERSION, AdmissionContext, Entailment, entail, evidence_market,
                                 fact_binding, fact_context, sanity_rejection)
from .variant_map import VARIANT_MAP_VERSION
from .field_recovery import current_evaluation, material_key
from .fields import normalize_field_name, propulsion_of, resolve_requested_fields
from .storage import trace
from .storage.atomic import atomic_write_json, atomic_write_text
from .storage.run_log import read_events

REPLAY_FILE = "binding_replay.jsonl"
SUMMARY_FILE = "binding_replay_summary.json"
REPLAY_VERSION = "binding-replay-v3"
ROOT = Path(__file__).resolve().parent
# the code and data binding depends on: a change to any of them invalidates a cached replay
VERSION_FILES = (ROOT / "document_binding.py", ROOT / "evidence_admission.py", ROOT / "structure_harvest.py",
                 ROOT / "variant_map.py",
                 ROOT / "candidate_harvest.py", ROOT / "binding_replay.py", ROOT / "source_authority.py",
                 ROOT.parent / "data" / "identity_vocabulary.json", ROOT.parent / "data" / "catalog_trim_index.json",
                 ROOT.parent / "data" / "enrichment_fields.json", ROOT / "fields.py")
MATCH_ORDER = ("exact", "unclear", "unbound", "different")
BINDING_KEYS = ("binding_level", "variant_match", "binding_veto", "binding_dimensions", "binding_basis",
                "binding_requirement", "binding_version", "year_context", "binding_rules", "binding_policy",
                "variant_map_region")


def code_version() -> str:
    """The binding code version a replay was computed with (versions + content hash of VERSION_FILES)."""
    digest = hashlib.sha256(f"{REPLAY_VERSION}|{BINDING_VERSION}|{ADMISSION_VERSION}|{VARIANT_MAP_VERSION}".encode())
    for path in VERSION_FILES:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"-")
    return f"{BINDING_VERSION}:{digest.hexdigest()[:12]}"


class ReadOnlyCache:
    """The DocumentCache read interface over the shared cache and, as a fallback, the run's exported documents/
    copies. Never writes: a derived extraction missing from disk is computed in memory only."""

    def __init__(self, *roots: Path | str | None):
        self.dirs = [Path(r) / "documents" for r in roots if r]
        self.sources: dict[str, str] = {}

    def _dir(self, document_id: str) -> Path | None:
        for folder in self.dirs:
            if (folder / str(document_id) / "meta.json").is_file():
                return folder / str(document_id)
        return None

    def get(self, document_id: str) -> dict | None:
        folder = self._dir(document_id)
        if folder is None:
            return None
        try:
            meta = json.loads((folder / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        self.sources[str(document_id)] = "shared_cache" if folder.parent == self.dirs[0] else "run_documents"
        return meta

    def read_body(self, document_id: str) -> bytes:
        folder = self._dir(document_id)
        path = folder / "body.bin" if folder else None
        return path.read_bytes() if path and path.is_file() else b""

    def read_text(self, document_id: str) -> str:
        folder = self._dir(document_id)
        path = folder / "text.txt" if folder else None
        return path.read_text("utf-8") if path and path.is_file() else ""

    def get_derived(self, document_id: str, name: str) -> Any | None:
        folder = self._dir(document_id)
        path = folder / f"derived_{name}.json" if folder else None
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except ValueError:
            return None

    def put_derived(self, document_id: str, name: str, value: Any) -> None:     # read-only: never stored
        return None

    def derived(self, document_id: str, name: str, compute) -> tuple[Any, bool]:
        value = self.get_derived(document_id, name)
        return (value, True) if value is not None else (compute(), False)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text("utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def default_cache_dir(runs_dir: Path | None = None) -> Path:
    """The shared cache next to a runs folder (TRIPY_DATA_DIR layout, or the legacy <runs>/_cache), else the
    configured one."""
    from .storage.paths import resolve_paths

    if runs_dir is not None:
        for candidate in (Path(runs_dir).parent / "cache", Path(runs_dir) / "_cache"):
            if (candidate / "documents").is_dir():
                return candidate
    return resolve_paths().cache_dir


# --- one item -----------------------------------------------------------------------------------------------------------

def _candidate_row(cand: dict) -> dict:
    return {"extraction_method": cand.get("extraction_method"), "table_index": cand.get("table_index"),
            "row_index": cand.get("row_index"), "column_index": cand.get("column_index"),
            "header": cand.get("variant_hint") if not cand.get("variant_hints") else " | ".join(cand["variant_hints"]),
            "column_identity": cand.get("column_identity") if not cand.get("column_identities")
            else " || ".join(cand["column_identities"]),
            "column_identity_unknown": bool(cand.get("column_identity_unknown")) or None, "quote": cand.get("quote")}


def _recorded(item: dict, propulsion: str | None = None) -> dict:
    year = ((item.get("binding_dimensions") or {}).get("year") or {}).get("status")
    return {"binding_level_recorded": item.get("binding_level"), "variant_match_recorded": item.get("variant_match"),
            "binding_basis_recorded": item.get("binding_basis"), "binding_veto_recorded": item.get("binding_veto"),
            "binding_version_recorded": item.get("binding_version"), "year_status_recorded": year or "absent",
            "binding_gap_recorded": binding_gaps(item, item.get("binding_requirement"), propulsion)
            if item.get("binding_level") in LEVELS else None}


def replay_fact(adm: AdmissionContext, cache, *, field: str, value: Any, quote: str, document_id: str | None,
                source_url: str | None, variant: str | None = None, claim: str | None = None,
                market_claim: Any = None, candidates: dict[str, list[dict]] | None = None,
                run_documents: Iterable[str] = ()) -> dict:
    """Today's binding of one fact (an evidence item or a candidate). {missing_document: True} when the cited
    document is in neither cache."""
    name = normalize_field_name(field)
    spec = adm.spec(name)
    material = adm.material(cache, document_id, source_url, list(run_documents))
    if material is None:
        return {"missing_document": True}
    entailment = entail(adm, spec, value, quote, material) if quote else Entailment(False, reason="quote_missing")
    # a fact admitted earlier whose quote no longer entails its value keeps its whole quote as the stating fragment
    ctx = fact_context(adm, material, quote, entailment if entailment.ok else Entailment(False))
    market, _ = evidence_market(material, market_claim)
    pool = (candidates or {}).get(str(material.document_id))
    binding, inputs = fact_binding(adm, material, name, spec, value, quote, ctx, variant_text=variant or "",
                                   claim=claim, market=market, candidates=pool)
    profile = material.profile
    layers = fact_layer_statuses(adm.identity, inputs["layers"], profile.get("trim_named_in_document", False))
    matching = inputs["matching"]
    columns = inputs["columns"]
    out = {"document_id": material.document_id, "source_url": material.url, "market_now": market,
           "source_authority": material.authority.get("source_authority"),
           "document_source": getattr(cache, "sources", {}).get(str(material.document_id)),
           "entailment_now": entailment.method if entailment.ok else f"failed:{entailment.reason}",
           "requirement": binding["binding_requirement"],
           "binding_level_now": binding["binding_level"], "variant_match_now": binding["variant_match"],
           "binding_basis_now": binding.get("binding_basis"), "binding_veto_now": binding["binding_veto"],
           "binding_gap_now": binding_gaps(binding, binding["binding_requirement"], adm.identity.propulsion),
           "binding_dimensions_now": binding["binding_dimensions"],
           "binding_rules_now": binding.get("binding_rules"), "binding_policy_now": binding.get("binding_policy"),
           "variant_map_region_now": binding.get("variant_map_region"),
           # today's admission sanity rules (semantic exclusions, plausibility): a stored value they reject now
           "rejected_now": sanity_rejection(adm, material, spec, value, quote, ctx),
           "document_statuses": profile["statuses"], "zone_statuses": profile.get("zone_statuses"),
           "layer_statuses": {layer: st for layer, st in layers},
           "year_context": profile.get("year_context"),
           "candidates_source": "run_events" if pool is not None else "document_harvest",
           "matching_candidates": [_candidate_row(c) for c in matching],
           "effective_column_identity": next((text for layer, text in inputs["layers"]
                                              if layer == "column_identity" and text), None),
           "candidate_match_count": len(matching),
           "candidate_match_ambiguous": len(matching) > 1 or len(columns) > 1
           or any(c.get("column_identity_unknown") for c in matching)}
    if len(matching) == 1:          # row-level provenance only when exactly one candidate matches
        row = _candidate_row(matching[0])
        out.update({"table_index": row["table_index"], "row_index": row["row_index"],
                    "column_index": row["column_index"], "column_header": row["header"],
                    "column_identity": row["column_identity"]})
    out["_binding"] = {**binding, "binding_version": BINDING_VERSION}
    return out


# --- one run ------------------------------------------------------------------------------------------------------------

def _run_inputs(run_dir: Path) -> tuple[list[dict], dict, dict, list[dict], str]:
    events = read_events(run_dir / "events.jsonl")
    payload = _read_json(run_dir / "input.json") or {}
    started = trace.first_event(events, "run_started") or {}
    vehicle = dict(started.get("vehicle_label") or {})
    names = [s.get("name") for s in started.get("requested_field_specs") or [] if s.get("name")]
    names = names or list((started.get("agent_config") or {}).get("requested_fields") or [])
    specs = resolve_requested_fields(names or None, propulsion=propulsion_of(payload, vehicle))
    return events, payload, vehicle, specs, str(started.get("target_market") or "IL")


def _candidates_by_document(events: list[dict]) -> dict[str, list[dict]]:
    """The deterministic harvest's candidates of each document, as the run logged them (what admission's column
    layers read; model-located candidates never feed binding)."""
    out: dict[str, list[dict]] = {}
    for event in events:
        if event.get("kind") == "candidates_harvested" and not event.get("source") and event.get("document_id"):
            out.setdefault(str(event["document_id"]), [])
            seen = {json.dumps(c, sort_keys=True, default=str) for c in out[str(event["document_id"])]}
            out[str(event["document_id"])] += [c for c in event.get("candidates") or []
                                               if json.dumps(c, sort_keys=True, default=str) not in seen]
    return out


def _best(matches: Iterable[str | None]) -> str | None:
    found = [m for m in matches if m in MATCH_ORDER]
    return min(found, key=MATCH_ORDER.index) if found else None


def _would_be(events: list[dict], replayed: dict[str, dict], specs: list[dict], market: str,
              rejected: set[str] | frozenset = frozenset()) -> list[dict]:
    """current_evaluation() over an in-memory copy of the events whose evidence carries the replayed binding; evidence
    today's admission sanity rules reject (`rejected`, evidence ids) is removed."""
    copied = [e for e in copy.deepcopy(events)
              if not (e.get("kind") == "evidence" and isinstance(e.get("evidence"), dict)
                      and str(e["evidence"].get("evidence_id")) in rejected)]
    for event in copied:
        item = event.get("evidence") if event.get("kind") == "evidence" else None
        binding = replayed.get(str((item or {}).get("evidence_id")))
        if isinstance(item, dict) and binding is not None:
            for key in BINDING_KEYS:
                item.pop(key, None)
            item.update({k: v for k, v in binding.items() if k in BINDING_KEYS and v not in (None, [], {})})
    return current_evaluation(copied, specs, market)


def replay_run(run_dir: Path | str, cache_dir: Path | str | None = None, *, write: bool = True) -> dict:
    """Replay one vehicle run folder; returns {summary, items}. With `write`, binding_replay.jsonl and
    binding_replay_summary.json are written next to the run (nothing else is touched)."""
    run_dir = Path(run_dir)
    events, payload, vehicle, specs, market = _run_inputs(run_dir)
    cache = ReadOnlyCache(cache_dir if cache_dir is not None else default_cache_dir(run_dir.parent.parent), run_dir)
    adm = AdmissionContext.for_run(payload, vehicle, specs, market)
    run_documents = trace.document_ids(events)
    candidates = _candidates_by_document(events)
    recorded_eval = current_evaluation(events, specs, market)
    items: list[dict] = []
    replayed: dict[str, dict] = {}
    missing: dict[str, dict] = {}
    for item in trace.evidence_items(events):
        if item.get("admission_status", "accepted") != "accepted":
            continue
        row = {"kind": "evidence", "evidence_id": item.get("evidence_id"),
               "field": normalize_field_name(item.get("field")), "value": item.get("value"),
               "document_id": item.get("document_id"), "source_url": item.get("source_url"),
               "market": item.get("market"), "quote": item.get("quote"),
               "binding_rules_recorded": item.get("binding_rules"), **_recorded(item, adm.identity.propulsion)}
        try:
            now = replay_fact(adm, cache, field=item.get("field"), value=item.get("value"),
                              quote=str(item.get("quote") or ""), document_id=item.get("document_id"),
                              source_url=item.get("source_url"), variant=item.get("variant"),
                              claim=item.get("model_variant_claim"),
                              market_claim=item.get("model_market_claim") or item.get("market"),
                              candidates=candidates, run_documents=run_documents)
        except Exception as exc:  # noqa: BLE001 - one broken item never stops the replay
            now = {"replay_error": f"{type(exc).__name__}: {exc}"[:300]}
        binding = now.pop("_binding", None)
        if now.get("missing_document"):
            missing.setdefault(str(item.get("document_id") or item.get("source_url")),
                               {"document_id": item.get("document_id"), "source_url": item.get("source_url")})
        if binding is not None:
            replayed[str(item.get("evidence_id"))] = binding
            row["level_change"] = level_index(binding["binding_level"]) - level_index(item.get("binding_level")) \
                if item.get("binding_level") in LEVELS else None
            year_now = ((binding.get("binding_dimensions") or {}).get("year") or {}).get("status") or "absent"
            row["rose_by_year_rules"] = bool(row["level_change"] and row["level_change"] > 0
                                             and row["year_status_recorded"] == "mismatch" and year_now != "mismatch")
        items.append({**row, **now})
    rejected = {str(r["evidence_id"]) for r in items if r.get("rejected_now")}
    after = {e["field"]: e for e in _would_be(events, replayed, specs, market, rejected)}
    open_fields = [e["field"] for e in recorded_eval if e["retry_eligible"]]
    seen_candidates: set[tuple] = set()
    for cand in trace_candidates(events):
        name = normalize_field_name(cand.get("field"))
        if name not in open_fields:
            continue
        key = (name, cand.get("document_id"), material_key(cand.get("value")), cand.get("quote"))
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        row = {"kind": "candidate", "evidence_id": None, "field": name, "value": cand.get("value"),
               "document_id": cand.get("document_id"), "source_url": cand.get("source_url"),
               "quote": cand.get("quote"), "extraction_method": cand.get("extraction_method")}
        try:
            now = replay_fact(adm, cache, field=name, value=cand.get("value"), quote=str(cand.get("quote") or ""),
                              document_id=cand.get("document_id"), source_url=cand.get("source_url"),
                              candidates=candidates, run_documents=run_documents)
        except Exception as exc:  # noqa: BLE001
            now = {"replay_error": f"{type(exc).__name__}: {exc}"[:300]}
        now.pop("_binding", None)
        if now.get("missing_document"):
            missing.setdefault(str(cand.get("document_id") or cand.get("source_url")),
                               {"document_id": cand.get("document_id"), "source_url": cand.get("source_url")})
        items.append({**row, **now})
    for row in items:
        entry = after.get(row["field"])
        row["would_be_state"] = entry["state"] if entry else None
        row["would_be_ok"] = bool(entry and entry["state"] == "ok")
    summary = _summary(run_dir, items, recorded_eval, after, list(missing.values()), market)
    if write:
        lines = [json.dumps(row, ensure_ascii=False, default=str) for row in items]
        atomic_write_text(run_dir / REPLAY_FILE, "\n".join(lines) + ("\n" if lines else ""))
        atomic_write_json(run_dir / SUMMARY_FILE, summary)
    return {"summary": summary, "items": items}


def trace_candidates(events: list[dict]) -> list[dict]:
    """Every harvested candidate of the run (deterministic and model-located), with its document id."""
    out = []
    for event in events:
        if event.get("kind") == "candidates_harvested":
            out += [{**c, "document_id": c.get("document_id") or event.get("document_id")}
                    for c in event.get("candidates") or []]
    return out


def _summary(run_dir: Path, items: list[dict], recorded: list[dict], after: dict[str, dict], missing: list[dict],
             market: str) -> dict:
    evidence = [r for r in items if r["kind"] == "evidence"]
    fields: dict[str, dict] = {}
    for entry in recorded:
        name = entry["field"]
        rows = [r for r in items if r["field"] == name]
        admitted = [r for r in rows if r["kind"] == "evidence"]
        blocking = Counter(g for r in admitted if r.get("variant_match_now") not in ("exact", None)
                           for g in r.get("binding_gap_now") or [])
        now = after.get(name) or {}
        fields[name] = {"state_recorded": entry["state"], "would_be_state": now.get("state"),
                        "would_be_ok": now.get("state") == "ok",
                        "best_variant_match_recorded": _best(r.get("variant_match_recorded") for r in admitted),
                        "best_variant_match_now": _best(r.get("variant_match_now") for r in rows),
                        "evidence": len(admitted), "candidates": len(rows) - len(admitted),
                        "blocking_dimensions": dict(blocking),
                        "best_binding_level_now": max((r["binding_level_now"] for r in rows
                                                       if r.get("binding_level_now") in LEVELS),
                                                      key=level_index, default=None),
                        "binding_level_histogram": dict(Counter(r["binding_level_now"] for r in admitted
                                                                if r.get("binding_level_now") in LEVELS)),
                        "most_common_blocking_dimension": blocking.most_common(1)[0][0] if blocking else None,
                        "rejected_now": sum(1 for r in admitted if r.get("rejected_now")),
                        # the binding-v4 rule(s) that made this field's items exact now (binding_basis_now of the
                        # items exact now but not exact as recorded)
                        "raised_by": sorted({str(r.get("binding_basis_now") or "per_value") for r in admitted
                                             if r.get("variant_match_now") == "exact"
                                             and r.get("variant_match_recorded") != "exact"
                                             and not r.get("rejected_now")})}
    gaps = Counter(g for r in evidence if r.get("variant_match_now") not in ("exact", None)
                   for g in r.get("binding_gap_now") or [])
    year_now = Counter(str((r.get("binding_dimensions_now") or {}).get("year", {}).get("status") or "absent")
                       for r in evidence if r.get("binding_level_now"))
    vehicle = {
        "fields": len(recorded), "fields_ok_recorded": sum(1 for e in recorded if e["state"] == "ok"),
        "fields_ok_now": sum(1 for e in after.values() if e["state"] == "ok"),
        "fields_newly_ok": sorted(n for n, f in fields.items() if f["would_be_ok"] and f["state_recorded"] != "ok"),
        "fields_no_longer_ok": sorted(n for n, f in fields.items()
                                      if not f["would_be_ok"] and f["state_recorded"] == "ok"),
        "states_recorded": dict(Counter(e["state"] for e in recorded)),
        "states_now": dict(Counter(e["state"] for e in after.values())),
        "evidence": len(evidence), "candidates": len(items) - len(evidence),
        "evidence_exact_recorded": sum(1 for r in evidence if r.get("variant_match_recorded") == "exact"),
        "evidence_exact_now": sum(1 for r in evidence if r.get("variant_match_now") == "exact"),
        "evidence_level_rose": sum(1 for r in evidence if (r.get("level_change") or 0) > 0),
        "evidence_level_fell": sum(1 for r in evidence if (r.get("level_change") or 0) < 0),
        "evidence_rose_by_year_rules": sum(1 for r in evidence if r.get("rose_by_year_rules")),
        "evidence_year_mismatch_recorded": sum(1 for r in evidence if r.get("year_status_recorded") == "mismatch"),
        "year_status_counts_now": dict(year_now),
        "gap_counts": dict(gaps),
        "gap_counts_recorded": dict(Counter(g for r in evidence if r.get("variant_match_recorded") not in
                                            ("exact", None) for g in r.get("binding_gap_recorded") or [])),
        "non_target_now": sum(1 for r in evidence if r.get("variant_match_now") in NON_TARGET_MATCHES),
        "missing_documents": len(missing),
        # binding-v4 telemetry: evidence today's admission sanity rules reject (removed from would_be_state), and the
        # rule that raised each item whose level rose
        "rejected_now": sum(1 for r in evidence if r.get("rejected_now")),
        "rejected_now_reasons": dict(Counter(str((r.get("rejected_now") or {}).get("reason")) for r in evidence
                                             if r.get("rejected_now"))),
        "evidence_rose_by_rule": dict(Counter(str(r.get("binding_basis_now") or "per_value") for r in evidence
                                              if (r.get("level_change") or 0) > 0)),
        "fields_newly_ok_by_rule": {n: f["raised_by"] for n, f in sorted(fields.items())
                                    if f["would_be_ok"] and f["state_recorded"] != "ok"},
        "variant_map_regions": dict(Counter(str((r.get("variant_map_region_now") or {}).get("status"))
                                            for r in evidence if r.get("variant_map_region_now"))),
    }
    return {"schema": REPLAY_VERSION, "code_version": code_version(), "binding_version": BINDING_VERSION,
            "run_id": run_dir.parent.name, "record_id": run_dir.name, "target_market": market,
            "vehicle": vehicle, "fields": fields, "missing_documents": missing,
            "note": "read-only replay with the current binding code; nothing in the run was changed"}


def load_replay(run_dir: Path | str) -> dict | None:
    """{summary, items} of a replay written for this run with the CURRENT code version, else None."""
    run_dir = Path(run_dir)
    summary = _read_json(run_dir / SUMMARY_FILE)
    if not summary or summary.get("code_version") != code_version():
        return None
    if not (run_dir / REPLAY_FILE).is_file():
        return None
    items = read_events(run_dir / REPLAY_FILE)
    return {"summary": summary, "items": items}


def load_or_replay(run_dir: Path | str, cache_dir: Path | str | None = None, *, timeout_s: float = 30,
                   persist: bool = True) -> dict:
    """Reuse a current replay; compute old runs in a killable process with a hard per-run deadline.

    The child never writes. Only a complete response is persisted, so a timeout cannot publish a partial replay
    or leave a background task writing after diagnostics returned. `persist=False` (the read-only MCP) returns the
    computed replay without writing it.
    """
    cached = load_replay(run_dir)
    if cached is not None:
        return cached
    code = ("import json,sys; from src.binding_replay import replay_run; "
            "print(json.dumps(replay_run(sys.argv[1], sys.argv[2] or None, write=False), ensure_ascii=False))")
    try:
        proc = subprocess.run([sys.executable, "-c", code, str(run_dir), str(cache_dir or "")],
                              cwd=ROOT.parent, capture_output=True, text=True, timeout=timeout_s, check=True)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Binding replay exceeded {timeout_s:g} s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip()[-2000:] or str(exc)) from exc
    replay = json.loads(proc.stdout)
    if not persist:
        return replay
    atomic_write_text(Path(run_dir) / REPLAY_FILE, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                                          for r in replay["items"]))
    atomic_write_json(Path(run_dir) / SUMMARY_FILE, replay["summary"])
    return replay


def replay_after_result(run_log, cache_root) -> None:
    """Observational run-end hook. A replay failure cannot alter the result or status."""
    try:
        replay = replay_run(run_log.dir, cache_root, write=True)
        v = replay["summary"]["vehicle"]
        run_log.event("binding_replay_written", fields_ok_recorded=v["fields_ok_recorded"],
                      fields_ok_now=v["fields_ok_now"], evidence_exact_recorded=v["evidence_exact_recorded"],
                      evidence_exact_now=v["evidence_exact_now"], gap_counts=v["gap_counts"])
    except Exception as exc:
        # The listener may itself fail; diagnostics must never cost a run.
        from .server_logging import get_logger
        error = f"{type(exc).__name__}: {exc}"
        get_logger("binding_replay").warning("replay for %s failed: %s", run_log.dir, error)
        try:
            run_log.event("binding_replay_failed", error=error)
        except Exception:
            pass



def run_dirs(runs_dir: Path | str) -> list[Path]:
    """Every vehicle run folder (<runs>/<run_id>/<record_id> with events.jsonl)."""
    return sorted(p.parent for p in Path(runs_dir).glob("*/*/events.jsonl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.binding_replay", description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-dir", help="one vehicle run folder: <runs>/<run_id>/<record_id>")
    target.add_argument("--runs-dir", help="the runs folder (with --all)")
    parser.add_argument("--all", action="store_true", help="replay every vehicle run under --runs-dir")
    parser.add_argument("--cache-dir", help="the shared document cache (default: next to the runs folder, else the "
                                            "configured TRIPY_DATA_DIR cache)")
    args = parser.parse_args(argv)
    if args.runs_dir and not args.all:
        parser.error("--runs-dir needs --all")
    dirs = [Path(args.run_dir)] if args.run_dir else run_dirs(args.runs_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    for run_dir in dirs:
        if not (run_dir / "events.jsonl").is_file():
            print(json.dumps({"run_dir": str(run_dir), "error": "no events.jsonl"}), file=sys.stderr)
            continue
        summary = replay_run(run_dir, cache_dir)["summary"]
        print(json.dumps({"run_dir": str(run_dir), **summary["vehicle"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

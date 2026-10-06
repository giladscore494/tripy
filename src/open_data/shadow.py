"""R: the open-data shadow report on the server (the Data page's "Shadow report" button, POST /api/data/open-data/
shadow-report; MCP open_data_shadow_report() returns the last one).

Read-only over every run on the volume (the kept research-era runs included): per vehicle the Level 1.5 payload the
run used (input.json) and its identity fingerprint (the run's event, else recomputed from the payload), the open-data
match recomputed NOW from the committed snapshots, and its field offers compared with every field that run evaluates
`ok` (field_recovery.current_evaluation; a run without its field specs: the values its result.json states). Those
values are the test reference only; nothing is admitted.

Output (one JSON in <data>/derived/, written only when the volume has MIN_FREE_BYTES free):

    table          per (source, field, route): agree / disagree (any offer with a value) and offered_agree /
                   offered_disagree (status `offered`), no_offer (the run has the field ok, the source offers no value)
    proposed       the data/open_data_admission.json triples: (source, field, route) with >= min_agree agreements of
                   `offered` offers and 0 disagreements of any offer (the PR #56 rule; never written to the allowlist)
    disagreements  every disagreement with both values
    vehicles       per vehicle: route, level, designation, the status per source, the EEA type-code match
    type_code      per make: type codes, matches per rule, unmatched examples (K1)
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import datasets as ds

REPORT_NAME = "open_data_shadow_report.json"
SHADOW_VERSION = "open-data-shadow-v1"
MIN_AGREE = 5
EEA = "eea_co2_cars"
log = logging.getLogger("tripy.open_data")


def same_value(a: Any, b: Any) -> bool:
    """Numbers compared as numbers (1e-6), anything else as trimmed lower-case text."""
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def vehicle_dirs(runs_dir: Path) -> list[Path]:
    """Every <run>/<record>/ with an input.json (internal folders `_*` / `.*` skipped), newest run first."""
    out = []
    if not runs_dir.is_dir():
        return out
    for run in sorted((p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))),
                      key=lambda p: p.name, reverse=True):
        out += sorted(v for v in run.iterdir() if v.is_dir() and (v / "input.json").is_file())
    return out


def ok_values(vehicle: Path, events: list[dict]) -> dict[str, Any]:
    """{field: value} the run evaluates `ok` (its value from result.json)."""
    from ..fields import normalize_field_name
    from ..field_recovery import current_evaluation
    from ..schemas import iter_fields
    from ..storage import trace

    result = ds._read_json(vehicle / "result.json")
    values = {normalize_field_name(name): entry for name, entry in iter_fields(result.get("output"))}
    started = trace.first_event(events, "run_started") or {}
    specs = started.get("requested_field_specs") or []
    out: dict[str, Any] = {}
    if specs:
        for state in current_evaluation(events, specs, started.get("target_market")):
            entry = values.get(normalize_field_name(state.get("field"))) or {}
            value = entry.get("value")
            if state.get("state") == "ok" and value not in (None, "", []):
                out[normalize_field_name(state["field"])] = value
        return out
    for name, entry in values.items():                  # no specs recorded: the values the result states
        if entry.get("value") not in (None, "", []):
            out[name] = entry["value"]
    return out


def analyse_vehicle(vehicle: Path, folder: Path | None = None) -> dict:
    """The recomputed match of one run's vehicle and its offers compared with the run's ok values."""
    from ..fields import normalize_field_name
    from ..gov_registry import fingerprint_from_events, identity_fingerprint
    from ..storage.run_log import read_events
    from .makes import tozar_makes
    from .match import match
    from .offers import field_offers

    events = read_events(vehicle / "events.jsonl") if (vehicle / "events.jsonl").is_file() else []
    payload = ds._read_json(vehicle / "input.json")
    fingerprint = fingerprint_from_events(events) or identity_fingerprint(payload)
    result = match(fingerprint, payload, folder=folder)
    offers = field_offers(result)
    ok = ok_values(vehicle, events)
    for offer in offers:
        field = normalize_field_name(offer["field"])
        if offer.get("value") is None:
            offer["comparison"] = "no value"
        elif field not in ok:
            offer["comparison"] = "no ok value"
        else:
            offer["comparison"] = "agree" if same_value(offer["value"], ok[field]) else "disagree"
            offer["run_value"] = ok[field]
    ident = payload.get("identity") or {}
    plain, gated = tozar_makes(str(ident.get("manufacturer") or ""))
    return {"run_id": vehicle.parent.name, "record_id": vehicle.name,
            "label": " ".join(str(x) for x in (ident.get("manufacturer"), ident.get("commercial_name"),
                                                ident.get("year")) if x),
            "make": (plain or gated or [None])[0], "type_code": (result.get("keys") or {}).get("type_code"),
            "type_code_match": ((result.get("sources") or {}).get(EEA) or {}).get("type_code"),
            "route": result.get("route_detail") or result.get("route"), "level": result.get("level"),
            "designation": result.get("designation"),
            "sources": {s: v.get("status") for s, v in (result.get("sources") or {}).items()},
            "ok_fields": sorted(ok), "offers": offers}


def tally(vehicles: list[dict], field_map: list[dict] | None = None) -> list[dict]:
    """Per (source, field, route): agree / disagree / offered_agree / offered_disagree / no_offer."""
    from ..fields import normalize_field_name

    field_map = ds.config().get("field_map") if field_map is None else field_map
    counts: dict[tuple, Counter] = defaultdict(Counter)
    for row in vehicles:
        if row.get("error"):
            continue
        route = row.get("route")
        by_source_field = defaultdict(list)
        for offer in row.get("offers") or []:
            by_source_field[(offer["source"], normalize_field_name(offer["field"]))].append(offer)
        for entry in field_map or []:
            key = (entry["source"], normalize_field_name(entry["field"]))
            offers = [o for o in by_source_field.get(key, []) if o.get("value") is not None]
            if not offers:
                if key[1] in (row.get("ok_fields") or []):
                    counts[(*key, route)]["no_offer"] += 1
                continue
            for offer in offers:
                if offer.get("comparison") in ("agree", "disagree"):
                    counts[(*key, route)][offer["comparison"]] += 1
                    if offer.get("status") == "offered":
                        counts[(*key, route)][f"offered_{offer['comparison']}"] += 1
    return [{"source": s, "field": f, "route": r, **{k: c.get(k, 0) for k in (
        "agree", "disagree", "offered_agree", "offered_disagree", "no_offer")}}
        for (s, f, r), c in sorted(counts.items(), key=lambda kv: tuple(str(x) for x in kv[0]))]


def proposal(table: list[dict], min_agree: int = MIN_AGREE) -> list[dict]:
    """The (source, field, route) triples with >= min_agree agreements of offered offers and no disagreement."""
    return [{"source": t["source"], "field": t["field"], "route": t["route"], "agreements": t["offered_agree"]}
            for t in table if t["offered_agree"] >= min_agree and not t["disagree"] and not t["offered_disagree"]]


def type_code_view(vehicles: list[dict]) -> dict[str, dict]:
    """K1 per make: the vehicles' type codes, matches per rule, unmatched examples (with the match status)."""
    out: dict[str, dict] = {}
    for row in vehicles:
        if row.get("error") or not row.get("type_code") or not row.get("make"):
            continue
        item = out.setdefault(row["make"], {"degem_nm": set(), "per_rule": Counter(), "unmatched_examples": []})
        item["degem_nm"].add(row["type_code"])
        found = row.get("type_code_match") or {}
        if found.get("status") == "match":
            item["per_rule"][found.get("rule")] += 1
        elif len(item["unmatched_examples"]) < 8:
            item["unmatched_examples"].append(f"{row['type_code']} ({found.get('status') or 'no EEA rows'})")
    return {make: {"degem_nm": len(v["degem_nm"]), "per_rule": dict(v["per_rule"]),
                   "unmatched_examples": v["unmatched_examples"]} for make, v in sorted(out.items())}


def shadow_report(runs_dir: Path, *, folder: Path | None = None, min_agree: int = MIN_AGREE) -> dict:
    """The report of the module docstring (not written)."""
    vehicles = []
    for vehicle in vehicle_dirs(Path(runs_dir)):
        try:
            vehicles.append(analyse_vehicle(vehicle, folder))
        except Exception as exc:  # noqa: BLE001 - one vehicle never costs the report
            vehicles.append({"run_id": vehicle.parent.name, "record_id": vehicle.name,
                             "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    table = tally(vehicles)
    disagreements = [{"run_id": v["run_id"], "record_id": v["record_id"], "source": o["source"], "field": o["field"],
                      "route": v.get("route"), "status": o.get("status"), "offer_value": o.get("value"),
                      "run_value": o.get("run_value"), "row_ids": (o.get("row_ids") or [])[:5]}
                     for v in vehicles if not v.get("error") for o in v.get("offers") or []
                     if o.get("comparison") == "disagree"]
    man = ds.manifest()
    return {"version": SHADOW_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "runs_dir": str(runs_dir), "min_agree": min_agree, "snapshots_built_at": man.get("built_at"),
            "snapshots_run_url": man.get("run_url"),
            "vehicles_total": len(vehicles), "vehicles_failed": sum(1 for v in vehicles if v.get("error")),
            "table": table, "proposed": proposal(table, min_agree), "disagreements": disagreements,
            "type_code": type_code_view(vehicles),
            "vehicles": [{k: v.get(k) for k in ("run_id", "record_id", "label", "route", "level", "designation",
                                                "sources", "type_code", "type_code_match", "error")}
                         | {"offered": sum(1 for o in v.get("offers") or [] if o.get("status") == "offered"),
                            "agree": sum(1 for o in v.get("offers") or [] if o.get("comparison") == "agree"),
                            "disagree": sum(1 for o in v.get("offers") or [] if o.get("comparison") == "disagree")}
                         for v in vehicles]}


def report_path(derived_dir: Path) -> Path:
    return Path(derived_dir) / REPORT_NAME


def run_and_store(runs_dir: Path, derived_dir: Path, *, folder: Path | None = None,
                  min_agree: int = MIN_AGREE) -> dict:
    """Build the report and write it atomically to <derived>/open_data_shadow_report.json; refused (InsufficientDisk)
    when the volume has less than MIN_FREE_BYTES free. Reads runs only; writes only that JSON."""
    from ..storage.disk import check_free

    derived_dir = Path(derived_dir)
    derived_dir.mkdir(parents=True, exist_ok=True)
    check_free(derived_dir, "open-data shadow report")
    report = shadow_report(runs_dir, folder=folder, min_agree=min_agree)
    target = report_path(derived_dir)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), "utf-8")
    os.replace(tmp, target)
    log.info("open-data shadow report: %d vehicles, %d proposed triples, %d disagreements", report["vehicles_total"],
             len(report["proposed"]), len(report["disagreements"]))
    return report


def last_report(derived_dir: Path) -> dict | None:
    """The last stored report (None before the first)."""
    data = ds._read_json(report_path(derived_dir))
    return data or None

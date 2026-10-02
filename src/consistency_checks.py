"""Deterministic cross-field sanity checks over admitted evidence (QA signals, never truth generation).

Each check reads admitted target-variant evidence (and the fixed Level 1.5 record) and says

    consistent      the values are mutually plausible
    suspicious      the values contradict each other or the vehicle (look at the sources again)
    not_checkable   a needed value is missing

A check NEVER creates, replaces, removes or ranks a value, never changes a field state and is never
provenance: "CO2 103 g/km is consistent with 4.5 l/100km" says the fuel figure is not absurd, not that it
is right. Approximate physics is used only to flag; the bands are deliberately wide.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .fields import normalize_field_name
from .typed_values import numbers_in

CHECKS_VERSION = "consistency-v1"
# kg CO2 per litre of fuel / 100 = g/km per l/100km (tank-to-wheel, approximate)
CO2_PER_L100 = {"petrol": 23.2, "diesel": 26.4}
CO2_RATIO_BAND = (0.8, 1.25)
CURB_SHARE_OF_GROSS = (0.4, 1.0)
NON_TARGET = ("different", "unbound")
SEVERITY = {"suspicious": 2, "consistent": 1, "not_checkable": 0}
TIRE_RIM = re.compile(r"r\s?(\d{2})", re.I)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _target_items(evidence: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in evidence or []:
        if str(item.get("variant_match") or "").lower() in NON_TARGET:
            continue
        out.setdefault(normalize_field_name(item.get("field")), []).append(item)
    return out


def _nums(items: list[dict]) -> list[tuple[float, str]]:
    out = []
    for item in items:
        for n in numbers_in(item.get("value")):
            out.append((n, str(item.get("evidence_id"))))
    return out


def _check(name: str, status: str, fields: list[str], ids: list[str], detail: str) -> dict:
    return {"check": name, "status": status, "fields": fields, "evidence_ids": sorted(set(ids)), "detail": detail}


def _co2(by_field: dict, payload: dict) -> dict:
    fuel = _nums(by_field.get("fuel_consumption_combined_l_100km", []))
    env, engine = payload.get("environment") or {}, payload.get("engine_drivetrain") or {}
    co2 = _number(env.get("co2_wltp"))
    kind = str(engine.get("fuel_normalized") or "").lower()
    factor = CO2_PER_L100.get("diesel" if "diesel" in kind else "petrol" if kind and "electric" not in kind else "")
    fields = ["fuel_consumption_combined_l_100km", "government:co2_wltp"]
    if not fuel or not co2 or not factor:
        return _check("co2_vs_fuel_consumption", "not_checkable", fields, [i for _, i in fuel],
                      "needs fuel consumption evidence, the government WLTP CO2 and a petrol/diesel fuel")
    implied = float(co2) / factor
    bad = [(n, i) for n, i in fuel if not CO2_RATIO_BAND[0] <= n / implied <= CO2_RATIO_BAND[1]]
    detail = f"government CO2 {co2} g/km implies ~{implied:.2f} l/100km ({kind or 'petrol'})"
    return _check("co2_vs_fuel_consumption", "suspicious" if bad else "consistent", fields,
                  [i for _, i in (bad or fuel)], detail + (f"; outside the band: {[n for n, _ in bad]}" if bad else ""))


def _curb(by_field: dict, payload: dict) -> dict:
    curb = _nums(by_field.get("curb_weight_kg", []))
    gross = _number((payload.get("structure") or {}).get("gross_weight_kg"))
    fields = ["curb_weight_kg", "government:gross_weight_kg"]
    if not curb or not gross:
        return _check("curb_vs_gross_mass", "not_checkable", fields, [i for _, i in curb],
                      "needs curb weight evidence and the government gross mass")
    low, high = CURB_SHARE_OF_GROSS
    bad = [(n, i) for n, i in curb if not (low * float(gross) <= n < high * float(gross))]
    return _check("curb_vs_gross_mass", "suspicious" if bad else "consistent", fields, [i for _, i in (bad or curb)],
                  f"gross mass {gross} kg; curb must be below it (and above {low:.0%} of it)")


def _rims(by_field: dict, _: dict) -> dict:
    rims = _nums(by_field.get("rim_diameter_in", []))
    tire_items = [it for f in ("tire_size_front", "tire_size_rear", "alternative_tire_sizes")
                  for it in by_field.get(f, [])]
    sizes = {int(m.group(1)) for it in tire_items for m in TIRE_RIM.finditer(str(it.get("value") or ""))}
    fields = ["rim_diameter_in", "tire_size_front", "tire_size_rear", "alternative_tire_sizes"]
    ids = [i for _, i in rims] + [str(it.get("evidence_id")) for it in tire_items]
    if not rims or not sizes:
        return _check("rim_vs_tire_size", "not_checkable", fields, ids, "needs rim diameter and tire size evidence")
    bad = [n for n, _ in rims if int(round(n)) not in sizes]
    return _check("rim_vs_tire_size", "suspicious" if bad else "consistent", fields, ids,
                  f"tire rim diameters {sorted(sizes)}" + (f"; rim values not among them: {bad}" if bad else ""))


def _battery(by_field: dict, _: dict) -> dict:
    gross, usable = _nums(by_field.get("battery_gross_kwh", [])), _nums(by_field.get("battery_usable_kwh", []))
    ids = [i for _, i in gross + usable]
    if not gross or not usable:
        return _check("usable_vs_gross_battery", "not_checkable", ["battery_usable_kwh", "battery_gross_kwh"], ids,
                      "needs gross and usable battery evidence")
    bad = max(n for n, _ in usable) > max(n for n, _ in gross) * 1.001
    return _check("usable_vs_gross_battery", "suspicious" if bad else "consistent",
                  ["battery_usable_kwh", "battery_gross_kwh"], ids, "usable capacity cannot exceed gross capacity")


def _dimensions(by_field: dict, _: dict) -> dict:
    length = _nums(by_field.get("length_mm", []))
    others = {f: _nums(by_field.get(f, [])) for f in ("wheelbase_mm", "width_mm", "height_mm")}
    ids = [i for _, i in length] + [i for v in others.values() for _, i in v]
    fields = ["length_mm", "wheelbase_mm", "width_mm", "height_mm"]
    if not length or not any(others.values()):
        return _check("dimensions_below_length", "not_checkable", fields, ids, "needs length and another dimension")
    shortest = min(n for n, _ in length)
    bad = [(f, n) for f, values in others.items() for n, _ in values if n >= shortest]
    return _check("dimensions_below_length", "suspicious" if bad else "consistent", fields, ids,
                  "wheelbase, width and height must be below the length" + (f"; violated by {bad}" if bad else ""))


def _applicability(by_field: dict, payload: dict, specs: dict[str, dict]) -> dict:
    """Evidence for a field the schema says this propulsion does not have (e.g. charging data of a regular HEV)."""
    propulsion = (payload.get("engine_drivetrain") or {}).get("propulsion_normalized")
    if not propulsion:
        return _check("fields_vs_propulsion", "not_checkable", [], [], "propulsion unknown")
    bad = {name: items for name, items in by_field.items()
           if (specs.get(name) or {}).get("applies_to") and propulsion not in specs[name]["applies_to"]}
    checked = [name for name in by_field if (specs.get(name) or {}).get("applies_to")]
    if not checked:
        return _check("fields_vs_propulsion", "not_checkable", [], [], "no propulsion-specific field has evidence")
    ids = [str(it.get("evidence_id")) for items in (bad or {n: by_field[n] for n in checked}).values() for it in items]
    return _check("fields_vs_propulsion", "suspicious" if bad else "consistent", sorted(bad or checked), ids,
                  f"propulsion {propulsion}" + (f"; evidence for fields it does not have: {sorted(bad)}" if bad else ""))


def run_checks(evidence: Iterable[dict], payload: dict | None, specs: Iterable[dict] | None = None) -> dict:
    payload = payload or {}
    by_field = _target_items(evidence)
    spec_map: dict[str, dict] = {}
    from .fields import load_schema

    for spec in list(load_schema()) + list(specs or []):
        spec_map[normalize_field_name(spec.get("name"))] = {**spec_map.get(normalize_field_name(spec.get("name")), {}),
                                                            **spec}
    checks = [_co2(by_field, payload), _curb(by_field, payload), _rims(by_field, payload),
              _battery(by_field, payload), _dimensions(by_field, payload), _applicability(by_field, payload, spec_map)]
    by_evidence: dict[str, str] = {}
    for check in checks:
        for eid in check["evidence_ids"]:
            current = by_evidence.get(eid, "not_checkable")
            if SEVERITY[check["status"]] > SEVERITY[current]:
                by_evidence[eid] = check["status"]
    summary = {status: sum(1 for c in checks if c["status"] == status) for status in SEVERITY}
    return {"version": CHECKS_VERSION, "checks": checks, "summary": summary, "by_evidence": by_evidence,
            "note": "QA signals only: never a replacement value, never provenance, never a field state"}


def sanity_status(evidence_id: Any, report: dict | None) -> str:
    return ((report or {}).get("by_evidence") or {}).get(str(evidence_id), "not_checkable")

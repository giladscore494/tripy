"""D-4: the field offers of a match (data/open_datasets.json `field_map`).

Each offer is a fact: {field, source, row_ids, raw (value, unit), value (the field's unit), definition, routes it is
valid for, identified_by (unique | all_survivors_agree), status}. Status:

    offered                  valid for the target's route, its definition is the route's, the match level reaches the
                             entry's min_level and every required key / corroboration holds: admit mode MAY admit it
                             (only for a (source, field, route) triple of data/open_data_admission.json)
    alternative_definition   another definition of the field (CVS curb weight `na_curb` on the european route): an
                             alternative, never a conflict
    reference_only           never portable (another test cycle: EPA / NRCan consumption; SAE luggage volume), or a CVS
                             offer whose measured_year is more than the dataset's measured_year_max_age years before the
                             target year (it may be an older generation)
    below_level              the match level is below the entry's min_level
    missing_key              a required key did not match (co2_wltp for WLTP consumption)
    uncorroborated           CVS dimensions without a second source agreeing on the wheelbase
    survivors_disagree       the surviving configurations state different values (no offer value)
    range                    a Min / Max column pair (ADEME; EEA's min / max around the median) with Min != Max
                             while the match leaves several configurations (no offer value); with one configuration
                             the column's value (ADEME Min, EEA median) is offered and the offer records `range`

A value is identified without a unique candidate when every surviving candidate that states it states the same value
(the PR #45 R4 rule applied to dataset rows; M2: a survivor without the value states nothing and is counted in
`n_null`, never a disagreement). M3: when the match narrowed an `exact_subset` (type code + co2, type code, or co2),
the offers read only those rows. Never across test cycles: no mpg -> l/100km, no 0-60 mph -> 0-100 km/h. Y3 (EEA):
an entry with `row_cycle: wltp` never reads an NEDC row and `row_cycle: nedc` (co2_nedc_g_km, standard NEDC) reads only
NEDC rows (`row_cycle`); the rows it leaves out are counted in `cycle_excluded`, never a disagreement.
"""

from __future__ import annotations

from typing import Any

from . import datasets as ds
from .match import LEVEL_EXACT, transmission_of

LEVELS = ("unknown", "model_family", "generation", "body_powertrain", "exact_technical_variant", "exact_market_trim")


def _level(value: str | None) -> int:
    return LEVELS.index(value) if value in LEVELS else -1


def _value(row: dict, entry: dict) -> Any:
    raw = row.get(entry["column"])
    if raw in (None, ""):
        return None
    transform = entry.get("transform")
    if transform in ("gearbox_type", "gear_count"):
        trans = transmission_of(raw)
        if not trans:
            return None
        return trans.get("gearbox_type") if transform == "gearbox_type" else trans.get("gears")
    try:
        number = float(str(raw).replace(",", ""))
    except ValueError:
        return None
    if number <= 0:
        return None
    number *= float(entry.get("scale") or 1)
    return int(round(number)) if float(number).is_integer() or entry.get("unit") in ("mm", "cm", "kg") \
        else round(number, 1)


def row_cycle(row: dict, config: dict | None = None) -> str | None:
    """Y3: wltp (the row states co2_wltp) | nedc (no co2_wltp, and co2_nedc stated or a year of
    eea_co2_cars.nedc_years) | None (unknown)."""
    if row.get("co2_wltp") not in (None, ""):
        return "wltp"
    config = ds.config() if config is None else config
    through = (((config.get("datasets") or {}).get("eea_co2_cars") or {}).get("nedc_years") or {}).get("through")
    year = ds._int(row.get("year"))
    if row.get("co2_nedc") not in (None, "") or (through is not None and year is not None and year <= int(through)):
        return "nedc"
    return None


def _cycle_rows(rows: list[dict], cycle: str | None) -> list[dict]:
    if cycle == "nedc":
        return [r for r in rows if row_cycle(r) == "nedc"]
    if cycle == "wltp":
        return [r for r in rows if row_cycle(r) != "nedc"]
    return rows


def field_offers(result: dict, field_map: list[dict] | None = None) -> list[dict]:
    """Every field offer of a match result (module docstring)."""
    field_map = ds.config().get("field_map") if field_map is None else field_map
    route = result.get("route") or "unknown"
    level = result.get("level")
    sources = result.get("sources") or {}
    out: list[dict] = []
    for entry in field_map or []:
        src = sources.get(entry.get("source")) or {}
        survivors = src.get("survivors") or []
        if src.get("status") not in ("unique", "ambiguous") or not survivors:
            continue
        subset = set((src.get("exact_subset") or {}).get("row_ids") or [])
        if subset:                                  # M3: the narrowest exact subset (type code + co2 / type code / co2)
            survivors = [r for r in survivors if r.get("row_id") in subset] or survivors
        excluded = len(survivors)
        survivors = _cycle_rows(survivors, entry.get("row_cycle"))   # Y3: never NEDC as WLTP, nor WLTP as NEDC
        excluded -= len(survivors)
        values = [(_value(r, entry), r) for r in survivors]
        stated = [(v, r) for v, r in values if v is not None]
        if not stated:
            continue
        offer = {"field": entry["field"], "source": entry["source"], "column": entry["column"],
                 "definition": entry.get("definition"), "routes": entry.get("routes") or [],
                 "row_ids": [r.get("row_id") for _, r in stated][:20],
                 "raw": {"value": stated[0][1].get(entry["column"]), "unit": entry.get("unit")},
                 "n_null": len(survivors) - len(stated)}
        if subset:
            offer["subset"] = (src.get("exact_subset") or {}).get("basis")
        if excluded:
            offer["cycle_excluded"] = excluded
        distinct = {v for v, _ in stated}
        if len(distinct) != 1:                      # M2: a null states nothing; only different values disagree
            out.append({**offer, "status": "survivors_disagree", "values": sorted(map(str, distinct))[:10]})
            continue
        value = distinct.pop()
        if entry.get("max_column"):
            # Min / Max (ADEME Min / Max columns): Min != Max identifies a single value only when the match leaves one
            # configuration. EEA (`range_policy: equal_only`, the value is an average when MIN != MAX): only MIN = MAX
            lows = {_value(r, {**entry, "column": entry["min_column"]}) for _, r in stated} - {None} \
                if entry.get("min_column") else {value}
            highs = {_value(r, {**entry, "column": entry["max_column"]}) for _, r in stated} - {None}
            low, high = min(lows | {value}), max(highs | {value}) if highs else value
            if low != high:
                offer["range"] = [low, high]
                if entry.get("range_policy") == "equal_only":
                    out.append({**offer, "status": "range", "reason": "MIN != MAX within the configuration "
                                "(the stored value is the registration-weighted median, never offered)"})
                    continue
                if src.get("status") != "unique":
                    out.append({**offer, "status": "range", "reason": "Min != Max and the match leaves "
                                f"{len(src.get('configurations') or []) or 'several'} configurations"})
                    continue
        offer.update(value=value, identified_by="unique" if src.get("status") == "unique" and len(survivors) == 1
                     else "all_survivors_agree")      # every survivor that states the value agrees (n_null: the rest)
        if entry.get("companion"):
            offer["companion"] = entry["companion"]
        # E5: CVS rows record their measurement year (MYR); the oldest one of the offer's rows decides
        measured = sorted({ds._int(r.get("measured_year")) for _, r in stated} - {None})
        max_age = (ds.datasets().get(entry["source"]) or {}).get("measured_year_max_age")
        target_year = ds._int((result.get("keys") or {}).get("year"))
        if measured:
            offer["measured_year"] = measured[0] if len(measured) == 1 else measured
        too_old = bool(measured and max_age is not None and target_year is not None
                       and target_year - measured[0] > int(max_age))
        if entry.get("reference_only") or not entry.get("routes"):
            offer.update(status="reference_only", reason=entry.get("reference_only") or "no route")
        elif too_old:
            offer.update(status="reference_only", reason=f"measured in {measured[0]}, more than {max_age} years before "
                         f"the target year {target_year} (may be an older generation)")
        elif route not in entry["routes"]:
            same_field = [e for e in field_map if e["field"] == entry["field"] and route in (e.get("routes") or [])]
            offer.update(status="alternative_definition" if same_field else "route_mismatch",
                         reason=f"{entry.get('definition')} is not the {route} route's definition")
        elif _level(level) < _level(entry.get("min_level") or LEVEL_EXACT):
            offer.update(status="below_level", reason=f"match level {level}")
        elif entry.get("requires_key") and entry["requires_key"] not in (src.get("keys_matched") or []):
            offer.update(status="missing_key", reason=f"{entry['requires_key']} not matched")
        else:
            offer["status"] = "offered"
        offer["portability_scope"] = entry.get("portability_scope")
        out.append(offer)
    # CVS dimensions only with a second source agreeing on the wheelbase
    wheelbases = {o["source"]: o.get("value") for o in out if o["field"] == "wheelbase_mm" and o.get("value")}
    for offer in out:
        entry = next((e for e in field_map if e["field"] == offer["field"] and e["source"] == offer["source"]
                      and e["column"] == offer["column"]), {})
        corroborate = entry.get("requires_corroboration")
        if corroborate and offer.get("status") == "offered":
            own = wheelbases.get(offer["source"])
            others = {v for s, v in wheelbases.items() if s != offer["source"]}
            if own is None or own not in others:
                offer.update(status="uncorroborated", reason=f"no second source states the same {corroborate}")
    return out

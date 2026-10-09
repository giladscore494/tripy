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
    inconsistent_values      (the facts API only) every survivor's value was dropped by E1 / the row MIN / MAX rule
    range                    a Min / Max column pair (ADEME; EEA's min / max around the median) with Min != Max
                             while the match leaves several configurations (no offer value); with one configuration
                             the column's value (ADEME Min, EEA median) is offered and the offer records `range`

A value is identified without a unique candidate when every surviving candidate that states it states the same value
(the PR #45 R4 rule applied to dataset rows; M2: a survivor without the value states nothing and is counted in
`n_null`, never a disagreement). M3: when the match narrowed an `exact_subset` (type code + co2, type code, or co2),
the offers read only those rows. Never across test cycles: no mpg -> l/100km, no 0-60 mph -> 0-100 km/h. Y3 (EEA):
an entry with `row_cycle: wltp` never reads an NEDC row and `row_cycle: nedc` (co2_nedc_g_km, standard NEDC) reads only
NEDC rows (`row_cycle`); the rows it leaves out are counted in `cycle_excluded`, never a disagreement.

The facts API (field_offers(..., agreement=data/facts_admission.json `agreement`); a run's offers keep the exact rules):
E1 drops a survivor's consumption value outside its fuel's CO2 / consumption band (data/open_datasets.json
`consistency_bands`), the row MIN / MAX rule drops a value whose own MIN / MAX is wider than the field's precision
(more than row_range_max_share of the survivors: `range`), and E2 lets the remaining values agree within the field's
precision (value: the lower median, rounded; the offer carries `agreement` and the agreeing `values`). The dropped
values are listed in the offer's `dropped` {reason: [{row_id, value, ...}]}.
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
    if number < 0 or (number == 0 and not entry.get("zero_is_value")):
        return None                                 # `zero_is_value`: a 0 is a value (NEDC CO2 of a battery-electric)
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


# --- the facts API's survivor rules (E1 / E2: field_offers(..., agreement=...); a run's offers never use them) --------

DROP_INCONSISTENT = "inconsistent_co2_consumption"
DROP_ROW_RANGE = "row_range_dropped"
EPSILON = 1e-9


def lower_median(values: list[float]) -> float:
    """The median of one value per configuration row, the lower one of an even count (the build's weighted-median rule
    with every weight 1: deterministic, always a stated value)."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def round_to(value: float, step: Any) -> int | float:
    """value rounded half up to a multiple of step (0.1 -> one decimal, 5 -> a multiple of 5 kg)."""
    step = float(step)
    places = max(0, -int(f"{step:e}".split("e")[1])) if step < 1 else 0
    rounded = float(int(value / step + 0.5 + EPSILON) * step) if value >= 0 else -round_to(-value, step)
    rounded = round(rounded, places)
    return int(rounded) if float(rounded).is_integer() else rounded


def consistency_band(row: dict, entry: dict, bands: dict | None) -> tuple[float, float, float] | None:
    """E1: (low, high, the row's own CO2) of the CO2 / consumption ratio band for this row, or None (no filter: another
    field or source, a hybrid / plug-in fuel_mode, a fuel without a band, no CO2 on the row). The CO2 is the row's WLTP
    CO2, or its NEDC CO2 for an NEDC row."""
    if not bands or entry.get("field") not in (bands.get("fields") or []) \
            or entry.get("source") not in (bands.get("sources") or []):
        return None
    if str(row.get("fuel_mode") or "").strip().upper() in {str(m).upper() for m in bands.get("skip_fuel_modes") or []}:
        return None
    band = (bands.get("bands") or {}).get(" ".join(str(row.get("fuel") or "").split()).upper())
    if not isinstance(band, list) or len(band) != 2:
        return None
    column = (bands.get("co2_columns") or {}).get("nedc" if row_cycle(row) == "nedc" else "wltp")
    co2 = ds._number(row.get(column)) if column else None
    if co2 is None or co2 <= 0:
        return None
    return float(band[0]), float(band[1]), float(co2)


def _in_band(value: Any, band: tuple[float, float, float]) -> bool:
    return isinstance(value, (int, float)) and value > 0 and band[0] - EPSILON <= band[2] / value <= band[1] + EPSILON


def facts_rows(stated: list[tuple[Any, dict]], entry: dict, rule: dict, bands: dict | None
               ) -> tuple[list[tuple[Any, dict]], dict[str, list[dict]]]:
    """E1 + the row MIN / MAX rule: (the rows that keep their value, {drop reason: [{row_id, value, ...}]}).

    inconsistent_co2_consumption   the row's value is outside its fuel's CO2 / consumption band
    row_range_dropped              (a field with `row_range`, range_policy equal_only) the row's own MIN or MAX is
                                   outside the band, or MAX - MIN is wider than row_range
    A dropped row loses only this field's value; its other fields stay."""
    kept: list[tuple[Any, dict]] = []
    dropped: dict[str, list[dict]] = {}
    row_range = rule.get("row_range")
    ranged = row_range is not None and entry.get("range_policy") == "equal_only" and entry.get("max_column")
    for value, row in stated:
        band = consistency_band(row, entry, bands)
        item = {"row_id": row.get("row_id"), "value": value}
        if band is not None and not _in_band(value, band):
            dropped.setdefault(DROP_INCONSISTENT, []).append({**item, "co2": band[2]})
            continue
        if ranged:
            low = _value(row, {**entry, "column": entry["min_column"]}) if entry.get("min_column") else None
            high = _value(row, {**entry, "column": entry["max_column"]})
            low, high = value if low is None else low, value if high is None else high
            if (band is not None and not (_in_band(low, band) and _in_band(high, band))) \
                    or float(high) - float(low) > float(row_range) + EPSILON:
                dropped.setdefault(DROP_ROW_RANGE, []).append({**item, "range": [low, high]})
                continue
        kept.append((value, row))
    return kept, dropped


def within_precision(values: list[Any], rule: dict) -> dict | None:
    """E2: {value, agreement} when the spread of these (numeric) values is within the rule's precision (`spread`, and
    `spread_pct` of the median when set); None otherwise. The value: the lower median, rounded to `round`."""
    if not rule or rule.get("spread") is None or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                                                     for v in values):
        return None
    spread = round(float(max(values)) - float(min(values)), 6)
    median = lower_median([float(v) for v in values])
    if spread > float(rule["spread"]) + EPSILON:
        return None
    if rule.get("spread_pct") is not None and spread > float(rule["spread_pct"]) / 100.0 * abs(median) + EPSILON:
        return None
    value = round_to(median, rule["round"]) if rule.get("round") is not None else \
        (int(median) if float(median).is_integer() else median)
    spread_out = int(spread) if float(spread).is_integer() else spread
    return {"value": value, "agreement": {"rule": "within_precision", "spread": spread_out,
                                          "n_values": len(set(values))}}


def field_offers(result: dict, field_map: list[dict] | None = None, agreement: dict | None = None) -> list[dict]:
    """Every field offer of a match result (module docstring). `agreement` (the facts API only: data/facts_admission.json
    `agreement`) turns on E1 (data/open_datasets.json `consistency_bands`), the row MIN / MAX rule and the E2
    within-precision agreement for the sources and fields it names; None (a run) keeps the exact rules."""
    field_map = ds.config().get("field_map") if field_map is None else field_map
    bands = ds.config().get("consistency_bands") if agreement is not None else None
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
        n_null = len(survivors) - len(stated)
        rule = ((agreement or {}).get("fields") or {}).get(entry["field"]) or {} \
            if entry.get("source") in ((agreement or {}).get("sources") or []) else {}
        dropped: dict[str, list[dict]] = {}
        if agreement is not None:
            stated, dropped = facts_rows(stated, entry, rule, bands)
        base = {"field": entry["field"], "source": entry["source"], "column": entry["column"],
                "definition": entry.get("definition"), "routes": entry.get("routes") or []}
        if dropped:
            base["dropped"] = dropped
        if not stated:                              # every stated value was dropped (E1 / the row MIN / MAX rule)
            out.append({**base, "row_ids": [], "n_null": n_null, "status": "inconsistent_values",
                        "reason": "every survivor's value was dropped (" + ", ".join(sorted(dropped)) + ")"})
            continue
        offer = {**base, "row_ids": [r.get("row_id") for _, r in stated][:20],
                 "raw": {"value": stated[0][1].get(entry["column"]), "unit": entry.get("unit")}, "n_null": n_null}
        if subset:
            offer["subset"] = (src.get("exact_subset") or {}).get("basis")
        if excluded:
            offer["cycle_excluded"] = excluded
        ranged_out = len(dropped.get(DROP_ROW_RANGE) or [])
        share = float(rule.get("row_range_max_share", 0.25))
        if ranged_out and ranged_out > share * len(survivors) + EPSILON:
            out.append({**offer, "status": "range", "reason": f"{ranged_out} of {len(survivors)} survivors have MIN != MAX "
                        f"beyond the precision (more than {share:.0%})"})
            continue
        distinct = {v for v, _ in stated}
        agreed = None
        if len(distinct) != 1:                      # M2: a null states nothing; only different values disagree
            agreed = within_precision([v for v, _ in stated], rule) if rule else None
            if agreed is None:
                out.append({**offer, "status": "survivors_disagree", "values": sorted(map(str, distinct))[:10]})
                continue
            value = agreed["value"]
            offer["agreement"] = agreed["agreement"]
            offer["values"] = sorted(map(str, distinct))[:10]      # what agreed (the co2_selection diagnostic)
        else:
            value = distinct.pop()
        if entry.get("max_column") and agreement is not None and entry.get("range_policy") == "equal_only":
            # the facts API: each configuration's own MIN / MAX (a row kept under `row_range` counts with its value)
            if rule.get("row_range") is None and any(
                    (_value(r, {**entry, "column": entry["min_column"]}) if entry.get("min_column") else None)
                    not in (None, v) or _value(r, {**entry, "column": entry["max_column"]}) not in (None, v)
                    for v, r in stated):
                lows = [_value(r, {**entry, "column": entry["min_column"]}) for _, r in stated] \
                    if entry.get("min_column") else []
                highs = [_value(r, {**entry, "column": entry["max_column"]}) for _, r in stated]
                offer["range"] = [min([x for x in lows if x is not None] + [v for v, _ in stated]),
                                  max([x for x in highs if x is not None] + [v for v, _ in stated])]
                out.append({**offer, "status": "range", "reason": "MIN != MAX within the configuration "
                            "(the stored value is the registration-weighted median, never offered)"})
                continue
        elif entry.get("max_column"):
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

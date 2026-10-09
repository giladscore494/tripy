"""The `vehicle-facts/1` record, pure and deterministic (no I/O beyond the snapshots the match reads).

Government facts (the Level 1.5 row; source `government`): the canonical names yeda-rechev's
level15.GOVERNMENT_FACT_FIELDS and its 19 ADAS flags use (`adas.<flag>`). Only a field with a value appears: a null
column is left out, and a 0 is left out when data/facts_zero_semantics.json says the field's 0 means `unknown` (for
engine_cc a 0 is the value only for a battery-electric car: `value_when`). An ADAS
flag appears only when decode_equipment reports it as stated (equipment_stated); a flag that is not stated is unknown.

Open-data facts (source = the dataset; data/facts_admission.json):

    1. an offer of src/open_data/offers.py with status `offered` whose (source, field, route) has an entry, at a match
       level >= the entry's min_level, every `require` holding (list, or per target cycle wltp / nedc)
    2. never: the admission file's `never` rules (EPA cargo, EPA / NRCan consumption, an American-source gearbox or CVS
       dimensions for a European target, an ADEME catalogue out of period), whatever an entry says
    3. government wins: a field the government record has is never returned from open data (equal: government_wins,
       different: contradicts_government); a survivor row stating a government-overlap column (power, displacement,
       co2_wltp, seats) with another value is counted as contradicts_government
    E1 / E2 (src/open_data/offers.py, data/facts_admission.json `agreement`, data/open_datasets.json
       `consistency_bands`): before agreement a survivor's consumption value outside its fuel's CO2 / consumption band
       is dropped (inconsistent_co2_consumption), and so is one whose own MIN / MAX is wider than the field's precision
       (row_range_dropped); survivors then agree within the field's precision (the fact's value: the lower median,
       rounded; `agreement` {rule, spread, n_values} on the fact when the spread is not 0). Each drop is a withheld entry
    E3: a WLTP-year target without a government co2_wltp reads an entry's `wltp_no_gov_co2` requirements (curb weight:
       type_code_match instead of co2_selected; basis `type_code_match (no government CO2)`); every other entry keeps
       its `wltp` list (consumption keeps co2_selected)
    4. two open sources for one field: every other `offered` offer of the field equal within D1 (a number stated in
       the field's own unit equals only the same number; a converted one, e.g. CVS cm -> mm, within half its source
       step) -> the admitted fact is returned with the others as `corroborated_by`; any different -> neither
       (source_conflict)

Government-dataset facts (source_level `government_dataset`; src/gov_data/facts.py, the data/gov/ snapshots):
original_new_price_ils, original_importer, recalls, recall_count, road_survival. They never replace a Level 1.5 field
(no name is shared) and win over an open-data fact of the same name.

Everything that does not become a fact is listed in `withheld` ({source, field, reason, ...}): owner diagnostics only
(?debug=1 with the operator token, the MCP facts_preview, counts in the call log), never in the yeda-rechev record.
A withheld entry of a European source with reason offer_survivors_disagree, or one whose unmet requirement is
co2_selected, carries `co2_selection` (what the match's CO2 step did, and the values it left per admitted field); the
debug record lists every source's `co2_selection`. Diagnostics only: nothing here decides a fact.
"""

from __future__ import annotations

from typing import Any

from . import CONTRACT
from . import versions as V

LEVELS = ("unknown", "model_family", "generation", "body_powertrain", "exact_technical_variant", "exact_market_trim")
GOVERNMENT_LEVEL = "exact_market_trim"
GOVERNMENT_POLICY = "data.gov.il"

# (canonical name, Level 1.5 column, kind, unit, standard)
GOVERNMENT_FIELDS: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("fuel_type", "norm_fuel_type", "text", None, None),
    ("propulsion", "norm_propulsion_technology", "text", None, None),
    ("drivetrain", "norm_drivetrain", "text", None, None),
    ("body_style", "norm_body_style", "text", None, None),
    ("engine_cc", "nefah_manoa", "number", "cc", None),
    ("horsepower", "koah_sus", "number", "hp", None),
    ("automatic", "automatic_ind", "bool", None, None),
    ("doors", "mispar_dlatot", "number", "count", None),
    ("seats", "mispar_moshavim", "number", "count", None),
    ("gross_weight_kg", "mishkal_kolel", "number", "kg", None),
    ("towing_braked_kg", "kosher_grira_im_blamim", "number", "kg", None),
    ("towing_unbraked_kg", "kosher_grira_bli_blamim", "number", "kg", None),
    ("co2_wltp", "co2_wltp", "number", "g/km", "WLTP"),
    ("nox_wltp", "nox_wltp", "number", "mg/km", "WLTP"),
    ("co_wltp", "co_wltp", "number", "mg/km", "WLTP"),
    ("hc_wltp", "hc_wltp", "number", "mg/km", "WLTP"),
    ("co2_city", "kamut_co2_city", "number", "g/km", None),
    ("co2_highway", "kamut_co2_hway", "number", "g/km", None),
    ("pollution_group", "kvutzat_zihum", "number", None, None),
    ("green_index", "madad_yarok", "number", None, None),
    ("safety_score", "nikud_betihut", "number", None, None),
    ("safety_equipment_level", "ramat_eivzur_betihuty", "number", None, None),
    ("airbags", "mispar_kariot_avir", "number", "count", None),
    ("abs", "abs_ind", "bool", None, None),
    ("esc", "bakarat_yatzivut_ind", "bool", None, None),
)
SUBSET_BASIS = {"type_code+co2": "type_code_match+co2", "type_code": "type_code_match", "co2": "co2_match"}
NO_GOV_CO2_CYCLE = "wltp_no_gov_co2"                     # E3: a WLTP-year target whose government row has no co2_wltp
NO_GOV_CO2_BASIS = "type_code_match (no government CO2)"
DROP_REASONS = ("inconsistent_co2_consumption", "row_range_dropped")


def level_index(level: Any) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(number) if number.is_integer() else number


def _clean(fact: dict) -> dict:
    """A fact without empty keys: never a null, an empty list or a placeholder."""
    return {k: v for k, v in fact.items() if v not in (None, "", [], {})}


# --- government -------------------------------------------------------------------------------------------------------

def _government_provenance(row: dict) -> dict:
    from ..source_authority import dataset_policy

    policy = dataset_policy(GOVERNMENT_POLICY)
    return {"source": "government", "source_level": "government", "identity_level": GOVERNMENT_LEVEL,
            "basis": "variant_identity_key", "row_ids": [str(row.get("upstream_record_id"))],
            "licence": policy.get("licence"), "attribution": policy.get("attribution")}


def _zero_is_value(rule: dict, row: dict) -> bool:
    """A 0 is the value under `zero_means: value`, or under `value_when` when the row's own government field has one
    of the listed values (engine_cc: battery_electric); otherwise it is unknown."""
    if rule.get("zero_means", "unknown") == "value":
        return True
    columns = {name: column for name, column, *_ in GOVERNMENT_FIELDS}
    for field, allowed in (rule.get("value_when") or {}).items():
        raw = row.get(columns.get(field, field))
        if raw is not None and str(raw).strip() in {str(a) for a in allowed}:
            return True
    return False


def government_facts(row: dict, zero: dict | None = None) -> tuple[dict[str, dict], list[dict]]:
    """({name: fact}, withheld) of the Level 1.5 row under the zero semantics (module docstring)."""
    from ..db import EQUIPMENT_BITS, decode_equipment

    zero = V.zero_semantics() if zero is None else zero
    rules = zero.get("fields") or {}
    base = _government_provenance(row)
    facts: dict[str, dict] = {}
    withheld: list[dict] = []
    for name, column, kind, unit, standard in GOVERNMENT_FIELDS:
        raw = row.get(column)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue                                        # nothing stated: the field is absent
        if kind == "text":
            value: Any = str(raw).strip()
        else:
            value = _number(raw)
            if value is None:
                continue
            if value == 0 and not _zero_is_value(rules.get(name) or {}, row):
                withheld.append({"source": "government", "field": name, "reason": "zero_means_unknown"})
                continue
            if kind == "bool":
                value = bool(value)
        facts[name] = _clean({**base, "value": value, "unit": unit, "standard": standard})
    stated = decode_equipment(row.get("equipment_stated"), row.get("equipment_on"), row.get("equipment_sources"))
    for key, _, _ in EQUIPMENT_BITS:
        if key in stated:                                   # stated (equipment_stated): installed or not
            facts[f"adas.{key}"] = _clean({**base, "value": bool(stated[key])})
        else:
            withheld.append({"source": "government", "field": f"adas.{key}", "reason": "adas_not_stated"})
    return facts, withheld


def identity_of(row: dict) -> dict:
    """The record's identity block (the government row's own values; empty ones left out)."""
    from ..gov_registry import approval_route

    automatic = _number(row.get("automatic_ind"))
    identity = {"manufacturer": row.get("tozar"), "model": row.get("kinuy_mishari"),
                "model_year": _number(row.get("shnat_yitzur")), "trim": row.get("ramat_gimur"),
                "degem_nm": row.get("degem_nm"),
                "sug_tkina": approval_route(row.get("sug_tkina_cd"), row.get("sug_tkina_nm")),
                "body_style": row.get("norm_body_style"), "fuel_type": row.get("norm_fuel_type"),
                "propulsion": row.get("norm_propulsion_technology"), "drivetrain": row.get("norm_drivetrain"),
                "engine_cc": _number(row.get("nefah_manoa")), "horsepower": _number(row.get("koah_sus")),
                "automatic": bool(automatic) if automatic is not None else None,
                "upstream_record_id": str(row["upstream_record_id"]) if row.get("upstream_record_id") else None}
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in identity.items()
            if v is not None and not (isinstance(v, str) and not v.strip())}


# --- open data --------------------------------------------------------------------------------------------------------

def _field_map_entry(offer: dict, field_map: list[dict]) -> dict:
    return next((e for e in field_map if e.get("source") == offer.get("source") and e.get("field") == offer.get("field")
                 and e.get("column") == offer.get("column")), {})


def _half_step(entry: dict) -> float:
    """D1: half the source's last stated unit in the field's unit (CVS cm -> mm: 5); 0 for the field's own unit."""
    scale = _number(entry.get("scale"))
    return 0.5 * float(scale) if scale and scale > 1 else 0.0


def _never(offer: dict, route: str, sources: dict, rules: list[dict]) -> str | None:
    for rule in rules:
        if offer.get("source") not in (rule.get("sources") or []):
            continue
        if rule.get("fields") and offer.get("field") not in rule["fields"]:
            continue
        if rule.get("routes") and route not in rule["routes"]:
            continue
        status = rule.get("catalogue_status")
        if status and ((sources.get(offer["source"]) or {}).get("catalogue") or {}).get("status") != status:
            continue
        return str(rule.get("reason") or "never admitted")
    return None


def _requirement(name: str, offer: dict, src: dict) -> bool:
    if name == "all_survivors_agree":
        return offer.get("value") is not None and offer.get("identified_by") in ("unique", "all_survivors_agree")
    if name == "co2_selected":
        return (src.get("exact_subset") or {}).get("basis") in ("type_code+co2", "co2")
    if name == "type_code_match":
        return (src.get("type_code") or {}).get("status") == "match" and src.get("status") in ("unique", "ambiguous")
    return False                                            # an unknown requirement never holds


def _requires(entry: dict, cycle: str) -> tuple[list[str], str]:
    """(the entry's requirements for the target cycle, the cycle they were read for). E3: an entry without a
    `wltp_no_gov_co2` list uses its `wltp` list for that cycle."""
    require = entry.get("require") or []
    if not isinstance(require, dict):
        return [str(r) for r in require], "any"
    if cycle not in require and cycle == NO_GOV_CO2_CYCLE:
        cycle = "wltp"
    return [str(r) for r in require.get(cycle) or []], cycle


def _offer_rows(offer: dict, src: dict) -> list[dict]:
    ids = set(offer.get("row_ids") or [])
    return [r for r in src.get("survivors") or [] if r.get("row_id") in ids]


def _years(rows: list[dict]) -> list[int]:
    from ..open_data import datasets as ds

    return sorted({ds._int(r.get("year")) for r in rows} - {None})


def _provenance(offer: dict, src: dict, result: dict) -> dict:
    from ..open_data import datasets as ds
    from ..source_authority import attribution_for, dataset_policy

    dataset = ds.datasets().get(offer["source"]) or {}
    policy = dataset_policy(dataset.get("policy_id") or offer["source"])
    years = _years(_offer_rows(offer, src))
    catalogue = src.get("catalogue") or {}
    date = (catalogue.get("catalogue_dates") or [catalogue.get("catalogue_date")] or [None])[0]
    built = str(((ds.manifest().get("datasets") or {}).get(offer["source"]) or {}).get("built_at") or "")
    year_text = (str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}") if years else (built[:4] or "n/a")
    return {"source": offer["source"], "source_level": "open_data", "identity_level": result.get("level"),
            "basis": SUBSET_BASIS.get(offer.get("subset") or "") or offer.get("identified_by"),
            "row_ids": [str(r) for r in offer.get("row_ids") or []],
            "dataset_year": (years[0] if len(years) == 1 else years) if years else None,
            "licence": policy.get("licence"),
            "attribution": attribution_for(policy.get("entry"), year=year_text,
                                           date=str(date or built)[:10] or "n/a")}


def _equal(a: Any, b: Any, tolerance: float) -> bool:
    na, nb = _number(a), _number(b)
    if na is None or nb is None or isinstance(a, str) and _number(a) is None:
        return a == b
    return abs(float(na) - float(nb)) <= tolerance + 1e-9


def _government_matches(check: dict, stated: Any, government: Any) -> bool:
    compare = check.get("compare")
    if compare == "power_anchor":
        from ..document_binding import power_anchor

        anchor = power_anchor(_number(government), _number(stated), "kw")
        return anchor is None or bool(anchor["match"])
    a, b = _number(stated), _number(government)
    if a is None or b is None:
        return True
    if compare == "relative":
        return abs(a - b) <= float(check.get("tolerance") or 0) * abs(b) + 1e-9
    return a == b


def government_contradictions(result: dict, government: dict, checks: list[dict]) -> list[dict]:
    """contradicts_government entries: a matched source's survivor rows (its exact subset when narrowed) stating a
    government-overlap column with another value than the government record. Never returned as a fact either way."""
    out = []
    for name, src in sorted((result.get("sources") or {}).items()):
        survivors = src.get("survivors") or []
        if src.get("status") not in ("unique", "ambiguous") or not survivors:
            continue
        subset = set((src.get("exact_subset") or {}).get("row_ids") or [])
        rows = [r for r in survivors if r.get("row_id") in subset] or survivors if subset else survivors
        for check in checks:
            gov = (government.get(check.get("government_field")) or {}).get("value")
            if gov is None:
                continue
            values = sorted({str(r[check["column"]]) for r in rows if r.get(check["column"]) not in (None, "")
                             and not _government_matches(check, r[check["column"]], gov)})
            if values:
                out.append({"source": name, "field": check["column"], "reason": "contradicts_government",
                            "government_field": check["government_field"], "government": gov, "values": values})
    return out


def open_data_facts(result: dict, offers: list[dict], government: dict, adm: dict | None = None,
                    field_map: list[dict] | None = None) -> dict:
    """{facts, open_data_match, withheld} of a match result and its offers (module docstring)."""
    from ..open_data import datasets as ds
    adm = V.admission() if adm is None else adm
    field_map = ds.config().get("field_map") or [] if field_map is None else field_map
    route = result.get("route") or "unknown"
    level = result.get("level")
    sources = result.get("sources") or {}
    cycle = "nedc" if result.get("nedc_target") else \
        NO_GOV_CO2_CYCLE if result.get("co2_key") == "unavailable" else "wltp"    # Y3: NEDC year; E3: no gov CO2
    entries = [e for e in adm.get("entries") or [] if isinstance(e, dict)]
    field_units = adm.get("fields") or {}
    withheld: list[dict] = []
    admitted: list[tuple[int, dict]] = []
    no_gov_co2: set[int] = set()                                        # E3: offers admitted under wltp_no_gov_co2

    def hold(offer: dict, reason: str, **extra: Any) -> None:
        item = {"source": offer.get("source"), "field": offer.get("field"), "reason": reason, **extra}
        if offer.get("value") is not None:
            item["value"] = offer["value"]
        elif offer.get("values"):
            item["values"] = offer["values"]
        withheld.append(item)

    for offer in offers:
        for reason in DROP_REASONS:                 # E1 / the row MIN / MAX rule: one entry per dropped value (debug)
            for item in (offer.get("dropped") or {}).get(reason) or []:
                withheld.append(_clean({"source": offer.get("source"), "field": offer.get("field"), "reason": reason,
                                        **{k: item.get(k) for k in ("row_id", "value", "co2", "range")}}))
        never = _never(offer, route, sources, adm.get("never") or [])
        if never:
            hold(offer, "never_admitted", detail=never)
            continue
        if offer.get("status") != "offered":
            hold(offer, f"offer_{offer.get('status')}", **({"detail": offer["reason"]} if offer.get("reason") else {}))
            continue
        index, entry = next(((n, e) for n, e in enumerate(entries)
                             if e.get("source") == offer.get("source") and e.get("field") == offer.get("field")
                             and e.get("route") == route
                             and (not e.get("definition") or e["definition"] == offer.get("definition"))),
                            (-1, None))
        if entry is None:
            hold(offer, "not_admitted")
            continue
        if level_index(level) < level_index(entry.get("min_level") or "exact_technical_variant"):
            hold(offer, "below_admission_level", detail=f"match level {level}, entry min_level {entry.get('min_level')}")
            continue
        requires, used = _requires(entry, cycle)
        failed = [r for r in requires if not _requirement(r, offer, sources.get(offer["source"]) or {})]
        if failed:
            hold(offer, "requirement_unmet", detail=failed)
            continue
        if used == NO_GOV_CO2_CYCLE:
            no_gov_co2.add(id(offer))
        gov = (government.get(offer["field"]) or {}).get("value")
        if gov is not None:
            hold(offer, "government_wins" if _equal(offer.get("value"), gov, 0.0) else "contradicts_government",
                 government=gov)
            continue
        admitted.append((index, offer))
    withheld += government_contradictions(result, government,
                                          [c for c in (adm.get("government_overlap") or {}).get("checks") or []
                                           if isinstance(c, dict) and c.get("column") and c.get("government_field")])
    facts: dict[str, dict] = {}
    for field in sorted({o["field"] for _, o in admitted}):
        mine = sorted([(n, o) for n, o in admitted if o["field"] == field], key=lambda item: item[0])
        chosen = mine[0][1]
        chosen_entry = _field_map_entry(chosen, field_map)
        peers = [o for o in offers if o.get("field") == field and o.get("source") != chosen["source"]
                 and o.get("status") == "offered" and o.get("value") is not None]
        differing = [o for o in peers if not _equal(o["value"], chosen["value"],
                                                     _half_step(chosen_entry) + _half_step(_field_map_entry(o, field_map)))]
        if differing:
            values = sorted({f"{o['source']}={o['value']}" for o in [chosen, *peers]})
            for _, offer in mine:
                hold(offer, "source_conflict", detail=values)
            continue
        for _, offer in mine[1:]:
            hold(offer, "duplicate_source", detail=f"{chosen['source']} returned")
        src = sources.get(chosen["source"]) or {}
        companion = chosen.get("companion") or {}
        unit_spec = field_units.get(field) or {}
        provenance = _provenance(chosen, src, result)
        if id(chosen) in no_gov_co2:
            provenance["basis"] = NO_GOV_CO2_BASIS
        fact = {"value": chosen["value"], "unit": unit_spec.get("unit"),
                "standard": unit_spec.get("standard") or next(iter(companion.values()), None),
                "definition": chosen.get("definition"), **provenance, "agreement": chosen.get("agreement"),
                "corroborated_by": [_clean({"source": o["source"], "value": o["value"],
                                            "row_ids": [str(r) for r in o.get("row_ids") or []]})
                                    for o in sorted(peers, key=lambda o: o["source"])]}
        facts[field] = _clean(fact)
    match_view = _clean({"route": route, "level": level, "designation": result.get("designation")})
    selections = co2_selections(result, offers, entries)
    for item in withheld:
        detail = item.get("detail") if isinstance(item.get("detail"), list) else []
        if item.get("source") in selections and (item.get("reason") == "offer_survivors_disagree"
                                                 or "co2_selected" in detail):
            item["co2_selection"] = dict(selections[item["source"]])
    withheld.sort(key=lambda w: (str(w.get("source")), str(w.get("field")), str(w.get("reason"))))
    out = {"facts": facts, "open_data_match": match_view, "withheld": withheld}
    if selections:
        out["co2_selection"] = selections
    return out


def co2_selections(result: dict, offers: list[dict], entries: list[dict]) -> dict[str, dict]:
    """{source: the match's co2_selection + selected_values} (diagnostics only: the debug / MCP withheld detail and
    facts_coverage, never the public record). selected_values: per admitted field of the source, the values the offers
    read after the CO2 step (the offer's value, or its disagreeing values; at most the match's 40 survivor rows)."""
    out: dict[str, dict] = {}
    for name, src in sorted((result.get("sources") or {}).items()):
        selection = src.get("co2_selection")
        if not isinstance(selection, dict):
            continue
        fields = {e.get("field") for e in entries if e.get("source") == name}
        values: dict[str, list] = {}
        for offer in offers:
            if offer.get("source") != name or offer.get("field") not in fields:
                continue
            stated = [_number(v) if _number(v) is not None else v for v in offer.get("values") or []] or \
                ([offer["value"]] if offer.get("value") is not None else [])     # agreed / disagreeing: every value
            if stated:
                values[offer["field"]] = sorted(set(stated), key=lambda v: (isinstance(v, str),
                                                                            v if isinstance(v, str) else float(v)))
        out[name] = {**selection, "selected_values": dict(sorted(values.items()))}
    return out


def open_data_part(row: dict, government: dict, *, folder=None, rows_by_source: dict | None = None) -> dict:
    """The open-data half of a record: the match of the government row against the snapshots (no vPIC, no network),
    its offers and the admission. {facts, open_data_match, withheld, skipped_shards}."""
    from ..db import build_level15_payload
    from ..gov_registry import identity_fingerprint
    from ..open_data.match import match
    from ..open_data.offers import field_offers

    payload = build_level15_payload(row)
    result = match(identity_fingerprint(payload), payload, folder=folder, rows_by_source=rows_by_source)
    part = open_data_facts(result, field_offers(result, agreement=V.admission().get("agreement") or {}), government)
    skipped = sorted({str(s.get("problem")) for src in result["sources"].values() for s in src.get("skipped_shards") or []})
    if skipped:
        part["skipped_shards"] = skipped
    return part


def build_record(key: str, row: dict, open_part: dict, *, debug: bool = False,
                 government: tuple[dict, list] | None = None) -> dict:
    """The `vehicle-facts/1` record of one variant (government facts recomputed on every call; the open part as
    cached). debug adds `withheld` (+ counts by reason)."""
    gov_facts, gov_withheld = government if government is not None else government_facts(row)
    dataset_facts = open_part.get("gov_facts") or {}
    facts = {**{k: v for k, v in (open_part.get("facts") or {}).items() if k not in gov_facts and k not in dataset_facts},
             **{k: v for k, v in dataset_facts.items() if k not in gov_facts}, **gov_facts}
    record = {"variant_identity_key": key, "status": "ok", "identity": identity_of(row), "facts": facts,
              "open_data_match": open_part.get("open_data_match") or {}, "versions": V.versions()}
    if debug:
        withheld = sorted([*gov_withheld, *(open_part.get("withheld") or []), *(open_part.get("gov_withheld") or [])],
                          key=lambda w: (str(w.get("source")), str(w.get("field")), str(w.get("reason"))))
        record["withheld"] = withheld
        record["withheld_counts"] = withheld_counts(withheld)
        if open_part.get("co2_selection"):
            record["co2_selection"] = open_part["co2_selection"]        # every CO2-step decision, per source
    return record


def withheld_counts(withheld: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in withheld:
        counts[str(item.get("reason"))] = counts.get(str(item.get("reason")), 0) + 1
    return dict(sorted(counts.items()))


def not_found(key: str) -> dict:
    return {"variant_identity_key": key, "status": "not_found", "http_status": 404, "versions": {"contract": CONTRACT}}

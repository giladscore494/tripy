"""The government-dataset fields of a `vehicle-facts/1` record (src/facts), from the committed snapshots in data/gov/.

Every field carries source, source_level "government_dataset", licence (as the package states it), attribution,
resource_id, identity_level and basis. Only a field with a value appears; everything else is a `withheld` reason
(owner diagnostics only).

    original_new_price_ils   key (tozeret_cd, degem_cd, shnat_yitzur). One distinct price -> {value, unit ILS,
                             label_he}; several (trims or updates: the list has neither) -> ONLY {range: [min, max],
                             n_prices}, never one value. The only field allowed a `range` instead of a `value`. A list
                             price of a new car, never a transaction price.
    original_importer        the key's shem_yevuan when it is the only one
    recalls / recall_count   only for a RESOLVED model: its canonical make and kinuy_mishari are in the reviewed map
                             (data/gov/recall_model_map.json) and every matched notice states its production range.
                             {value: [{recall_id, recall_year, affected_system, fault_description, repair_method,
                             production_range}]}, recall_count {value: n}. An unresolved model gets NO recalls field
                             (never []); a resolved model with no notice in its year gets recall_count 0. Never a score.
    road_survival            {value: {cohort_size, cancelled_share_by_age, median_age_at_final_cancellation,
                             final_cancellation_rate, definition_he, cohort_basis, cohort_year}} of the cohort of the
                             variant's model year (directly, or through a dominant first-road year); none for a cohort
                             under the minimum size. The reason for a cancellation is unknown: never "reliability".
"""

from __future__ import annotations

import json
from . import SOURCE_LEVEL, SOURCES
from . import recalls as G2
from . import snapshot as SN
from . import survival as G3
from .names import catalogue_makes, norm_model, to_int

PRICE_LABEL_HE = "מחיר מחירון חדש מקורי"
FIELDS = ("original_new_price_ils", "original_importer", "recalls", "recall_count", "road_survival")


def _clean(fact: dict) -> dict:
    return {k: v for k, v in fact.items() if v not in (None, "", {})}


def _provenance(dataset: str, entry: dict, identity_level: str, basis: str) -> dict:
    ids = [r.get("resource_id") for r in entry.get("resources") or [] if r.get("resource_id")]
    return {"source": SOURCES[dataset], "source_level": SOURCE_LEVEL, "licence": entry.get("licence"),
            "attribution": entry.get("attribution"), "resource_id": ids[0] if len(ids) == 1 else ids,
            "identity_level": identity_level, "basis": basis, "dataset_built_at": str(entry.get("built_at") or "")[:10]}


def _entry(dataset: str) -> dict:
    return (SN.manifest().get("datasets") or {}).get(dataset) or {}


def price_facts(row: dict) -> tuple[dict, list[dict]]:
    dataset = "new_car_prices"
    key = (to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd")), to_int(row.get("shnat_yitzur")))
    path = SN.materialize(dataset)
    if path is None:
        return {}, [{"source": SOURCES[dataset], "field": "original_new_price_ils", "reason": "gov_snapshot_missing"}]
    if None in key:
        return {}, [{"source": SOURCES[dataset], "field": "original_new_price_ils", "reason": "no_government_key"}]
    rows = SN.query(path, "SELECT mehir, shem_yevuan, rows FROM prices WHERE tozeret_cd = ? AND degem_cd = ? AND "
                          "shnat_yitzur = ?", key)
    if not rows:
        return {}, [{"source": SOURCES[dataset], "field": "original_new_price_ils", "reason": "not_in_price_list"}]
    base = _provenance(dataset, _entry(dataset), "model_year", "tozeret_cd+degem_cd+shnat_yitzur")
    prices = sorted({int(r["mehir"]) for r in rows})
    price = {**base, "unit": "ILS", "label_he": PRICE_LABEL_HE, "price_type": "new_car_list_price"}
    if len(prices) == 1:
        price["value"] = prices[0]
    else:
        price.update(range=[prices[0], prices[-1]], n_prices=len(prices))
    facts = {"original_new_price_ils": _clean(price)}
    withheld = []
    importers = sorted({r["shem_yevuan"] for r in rows if r.get("shem_yevuan")})
    if len(importers) == 1:
        facts["original_importer"] = _clean({**base, "value": importers[0]})
    elif importers:
        withheld.append({"source": SOURCES[dataset], "field": "original_importer", "reason": "several_importers",
                         "values": importers})
    return facts, withheld


def recall_model_map() -> dict:
    return G2.load_map(SN.gov_dir() / SN.MAP_NAME)


def recall_facts(row: dict) -> tuple[dict, list[dict]]:
    dataset = "recall_notices"
    source = SOURCES[dataset]

    def hold(reason: str, **extra) -> tuple[dict, list[dict]]:
        return {}, [{"source": source, "field": "recalls", "reason": reason, **extra}]

    path = SN.materialize(dataset)
    if path is None:
        return hold("gov_snapshot_missing")
    meta = SN.meta_of(path)
    makes = catalogue_makes(row.get("tozar"))
    model = norm_model(row.get("kinuy_mishari"))
    year = to_int(row.get("shnat_yitzur"))
    if not makes or not model:
        return hold("recall_model_unresolved", detail="no canonical make or model name")
    pairs = [(make, degem) for (make, degem), kinuy in G2.map_pairs(recall_model_map()).items()
             if make in makes and model in {norm_model(k) for k in kinuy}]
    if not pairs:
        return hold("recall_model_unresolved", detail="not in data/gov/recall_model_map.json")
    if year is None:
        return hold("recall_range_unresolved", detail="no model year")
    code = to_int(row.get("tozeret_cd"))
    clauses, params = [], []
    for make, degem in sorted(pairs):
        clauses.append("(make = ? AND degem_norm = ?)")
        params += [make, degem]
        if meta.get("tozar_cd_used_as_key") and code is not None:
            clauses.append("(tozar_cd = ? AND degem_norm = ?)")
            params += [code, degem]
    rows = SN.query(path, f"SELECT * FROM recalls WHERE {' OR '.join(clauses)}", params)
    if any(r.get("begin_year") is None or r.get("end_year") is None for r in rows):
        return hold("recall_range_unresolved", detail="a matched notice states no parseable production range")
    items: dict[str, dict] = {}
    for r in rows:
        if not (int(r["begin_year"]) <= year <= int(r["end_year"])) or not r.get("recall_id"):
            continue
        items.setdefault(str(r["recall_id"]), _clean({
            "recall_id": str(r["recall_id"]), "recall_year": r.get("recall_year"),
            "affected_system": r.get("affected_system"), "fault_description": r.get("fault_description"),
            "repair_method": r.get("repair_method"),
            "production_range": _clean({"from": r.get("build_begin"), "to": r.get("build_end")})}))
    listed = [items[k] for k in sorted(items, key=lambda k: (-(items[k].get("recall_year") or 0), k))]
    base = _provenance(dataset, _entry(dataset), "model", "canonical_make+recall_model_map+production_range")
    return {"recalls": {**base, "value": listed}, "recall_count": {**base, "value": len(listed)}}, []


def survival_facts(row: dict) -> tuple[dict, list[dict]]:
    dataset = "road_survival"
    source = SOURCES[dataset]

    def hold(reason: str, **extra) -> tuple[dict, list[dict]]:
        return {}, [{"source": source, "field": "road_survival", "reason": reason, **extra}]

    path = SN.materialize(dataset)
    if path is None:
        return hold("gov_snapshot_missing")
    meta = SN.meta_of(path)
    tc, dc, year = to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd")), to_int(row.get("shnat_yitzur"))
    if None in (tc, dc, year):
        return hold("no_government_key")
    basis = meta.get("cohort_basis")
    cohort_year, extra = year, {}
    if basis == G3.BASIS_FIRST_ROAD:
        mapping = SN.query(path, "SELECT first_road_year, n FROM model_year_map WHERE tozeret_cd = ? AND degem_cd = ? "
                                 "AND shnat_yitzur = ?", (tc, dc, year))
        first, share, n = G3.dominant(mapping)
        if first is None:
            return hold("model_year_unmapped")
        if share < float(meta.get("model_year_min_share") or 0.9):
            return hold("model_year_ambiguous", detail=f"first-road year {first} holds {share} of {n}")
        cohort_year, extra = first, {"model_year_share": share}
    rows = SN.query(path, "SELECT * FROM cohorts WHERE tozeret_cd = ? AND degem_cd = ? AND cohort_year = ?",
                    (tc, dc, cohort_year))
    if not rows:
        return hold("no_cohort")
    cohort = rows[0]
    if cohort.get("shares") is None:
        return hold("small_cohort", detail=f"cohort_size {cohort['cohort_size']} < {meta.get('min_cohort_size')}")
    value = _clean({"cohort_size": int(cohort["cohort_size"]),
                    "cancelled_share_by_age": json.loads(cohort["shares"]),
                    "median_age_at_final_cancellation": cohort.get("age_median"),
                    "final_cancellation_rate": round(int(cohort["cancelled_count"]) / int(cohort["cohort_size"]), 4),
                    "definition_he": meta.get("definition_he"), "cohort_basis": basis, "cohort_year": cohort_year,
                    "reference_month": meta.get("reference_month"), **extra})
    base = _provenance(dataset, _entry(dataset), "model_year", f"tozeret_cd+degem_cd+{basis}")
    return {"road_survival": {**base, "value": value}}, []


def gov_part(row: dict) -> dict:
    """{facts, withheld} of the government datasets for one Level 1.5 row."""
    facts: dict[str, dict] = {}
    withheld: list[dict] = []
    for part in (price_facts, recall_facts, survival_facts):
        found, held = part(row)
        facts.update(found)
        withheld += held
    return {"facts": facts, "withheld": withheld}


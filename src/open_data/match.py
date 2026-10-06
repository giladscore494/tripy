"""D-2 / D-3: the deterministic international match of the target's government identity against the local open-data
snapshots.

Route (the approval type, A1):

    european, co2_wltp > 0   EEA, then ADEME          strong key: co2_wltp == Ewltp (exact) + ec + ep (A4) + make /
                                                      model alias + fuel
    european, BEV / no co2   ADEME, then EEA          ep / kW (A4) + make / model alias + version tokens shared with the
                                                      commercial name / trim + battery token + mass window; exact only
                                                      with ONE surviving configuration in both sources
    american                 EPA, NRCan, CVS (vPIC)   year (MY) + displacement + drive + transmission + fuel + body
    unknown                  all sources              the stricter of both gates

Every source is matched and recorded (the other route's sources corroborate); the route decides the level:

    european with co2 key: a unique survivor on the strong key                                   exact_technical_variant
    european without co2 (BEV / NEDC era): one survivor in ADEME AND in EEA                     exact_technical_variant
    american: a unique survivor in EPA AND in NRCan agreeing on displacement / drive /
              transmission, no veto from a vPIC decode                                           exact_technical_variant
    anything else with survivors                                                                 body_powertrain

Type-code key (K1, EEA, data/open_datasets.json `eea_type_code_rules`): when the target's government type code
(degem_nm) names EEA rows by its make's rule (exact_va: degem_nm == Va; toyota_style: degem_nm == Ve or Va without a
trailing "(...)"), those rows are the candidates before any other key (`type_code_match`): only the power (A4) and
displacement vetoes apply, an equal co2_wltp selects among them when some row states it, and a surviving type-code
match is exact_technical_variant on the european route even without a co2 match (the co2 narrows the measured values;
the identity is the type code). A make without a rule, or a code no row carries: the match below, unchanged.

Vetoes (data-driven, data/open_datasets.json): displacement (+-1 % or the source's rounding: 2.0 L = 1998 cc), power
(A4, both definitions of koah_sus), drive (4X2 = FWD / RWD, 4X4 = AWD / 4WD), fuel / propulsion, transmission class,
body / doors, co2_wltp when both have it, battery token, mass window. A key the source does not state is `unknown`,
never a match. Pure and deterministic: the same snapshot rows always give the same match.
"""

from __future__ import annotations

import re
from typing import Any

from . import datasets as ds

MATCH_VERSION = "open-data-match-v1"
LEVEL_EXACT, LEVEL_BODY = "exact_technical_variant", "body_powertrain"
SOURCE_ORDER = ("eea_co2_cars", "ademe_car_labelling", "epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs")
EUROPEAN_SOURCES = ("eea_co2_cars", "ademe_car_labelling")
AMERICAN_SOURCES = ("epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs")
LITRES = re.compile(r"(?<![\d.])(\d\.\d)\s*l(?![a-z])", re.I)
KWH = re.compile(r"(?<![\d.])(\d{2,3}(?:[.,]\d)?)\s*kwh", re.I)
TOKEN = re.compile(r"[a-z0-9א-ת]+")


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _decimals(value: Any) -> int:
    text = str(value or "").strip()
    return len(text.split(".", 1)[1]) if "." in text else 0


def target_keys(fingerprint: dict, payload: dict | None, identity=None) -> dict:
    """The government keys the match reads (from the fingerprint and the Level 1.5 payload)."""
    from ..document_binding import vocabulary

    payload = payload or {}
    ident = payload.get("identity") or {}
    engine = payload.get("engine_drivetrain") or {}
    structure = payload.get("structure") or {}
    vocab = vocabulary()
    manufacturer = str(ident.get("manufacturer") or "").strip()
    family = getattr(identity, "family", None)
    if family is None:
        from ..document_binding import target_identity

        family = target_identity(payload).family
    from .makes import aliases

    makes = [m.upper() for m in aliases().get(manufacturer) or []]
    models = (vocab.get("open_data_model_aliases") or {}).get(f"{manufacturer}|{family}") or ([family] if family else [])
    homologation = fingerprint.get("homologation") or {}
    technical = fingerprint.get("technical") or {}
    route = (fingerprint.get("approval_route") or {}).get("route") or "unknown"
    co2 = _num(homologation.get("co2_wltp"))
    propulsion = engine.get("propulsion_normalized") or technical.get("norm_propulsion_technology")
    words = [w for w in TOKEN.findall(f"{ident.get('commercial_name') or ''} {ident.get('trim') or ''}".lower())]
    battery = [float(w) for w in words if w.isdigit() and 30 <= int(w) <= 150]
    type_code = str(fingerprint.get("type_code") or ident.get("model_code") or "").strip() or None
    return {"manufacturer": manufacturer, "makes": makes, "type_code": type_code,
            "models": [str(m).upper() for m in models if m],
            "family": family, "year": int(_num(ident.get("year"))) if _num(ident.get("year")) else None,
            "cc": _num(engine.get("engine_cc")),
            "power": _num(engine.get("power_hp")), "propulsion": propulsion,
            "drivetrain": "awd" if str(engine.get("drivetrain_normalized") or "") in ("awd", "four_wheel_drive")
            else "two_wheel_drive" if str(engine.get("drivetrain_normalized") or "") == "two_wheel_drive" else None,
            "transmission": {1: "automatic", 0: "manual", "1": "automatic", "0": "manual"}.get(engine.get("automatic")),
            "body": structure.get("body_normalized"), "doors": _num(structure.get("doors")),
            "gross_weight": _num(structure.get("gross_weight_kg")), "co2_wltp": co2 if co2 and co2 > 0 else None,
            "route": route, "words": words, "battery": battery,
            "bev_or_no_co2": propulsion == "battery_electric" or not (co2 and co2 > 0)}


def _values(config: dict, group: str) -> dict[str, list[str]]:
    return {k: [str(x).lower() for x in v] for k, v in (config.get(group) or {}).items() if isinstance(v, list)}


def _classify(text: Any, table: dict[str, list[str]]) -> set[str]:
    low = str(text or "").strip().lower()
    return {key for key, values in table.items() if low and low in values}


def transmission_of(code: Any, config: dict | None = None) -> dict | None:
    """{class, gears, gearbox_type} of an EPA / NRCan transmission code (data/open_datasets.json transmission_codes);
    None for a code no pattern reads (never guessed)."""
    config = ds.config() if config is None else config
    text = str(code or "").strip().lower()
    for rule in (config.get("transmission_codes") or {}).get("patterns") or []:
        m = re.fullmatch(rule["regex"], text)
        if m:
            gears = rule.get("gears")
            if isinstance(gears, str) and gears.startswith("$"):
                gears = int(m.group(int(gears[1:])))
            return {"class": rule.get("class"), "gears": gears, "gearbox_type": rule.get("gearbox_type")}
    return None


def _model_matches(row: dict, keys: dict) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("model", "base_model", "variant", "version")).upper()
    return any(re.search(rf"(?<![A-Z0-9]){re.escape(m)}(?![A-Z0-9])", text) for m in keys["models"])


def _drive_from_text(text: str, config: dict, dataset: dict) -> str | None:
    tokens = set(TOKEN.findall(str(text or "").lower()))
    for drive, values in (dataset.get("drive_tokens") or {}).items():
        if tokens & {v.lower() for v in values}:
            return drive
    return dataset.get("drive_default")


def drive_from_model_text(row: dict, source: str, config: dict) -> str | None:
    """M1: awd | two_wheel_drive when the row's own model / version text names its drive (data/open_datasets.json
    `drive_tokens_model_text`, per canonical make + `any`; a token matches at the start of a word: 'xDrive30e'),
    None when it names neither or both."""
    from .makes import canonical_of

    table = config.get("drive_tokens_model_text") or {}
    if source not in (table.get("sources") or []):
        return None
    text = " ".join(str(row.get(k) or "") for k in ("model", "version", "variant")).lower()
    make = canonical_of(row.get("make")) or str(row.get("make") or "").strip().upper()
    found = set()
    for group in (table.get("any") or {}, (table.get("makes") or {}).get(make) or {}):
        for drive, tokens in group.items():
            if any(re.search(rf"(?<![a-z0-9]){re.escape(str(t).lower())}", text) for t in tokens):
                found.add(drive)
    return found.pop() if len(found) == 1 else None


def _body_of_row(row: dict, dataset: dict) -> set[str]:
    """M4: the target bodies a source's own body column allows (`body_map`, e.g. ADEME Carrosserie BREAK -> wagon;
    BERLINE is a sedan or a hatchback in French usage); an unmapped value allows nothing known (unknown)."""
    mapping = dataset.get("body_map") or {}
    value = " ".join(str(row.get("body") or "").split()).upper()
    return {str(b) for b in mapping.get(value) or []} if value else set()


def _check(row: dict, source: str, keys: dict, config: dict, dataset: dict) -> dict:
    """{vetoes, matched, unknown, power_definition} of one row."""
    vetoes, matched, unknown = [], [], []
    power_definition = None
    model_text = " ".join(str(row.get(k) or "") for k in ("model", "version", "variant"))
    # displacement: +-1 % or the source's rounding (2.0 L = 1998 cc)
    cc = keys["cc"]
    stated_cc, tolerance = None, None
    if _num(row.get("displacement_cc")) is not None:
        stated_cc, tolerance = _num(row["displacement_cc"]), 0.01 * (cc or 0)
    elif _num(row.get("displacement_l")) is not None:
        stated_cc = _num(row["displacement_l"]) * 1000
        tolerance = max(0.01 * (cc or 0), 0.5 * 10 ** -max(_decimals(row["displacement_l"]), 1) * 1000)
    elif LITRES.search(model_text):
        litres = LITRES.search(model_text).group(1)
        stated_cc, tolerance = float(litres) * 1000, max(0.01 * (cc or 0), 50.0)
    if keys["propulsion"] == "battery_electric":
        if stated_cc and stated_cc > 0:
            vetoes.append("displacement")
    elif stated_cc is None or cc is None:
        unknown.append("displacement")
    elif abs(stated_cc - cc) <= tolerance:
        matched.append("displacement")
    else:
        vetoes.append("displacement")
    # power (A4)
    from ..document_binding import power_anchor

    if _num(row.get("power_kw")) is not None and keys["power"]:
        anchor = power_anchor(keys["power"], _num(row["power_kw"]), "kw")
        if anchor and anchor["match"]:
            matched.append("power")
            power_definition = anchor["definition"]
        else:
            vetoes.append("power")
    else:
        unknown.append("power")
    # drive
    drives = _values(config, "drive_values")
    drive = None
    if row.get("drive"):
        found = _classify(row.get("drive"), drives)
        drive = found.pop() if len(found) == 1 else None
    if drive is None and (dataset.get("drive_tokens") or dataset.get("drive_default")):
        drive = _drive_from_text(model_text, config, dataset)
    if drive is None:
        drive = drive_from_model_text(row, source, config)          # M1: "530e xDrive", "C 300 4MATIC"
    if drive is None or keys["drivetrain"] is None:
        unknown.append("drive")
    elif drive == keys["drivetrain"]:
        matched.append("drive")
    else:
        vetoes.append("drive")
    # fuel / propulsion
    fuels = _classify(row.get("fuel"), _values(config, "fuel_values"))
    modes = _classify(row.get("fuel_mode"), {k: [x.lower() for x in v] for k, v in
                                               (config.get("fuel_mode_values") or {}).items()})
    if modes:
        fuels = fuels & modes if fuels & modes else modes
    if not fuels or keys["propulsion"] is None:
        unknown.append("fuel")
    elif keys["propulsion"] in fuels:
        matched.append("fuel")
    else:
        vetoes.append("fuel")
    # transmission class
    trans = transmission_of(row.get("transmission"), config) if row.get("transmission") else None
    if trans is None or keys["transmission"] is None:
        unknown.append("transmission")
    elif trans["class"] == keys["transmission"]:
        matched.append("transmission")
    else:
        vetoes.append("transmission")
    # body / doors (a body word in the source's own model / class text)
    bodies = _values(config, "body_values")
    found_bodies = _body_of_row(row, dataset) or {
        b for b, words in bodies.items()
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", model_text.lower()) for w in words)}
    if not found_bodies or not keys["body"]:
        unknown.append("body")
    elif keys["body"] in found_bodies:
        matched.append("body")
    else:
        vetoes.append("body")
    # co2_wltp, exact, when both state it
    stated_co2 = _num(row.get("co2_wltp"))
    if keys["co2_wltp"] and stated_co2:
        (matched if stated_co2 == keys["co2_wltp"] else vetoes).append("co2_wltp")
    elif keys["co2_wltp"]:
        unknown.append("co2_wltp")
    # BEV / no co2: battery token and mass window
    if keys["bev_or_no_co2"]:
        stated_kwh = [float(x.replace(",", ".")) for x in KWH.findall(model_text)]
        if keys["battery"] and stated_kwh:
            (matched if any(abs(b - k) < 0.6 for b in keys["battery"] for k in stated_kwh) else vetoes).append(
                "battery")
        window = config.get("mass_window") or {}
        mass = _num(row.get("mass_running_order_kg"))
        if mass and keys["gross_weight"] and window:
            payload = keys["gross_weight"] - mass
            ok = float(window.get("min_payload_kg", 0)) <= payload <= float(window.get("max_payload_kg", 10 ** 6))
            (matched if ok else vetoes).append("mass_window")
        version_words = set(TOKEN.findall(model_text.lower()))
        own = [w for w in keys["words"] if not w.isdigit() and w.upper() not in keys["models"] and len(w) >= 2]
        if source == "ademe_car_labelling" and own:
            (matched if version_words & set(own) else vetoes).append("version_tokens")
    return {"vetoes": vetoes, "matched": matched, "unknown": unknown, "power_definition": power_definition}


PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _type_code(value: Any) -> str:
    """A type code as compared: trimmed, upper case, inner whitespace as '-' ('ZWE211L DEXGBW' = 'ZWE211L-DEXGBW')."""
    return re.sub(r"\s+", "-", str(value or "").strip().upper())


def type_code_rule(rule: str, code: Any, row: dict) -> bool:
    """Does an EEA row carry the government type code under this rule (data/open_datasets.json eea_type_code_rules)?"""
    code = _type_code(code)
    if len(code) < 2:
        return False
    if rule == "exact_va":
        return code == str(row.get("variant") or "").strip().upper()
    if rule == "toyota_style":
        return any(code == _type_code(PAREN_SUFFIX.sub("", str(row.get(k) or ""))) for k in ("version", "variant"))
    return False                                # an unknown rule name never matches


def type_code_rules(config: dict | None = None) -> dict[str, list[str]]:
    """{canonical make: [rule, ...]} (ordered)."""
    config = ds.config() if config is None else config
    makes = (config.get("eea_type_code_rules") or {}).get("makes") or {}
    return {str(k).upper(): [str(r) for r in v] for k, v in makes.items() if isinstance(v, list)}


def type_code_match(rows: list[dict], code: Any, config: dict | None = None) -> dict:
    """{status: no_code | no_rows | no_rule | no_match | match, rule, rows}: the rows whose make has rules and carries
    the code by the first rule (in order) that matches any of them."""
    from .makes import canonical_of

    if not _type_code(code):
        return {"status": "no_code", "rows": []}
    if not rows:
        return {"status": "no_rows", "rows": []}         # no EEA row of the make / years (e.g. a 2023+ year before K3)
    rules = type_code_rules(config)
    by_make: dict[str, list[dict]] = {}
    for row in rows:
        make = canonical_of(row.get("make")) or str(row.get("make") or "").strip().upper()
        if make in rules:
            by_make.setdefault(make, []).append(row)
    if not by_make:
        return {"status": "no_rule", "rows": []}
    tried: list[str] = []
    for make, make_rows in sorted(by_make.items()):
        for rule in rules[make]:
            tried.append(rule)
            found = [r for r in make_rows if type_code_rule(rule, code, r)]
            if found:
                return {"status": "match", "rule": rule, "make": make, "rows": found}
    return {"status": "no_match", "rules": sorted(set(tried)), "rows": []}


def type_code_coverage(codes_by_make: dict[str, list[str]], rows_by_make: dict[str, list[dict]],
                       config: dict | None = None, examples: int = 8) -> dict[str, dict]:
    """The shadow report's per-make view of the key: {make: {degem_nm, rules (defined), per_rule {rule: n}, matched,
    unmatched_examples}} for type codes against EEA rows of that make."""
    rules = type_code_rules(config)
    out = {}
    for make, codes in sorted(codes_by_make.items()):
        codes = sorted({_type_code(c) for c in codes if _type_code(c)})
        make_rows = rows_by_make.get(make) or []
        per_rule: dict[str, int] = {}
        unmatched = []
        for code in codes:
            hit = next((rule for rule in rules.get(make.upper(), []) if any(type_code_rule(rule, code, r)
                                                                            for r in make_rows)), None)
            if hit:
                per_rule[hit] = per_rule.get(hit, 0) + 1
            else:
                unmatched.append(code)
        out[make] = {"degem_nm": len(codes), "rules": rules.get(make.upper(), []), "per_rule": per_rule,
                     "matched": sum(per_rule.values()), "unmatched_examples": unmatched[:examples]}
    return out


def _years(source: str, keys: dict) -> list[int] | None:
    year = int(keys["year"]) if keys["year"] else None
    if year is None:
        return None
    if source == "eea_co2_cars":
        return [year, year + 1]                 # EEA: the registration year
    if source == "ademe_car_labelling":
        return None                             # ADEME: the dataset date, not a model year
    return [year]                               # EPA / NRCan: the model year; CVS: the file year (corroboration)


def designation(row: dict, source: str) -> str:
    if source == "eea_co2_cars":
        parts = [row.get("model"), row.get("variant"), row.get("version")]
    elif source == "ademe_car_labelling":
        parts = [row.get("model"), row.get("version")]
    else:
        parts = [row.get("model"), row.get("transmission"), row.get("drive")]
    return " ".join(str(p) for p in parts if p not in (None, ""))[:160]


def catalogue_period(source: str, keys: dict, dataset: dict, rows: list[dict] | None, folder=None) -> dict | None:
    """M4: a catalogue without a model year (ADEME, `catalogue_period`): {status in_period | out_of_period,
    catalogue_date, basis, catalogue_year, target_year}; the rows are candidates only for a target year >= the
    catalogue year - window_years. The date: the rows' catalogue_date, else the snapshot's (catalogue_date, else
    built_at). None without the rule, a target year or a date."""
    period = dataset.get("catalogue_period")
    if not isinstance(period, dict) or not keys.get("year"):
        return None
    date, basis = None, None
    if rows is not None:
        dates = sorted({str(r.get("catalogue_date")) for r in rows if r.get("catalogue_date")})
        date, basis = (dates[-1], "rows") if dates else (None, None)
    else:
        meta = ds.snapshot_meta(source, folder)
        date, basis = (meta.get("catalogue_date"), "catalogue_date") if meta.get("catalogue_date") else \
            (meta.get("built_at"), "built_at")
    year = ds._int(str(date)[:4]) if date else None
    if year is None:
        return None
    window = int(period.get("window_years") or 0)
    status = "in_period" if int(keys["year"]) >= year - window else "out_of_period"
    return {"status": status, "catalogue_date": date, "basis": basis, "catalogue_year": year,
            "target_year": int(keys["year"]), "window_years": window}


def match_source(source: str, keys: dict, folder=None, rows: list[dict] | None = None) -> dict:
    """The `international_variant` of one source."""
    config = ds.config()
    dataset = ds.datasets().get(source) or {}
    out: dict[str, Any] = {"source": source, "status": "none", "candidates": 0}
    if rows is None and not ds.available(source, folder):
        return {**out, "status": "no_snapshot"}
    period = catalogue_period(source, keys, dataset, rows, folder)
    if period:
        out["catalogue"] = period
        if period["status"] == "out_of_period":
            return {**out, "status": "out_of_period"}
    if rows is None:
        read: dict = {}
        rows = ds.query_rows(source, makes=keys["makes"], years=_years(source, keys), folder=folder, report=read)
        if read.get("skipped_shards"):
            out["skipped_shards"] = read["skipped_shards"]          # a missing / mismatched shard: reported, skipped
    rules = config.get("eea_type_code_rules") or {}
    by_code = {"status": "no_code", "rows": []}
    if source in (rules.get("sources") or []):
        by_code = type_code_match(rows, keys.get("type_code"), config)
    if by_code["status"] != "no_code":
        out["type_code"] = {**{k: v for k, v in by_code.items() if k != "rows"}, "code": keys.get("type_code"),
                            "rows": len(by_code["rows"])}
    typed = by_code["status"] == "match"
    if typed:
        # V2: the other type codes (Va) of the same model in the year window: a page naming only those is another
        # variant
        own = {str(r.get("variant") or "").strip().upper() for r in by_code["rows"]}
        out["type_code"]["siblings"] = sorted(
            {v for v in (str(r.get("variant") or "").strip().upper() for r in rows if _model_matches(r, keys))
             if re.fullmatch(r"[A-Z0-9]{3,10}", v) and re.search(r"\d", v)} - own)[:40]
    rows = by_code["rows"] if typed else [r for r in rows if _model_matches(r, keys)]
    out["candidates"] = len(rows)
    hard = set(rules.get("hard_vetoes") or ["power", "displacement"])
    survivors, vetoed = [], []
    for row in rows:
        verdict = _check(row, source, keys, config, dataset)
        if typed:
            # K1: the type code is the identity; only the power / displacement vetoes still apply, co2 selects below
            verdict["soft_vetoes"] = [v for v in verdict["vetoes"] if v not in hard]
            verdict["vetoes"] = [v for v in verdict["vetoes"] if v in hard]
            verdict["matched"] = verdict["matched"] + ["type_code_match"]
        entry = {"row_id": row.get("row_id"), "designation": designation(row, source), **verdict}
        (vetoed if verdict["vetoes"] else survivors).append((row, entry))
    superseded: list[tuple[dict, dict]] = []
    if typed and keys.get("co2_wltp"):
        equal = [(r, e) for r, e in survivors if _num(r.get("co2_wltp")) == keys["co2_wltp"]]
        out["type_code"]["co2_selected"] = bool(equal)
        if equal:
            superseded = [(r, e) for r, e in survivors if (r, e) not in equal]
            survivors = equal
    elif keys.get("co2_wltp") and any("co2_wltp" in e["matched"] for _, e in survivors):
        # M1: rows that do not state the co2 key never survive next to rows that match it exactly
        superseded = [(r, e) for r, e in survivors if "co2_wltp" not in e["matched"]]
        survivors = [(r, e) for r, e in survivors if "co2_wltp" in e["matched"]]
    if superseded:
        out["superseded_by_exact_key"] = {"key": "co2_wltp", "rows": len(superseded),
                                          "row_ids": [e["row_id"] for _, e in superseded][:20]}
    if survivors:
        co2 = all("co2_wltp" in e["matched"] for _, e in survivors)
        basis = "type_code+co2" if typed and co2 else "type_code" if typed else "co2" if co2 else None
        if basis:
            # M3: the narrowest exact subset the measured-value offers come from
            out["exact_subset"] = {"basis": basis, "rows": len(survivors),
                                   "row_ids": [e["row_id"] for _, e in survivors][:40]}
    out["vetoed"] = [{"row_id": e["row_id"], "designation": e["designation"], "vetoes": e["vetoes"]}
                     for _, e in vetoed][:40]
    out["veto_counts"] = {}
    for _, entry in vetoed:
        for veto in entry["vetoes"]:
            out["veto_counts"][veto] = out["veto_counts"].get(veto, 0) + 1
    if not survivors:
        return out
    configs = {}
    for row, entry in survivors:
        configs.setdefault(entry["designation"], []).append((row, entry))
    out["status"] = "unique" if len(configs) == 1 else "ambiguous"
    first = survivors[0][1]
    out["designation"] = first["designation"] if len(configs) == 1 else None
    out["source_ids"] = [e["row_id"] for _, e in survivors][:40]
    out["keys_matched"] = sorted(set.intersection(*(set(e["matched"]) for _, e in survivors)))
    out["keys_unknown"] = sorted(set.union(*(set(e["unknown"]) for _, e in survivors)))
    out["power_definition"] = first.get("power_definition")
    out["survivors"] = [{**{k: v for k, v in row.items() if k not in ("raw",)}} for row, _ in survivors][:40]
    out["configurations"] = sorted(configs)
    if typed:
        out["type_code"]["survivors"] = len(survivors)
        named = {" ".join(str(r.get(k)) for k in ("model", "variant") if r.get(k)) for r, _ in survivors}
        if out["designation"] is None and len(named) == 1:
            out["designation"] = named.pop()        # one model + type code (Va); the versions differ
    return out


def match(fingerprint: dict, payload: dict | None, identity=None, folder=None,
          rows_by_source: dict[str, list[dict]] | None = None) -> dict:
    """The open_data_match: {version, route, keys, sources: {source: international_variant}, level, designation}."""
    keys = target_keys(fingerprint, payload, identity)
    sources = {}
    for source in SOURCE_ORDER:
        rows = (rows_by_source or {}).get(source) if rows_by_source is not None else None
        if rows_by_source is not None and source not in rows_by_source:
            sources[source] = {"source": source, "status": "no_snapshot", "candidates": 0}
            continue
        sources[source] = match_source(source, keys, folder, rows)
    level, basis = match_level(keys, sources)
    lead = _lead_source(keys, sources)
    return {"version": MATCH_VERSION, "route": keys["route"],
            "route_detail": "european_co2" if keys["route"] == "european" and not keys["bev_or_no_co2"]
            else "european_no_co2" if keys["route"] == "european" else keys["route"],
            "keys": {k: keys[k] for k in ("makes", "models", "type_code", "year", "cc", "power", "drivetrain",
                                          "transmission", "propulsion", "body", "co2_wltp", "battery")},
            "sources": sources, "level": level, "level_basis": basis,
            "designation": (sources.get(lead) or {}).get("designation") if lead else None, "lead_source": lead}


def _unique(sources: dict, name: str) -> bool:
    return (sources.get(name) or {}).get("status") == "unique"


def _any_survivor(sources: dict, names) -> bool:
    return any((sources.get(n) or {}).get("status") in ("unique", "ambiguous") for n in names)


def _american_exact(sources: dict) -> bool:
    if not (_unique(sources, "epa_fueleconomy") and _unique(sources, "nrcan_fuel_ratings")):
        return False
    epa, nrcan = sources["epa_fueleconomy"], sources["nrcan_fuel_ratings"]
    a, b = (epa.get("survivors") or [{}])[0], (nrcan.get("survivors") or [{}])[0]
    ta, tb = transmission_of(a.get("transmission")), transmission_of(b.get("transmission"))
    same_disp = _num(a.get("displacement_l")) is not None and _num(a.get("displacement_l")) == _num(b.get("displacement_l"))
    same_trans = bool(ta and tb and ta["class"] == tb["class"] and ta["gears"] == tb["gears"])
    vpic = sources.get("nhtsa_vpic") or {}
    return same_disp and same_trans and "drive" not in (epa.get("keys_unknown") or []) and not vpic.get("vetoes")


def type_code_matched(sources: dict) -> bool:
    """K1: a type-code match that survived the power / displacement vetoes (EEA)."""
    return any(((sources.get(s) or {}).get("type_code") or {}).get("status") == "match"
               and (sources.get(s) or {}).get("status") in ("unique", "ambiguous") for s in EUROPEAN_SOURCES)


def _european_exact(keys: dict, sources: dict) -> bool:
    if type_code_matched(sources):
        return True
    if not keys["bev_or_no_co2"]:
        return any(_unique(sources, s) and "co2_wltp" in ((sources[s].get("keys_matched")) or [])
                   for s in EUROPEAN_SOURCES)
    return _unique(sources, "ademe_car_labelling") and _unique(sources, "eea_co2_cars")


def match_level(keys: dict, sources: dict) -> tuple[str | None, str]:
    route = keys["route"]
    if route == "european":
        exact, names = _european_exact(keys, sources), EUROPEAN_SOURCES
    elif route == "american":
        exact, names = _american_exact(sources), AMERICAN_SOURCES
    else:
        exact = _european_exact(keys, sources) and _american_exact(sources)
        names = SOURCE_ORDER
    if exact:
        return LEVEL_EXACT, f"{route}_type_code" if route == "european" and type_code_matched(sources) \
            else f"{route}_unique"
    if _any_survivor(sources, names):
        return LEVEL_BODY, f"{route}_survivors"
    return None, "no_match"


def _lead_source(keys: dict, sources: dict) -> str | None:
    order = {"european": ("ademe_car_labelling", "eea_co2_cars") if keys["bev_or_no_co2"] else EUROPEAN_SOURCES,
             "american": AMERICAN_SOURCES}.get(keys["route"], SOURCE_ORDER)
    return next((s for s in order if (sources.get(s) or {}).get("status") == "unique"), None) or \
        next((s for s in order if (sources.get(s) or {}).get("status") == "ambiguous"), None)

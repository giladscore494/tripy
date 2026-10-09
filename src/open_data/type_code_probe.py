"""The type-code probe (MCP type_code_probe): per canonical make, how the government type code (degem_nm) relates to the
EEA identity fields, to decide whether a type-code rule (data/open_datasets.json `eea_type_code_rules`) can exist for
that make. Read-only and measuring only: it writes nothing and changes no matching.

    sample      catalogue rows of the make's tozar (data/make_canonical.json, plain makes only: a model-gated make's rows
                are another make's tozar), private segment, the first `per_year` variant identity keys of each model
                year by md5(variant_identity_key), one row per key (the lowest upstream_record_id): SAMPLE_SQL, one
                read-only query for the whole window. Read: degem_nm, kinuy_mishari, engine_cc (nefah_manoa),
                horsepower (koah_sus), co2_wltp, shnat_yitzur
    candidates  the EEA rows of the make in the registration years model_year_from .. model_year_to + 1 (the matcher's
                window: match._years), by the existing make filter: the alias spellings of those tozar
                (makes.aliases, datasets.query_rows), less the rows whose spelling normalizes to another make
    relation    per catalogue row and EEA identity field (type_approval T, variant Va, version Ve), the strongest of:
                equal; prefix (one is a prefix of the other, the shorter >= 4 characters); contains (one contains the
                other, the shorter >= 4); none. Both sides normalized: NFKC, upper case, no spaces / dots / dashes
    agreement   among the EEA rows holding a row's relation: power (|horsepower x 0.7355 - kW| <= 1), cc (|cc - cc| <=
                20; a missing or zero cc on both sides is 0 = 0, the BEV case) and, when both state it, co2_wltp equal.
                A row is consistent when one of those EEA rows agrees on power and cc
    output      n; per field the share of each relation; per (relation, field) the consistency rate, the CO2 agreement
                and EXAMPLES examples (degem_nm, the EEA value, power, cc); EXAMPLES rows with no relation in any field,
                with the closest EEA values (difflib ratio) among the rows of the same model text (kinuy_mishari)
    budget      BUDGET_S per call: when reached, what is done is returned with `truncated: true`

Never a write to MILO or to the snapshots.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from typing import Any, Callable

from ..server_logging import get_logger

SOURCE = "eea_co2_cars"
FIELDS = ("type_approval", "variant", "version")
RELATIONS = ("equal", "prefix", "contains", "none")
MIN_PARTIAL = 4                          # prefix / contains: the shorter side has at least this many characters
HP_TO_KW = 0.7355
POWER_KW_TOLERANCE = 1.0
CC_TOLERANCE = 20.0
MAX_PER_YEAR = 200
DEFAULT_PER_YEAR = 60
MAX_YEARS = 20
BUDGET_S = 60.0
EXAMPLES = 10
CLOSEST = 3
log = get_logger("open_data")

SAMPLE_SQL = """
WITH keyed AS (
  SELECT v.variant_identity_key, v.upstream_record_id, v.tozar, v.degem_nm, v.kinuy_mishari,
         v.nefah_manoa AS engine_cc, v.koah_sus AS horsepower, v.co2_wltp, v.shnat_yitzur,
         ROW_NUMBER() OVER (PARTITION BY v.shnat_yitzur, v.variant_identity_key ORDER BY v.upstream_record_id) AS k
  FROM public.catalog_variants_current AS v
  WHERE v.vehicle_segment = 'private'
    AND v.tozar = ANY(%(tozar)s)
    AND v.shnat_yitzur BETWEEN %(year_from)s AND %(year_to)s
    AND v.variant_identity_key IS NOT NULL
), ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY shnat_yitzur ORDER BY md5(variant_identity_key)) AS n
  FROM keyed
  WHERE k = 1
)
SELECT variant_identity_key, upstream_record_id, tozar, degem_nm, kinuy_mishari, engine_cc, horsepower, co2_wltp,
       shnat_yitzur
FROM ranked
WHERE n <= %(per_year)s
ORDER BY shnat_yitzur, n
"""


class ProbeInputError(ValueError):
    """An argument out of range (the MCP reports it as a rejected call)."""


def _year(value: Any, name: str) -> int:
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ProbeInputError(f"{name} must be a model year") from None
    if not 1950 <= year <= 2100:
        raise ProbeInputError(f"{name} must be a model year between 1950 and 2100")
    return year


def arguments(make: Any, year_from: Any, year_to: Any, per_year: Any = DEFAULT_PER_YEAR,
              table: dict | None = None) -> tuple[str, list[str], int, int, int]:
    """The validated (canonical make, its tozar, model_year_from, model_year_to, per_year); ProbeInputError otherwise.
    The make is a canonical make of data/make_canonical.json (any spelling that normalizes to one)."""
    from .makes import canonical, canonical_of, tozar_makes

    table = canonical() if table is None else table
    name = canonical_of(str(make or "").strip(), table) if str(make or "").strip() else None
    tozar = sorted(t for t in table.get("tozar") or {} if name and name in tozar_makes(t, table)[0])
    if not name or not tozar:
        raise ProbeInputError(f"make must be a canonical make of data/make_canonical.json with a tozar: {make!r}"[:200])
    low, high = _year(year_from, "model_year_from"), _year(year_to, "model_year_to")
    if low > high:
        raise ProbeInputError("model_year_from must not be after model_year_to")
    if high - low + 1 > MAX_YEARS:
        raise ProbeInputError(f"at most {MAX_YEARS} model years per call")
    try:
        n = int(DEFAULT_PER_YEAR if per_year is None else per_year)
    except (TypeError, ValueError):
        raise ProbeInputError("per_year must be a number") from None
    if not 1 <= n <= MAX_PER_YEAR:
        raise ProbeInputError(f"per_year must be between 1 and {MAX_PER_YEAR}")
    return name, tozar, low, high, n


def norm(value: Any) -> str:
    """A type code as the probe compares it: NFKC, upper case, no whitespace / dots / dashes ('223.133' = '223133')."""
    return re.sub(r"[\s.\-‐-―]+", "", unicodedata.normalize("NFKC", str(value or "")).upper())


def relation(code: str, value: str) -> str:
    """The relation of two normalized strings: equal, prefix, contains or none (module docstring)."""
    if not code or not value:
        return "none"
    if code == value:
        return "equal"
    if min(len(code), len(value)) < MIN_PARTIAL:
        return "none"
    if code.startswith(value) or value.startswith(code):
        return "prefix"
    if code in value or value in code:
        return "contains"
    return "none"


def _num(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _cc(value: Any) -> float:
    return _num(value) or 0.0                    # a missing cc is 0 (a BEV states none or 0)


def agrees(row: dict, eea: dict) -> tuple[bool, bool | None]:
    """(power and cc agree, co2 agrees: None when either side has no co2_wltp)."""
    hp, kw = _num(row.get("horsepower")), _num(eea.get("power_kw"))
    power = hp is not None and kw is not None and abs(hp * HP_TO_KW - kw) <= POWER_KW_TOLERANCE
    cc = abs(_cc(row.get("engine_cc")) - _cc(eea.get("displacement_cc"))) <= CC_TOLERANCE
    co2_gov, co2_eea = _num(row.get("co2_wltp")), _num(eea.get("co2_wltp"))
    co2 = None if not co2_gov or not co2_eea else co2_gov == co2_eea
    return power and cc, co2


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


class _Field:
    """One EEA identity field: {normalized value: [the raw value, the distinct (kW, cc, co2) rows]}."""

    def __init__(self, name: str, rows: list[dict]):
        self.name = name
        values: dict[str, tuple[str, dict[tuple, dict]]] = {}
        for row in rows:
            key = norm(row.get(name))
            if not key:
                continue
            raw, measures = values.setdefault(key, (str(row.get(name)), {}))
            measures.setdefault((row.get("power_kw"), row.get("displacement_cc"), row.get("co2_wltp")), row)
        self.values = values
        self.keys = sorted(values)

    def best(self, code: str) -> tuple[str, list[str]]:
        """(the strongest relation of `code` to any value, the values holding it)."""
        if not code:
            return "none", []
        if code in self.values:
            return "equal", [code]
        found: dict[str, list[str]] = {"prefix": [], "contains": []}
        if len(code) >= MIN_PARTIAL:
            for value in self.keys:
                kind = relation(code, value)
                if kind != "none":
                    found[kind].append(value)
        for kind in ("prefix", "contains"):
            if found[kind]:
                return kind, found[kind]
        return "none", []

    def rows_of(self, values: list[str]) -> list[dict]:
        return [row for value in values for row in self.values[value][1].values()]


def _model_words(text: Any) -> list[str]:
    return [w for w in re.findall(r"[A-Z0-9]+", unicodedata.normalize("NFKC", str(text or "")).upper()) if len(w) >= 2]


def model_groups(eea_rows: list[dict]) -> dict[str, tuple[str, set[str], list[dict]]]:
    """{raw EEA model: (normalized model, its words, its rows)}: the rows grouped once per call."""
    out: dict[str, tuple[str, set[str], list[dict]]] = {}
    for eea in eea_rows:
        model = str(eea.get("model") or "")
        if model not in out:
            out[model] = (norm(model), set(_model_words(model)), [])
        out[model][2].append(eea)
    return out


def same_model(row: dict, groups: dict[str, tuple[str, set[str], list[dict]]], make: str) -> list[dict]:
    """The EEA rows of the catalogue row's model text: the EEA model (normalized, >= 2 characters) is in the normalized
    kinuy_mishari, or a kinuy_mishari word (>= 3 characters, not the make's name) is a word of the EEA model."""
    commercial = norm(row.get("kinuy_mishari"))
    own = {w for w in _model_words(row.get("kinuy_mishari")) if len(w) >= 3} - set(_model_words(make))
    out: list[dict] = []
    for model, words, rows in groups.values():
        if (len(model) >= 2 and model in commercial) or own & words:
            out += rows
    return out


def closest(code: str, rows: list[dict], n: int = CLOSEST) -> dict[str, list[list]]:
    """Per field, the n distinct raw values most similar to the code: [[value, difflib ratio], ...] (best first, ties by
    value)."""
    out = {}
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(code)
    for field in FIELDS:
        values: dict[str, str] = {}
        for row in rows:
            key = norm(row.get(field))
            if key:
                values.setdefault(key, str(row.get(field)))
        best: list[tuple[float, str]] = []
        for value in sorted(values):
            matcher.set_seq1(value)
            floor = best[-1][0] if len(best) >= n else -1.0
            if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
                continue
            ratio = matcher.ratio()
            if ratio > floor:
                best = sorted(best + [(ratio, value)], key=lambda item: (-item[0], item[1]))[:n]
        out[field] = [[values[v], round(r, 2)] for r, v in best]
    return out


def _power(row: dict, eea: dict | None) -> dict:
    hp = _num(row.get("horsepower"))
    return {"catalog_hp": hp, "catalog_kw": round(hp * HP_TO_KW, 1) if hp is not None else None,
            "eea_kw": _num((eea or {}).get("power_kw"))}


def _cc_view(row: dict, eea: dict | None) -> dict:
    return {"catalog": _num(row.get("engine_cc")), "eea": _num((eea or {}).get("displacement_cc"))}


def eea_candidates(make: str, tozar: list[str], low: int, high: int, folder=None) -> list[dict]:
    """The EEA rows of the make in the registration years low .. high + 1 (module docstring), ordered by row_id."""
    from . import datasets as ds
    from .makes import aliases, canonical_of

    known = aliases()
    spellings = sorted({s for t in tozar for s in known.get(t) or []})
    rows = ds.query_rows(SOURCE, makes=spellings, years=range(low, high + 2), folder=folder)
    rows = [r for r in rows if canonical_of(r.get("make")) in (make, None)]
    return sorted(rows, key=lambda r: str(r.get("row_id") or ""))


def probe(make: str, tozar: list[str], low: int, high: int, per_year: int, catalog_rows: list[dict],
          eea_rows: list[dict], *, budget_s: float = BUDGET_S, started: float | None = None,
          clock: Callable[[], float] = time.monotonic) -> dict:
    """The probe over a catalogue sample and the make's EEA rows (pure: no query; see type_code_probe)."""
    started = clock() if started is None else started
    fields = {f: _Field(f, eea_rows) for f in FIELDS}
    counts = {f: {r: 0 for r in RELATIONS} for f in FIELDS}
    pairs: dict[str, dict] = {f"{r}|{f}": {"n": 0, "power_cc_agree": 0, "co2_comparable": 0, "co2_agree": 0}
                              for r in RELATIONS[:-1] for f in FIELDS}
    examples: dict[str, list[dict]] = {key: [] for key in pairs}
    unrelated: list[dict] = []
    years: dict[int, int] = {}
    n = without = 0
    truncated = False
    best_cache: dict[tuple[str, str], tuple[str, list[str]]] = {}
    groups: dict | None = None
    for row in catalog_rows:
        if clock() - started >= budget_s:
            truncated = True
            break
        n += 1
        year = int(_num(row.get("shnat_yitzur")) or 0)
        years[year] = years.get(year, 0) + 1
        code = norm(row.get("degem_nm"))
        related = False
        for name, field in fields.items():
            hit = best_cache.get((name, code))
            if hit is None:
                hit = best_cache[(name, code)] = field.best(code)
            kind, values = hit
            counts[name][kind] += 1
            if kind == "none":
                continue
            related = True
            pair = pairs[f"{kind}|{name}"]
            pair["n"] += 1
            agreeing, comparable, co2_ok = None, False, False
            for eea in field.rows_of(values):
                power_cc, co2 = agrees(row, eea)
                comparable = comparable or co2 is not None
                if power_cc and agreeing is None:
                    agreeing = eea
                if power_cc and co2:
                    co2_ok = True
                    agreeing = eea
                    break
            pair["power_cc_agree"] += agreeing is not None
            pair["co2_comparable"] += comparable
            pair["co2_agree"] += co2_ok
            shown = examples[f"{kind}|{name}"]
            if len(shown) < EXAMPLES:
                eea = agreeing or field.rows_of(values)[0]
                shown.append({"degem_nm": row.get("degem_nm"), "kinuy_mishari": row.get("kinuy_mishari"),
                              "year": year, "eea_value": eea.get(name), "eea_model": eea.get("model"),
                              "eea_values_related": len(values), "power": _power(row, eea), "cc": _cc_view(row, eea),
                              "agree": agreeing is not None})
        if not related:
            without += 1
            if len(unrelated) < EXAMPLES:
                groups = model_groups(eea_rows) if groups is None else groups
                model_rows = same_model(row, groups, make)
                unrelated.append({"degem_nm": row.get("degem_nm"), "kinuy_mishari": row.get("kinuy_mishari"),
                                  "year": year, "power": _power(row, None), "cc": _cc_view(row, None),
                                  "model_rows": len(model_rows),
                                  "basis": "same_model_text" if model_rows else "all_rows",
                                  "closest": closest(code, model_rows or eea_rows)})
    relations = {f: {r: {"n": counts[f][r], "pct": _pct(counts[f][r], n)} for r in RELATIONS} for f in FIELDS}
    consistency = {key: {**p, "consistency_rate": _pct(p["power_cc_agree"], p["n"]),
                         "co2_agree_rate": _pct(p["co2_agree"], p["co2_comparable"])}
                   for key, p in pairs.items() if p["n"]}
    out = {"make": make, "tozar": tozar, "model_year_from": low, "model_year_to": high, "per_year": per_year,
           "eea_years": [low, high + 1], "eea_rows": len(eea_rows),
           "eea_distinct": {f: len(fields[f].values) for f in FIELDS},
           "n": n, "n_per_year": dict(sorted(years.items())), "without_relation": without,
           "without_relation_pct": _pct(without, n), "relations": relations, "consistency": consistency,
           "examples": {key: rows for key, rows in examples.items() if rows}, "none_examples": unrelated,
           "truncated": truncated, "budget_s": budget_s, "elapsed_s": round(clock() - started, 1),
           "definitions": {"normalization": "NFKC, upper case, no spaces / dots / dashes",
                           "prefix_contains_min_length": MIN_PARTIAL,
                           "power": f"|horsepower x {HP_TO_KW} - EEA kW| <= {POWER_KW_TOLERANCE}",
                           "cc": f"|cc - EEA cc| <= {CC_TOLERANCE:g} (missing = 0)",
                           "co2": "equal when both state co2_wltp",
                           "consistency_rate": "the related rows of which one related EEA row agrees on power and cc"}}
    return out


def type_code_probe(query: Callable[[str, dict], list[dict]] | None, make: Any, year_from: Any, year_to: Any,
                    per_year: Any = DEFAULT_PER_YEAR, *, folder=None, budget_s: float = BUDGET_S,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """The probe of one make (module docstring). Raises ProbeInputError (arguments) and CatalogUnavailable (no MILO,
    or a failed sample query)."""
    from ..catalog import CatalogUnavailable
    from ..db import _jsonable

    name, tozar, low, high, n = arguments(make, year_from, year_to, per_year)
    if query is None:
        raise CatalogUnavailable("DATABASE_URL is not set: the MILO catalogue is not available")
    started = clock()
    try:
        rows = query(SAMPLE_SQL, {"tozar": tozar, "year_from": low, "year_to": high, "per_year": n})
    except Exception as exc:  # noqa: BLE001 - an unreachable MILO is reported, never a fallback
        raise CatalogUnavailable(f"The MILO catalogue query failed: {type(exc).__name__}") from None
    rows = [_jsonable(dict(r)) for r in rows]
    eea_rows = eea_candidates(name, tozar, low, high, folder) if clock() - started < budget_s else []
    out = probe(name, tozar, low, high, n, rows, eea_rows, budget_s=budget_s, started=started, clock=clock)
    if not eea_rows and clock() - started >= budget_s:
        out["truncated"] = True
    log.info("type code probe make=%s years=%d-%d per_year=%d n=%d eea_rows=%d truncated=%s elapsed_s=%s", name, low,
             high, n, out["n"], out["eea_rows"], out["truncated"], out["elapsed_s"])
    return out

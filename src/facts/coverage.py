"""The facts coverage probe (MCP facts_coverage): per model year, how much of a deterministic catalogue sample the facts
API answers from open data, and why the rest is withheld. Read-only and measuring only: it changes nothing the API
returns.

    sample     per model year, the first `per_year` private-segment variant identity keys of
               public.catalog_variants_current by md5(variant_identity_key), optionally one manufacturer (tozar):
               db.LEVEL15_SAMPLE_SQL, one short read-only query per year. The same catalogue gives the same sample
    per key    exactly the record of POST /api/facts/v1/vehicles (debug): government_facts, FactsService.open_part (the
               same admission, cache and versions; the MCP's service writes no cache file), build_record. No network,
               no vPIC
    output     per model year: n, the route split, the open_data_match level distribution, the share of keys with an
               open-data fact (and an EEA fact), per admitted field (data/facts_admission.json) the % returned and the
               withheld counts by reason, and the EEA co2_selection aggregate; per manufacturer (the top 15 by count)
               the share with an EEA fact
    latency    per model year `ms_per_key` {p50, p95, n}: the time of each key's open part + record (nearest rank; a key
               the facts cache already holds is counted with its cache-hit time)
    budget     BUDGET_S per call: when reached, what is done is returned with `truncated: true`
    log        one line per call like the facts API: counts only, never a value

Never a write to MILO.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from ..server_logging import get_logger
from . import versions as V
from .record import build_record, government_facts

MAX_PER_YEAR = 300
DEFAULT_PER_YEAR = 120
MAX_YEARS = 20
BUDGET_S = 90.0
TOP_MANUFACTURERS = 15
EEA = "eea_co2_cars"
ROUTES = ("european", "american", "unknown")
log = get_logger("facts")


class CoverageInputError(ValueError):
    """An argument out of range (the MCP reports it as a rejected call)."""


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _year(value: Any, name: str) -> int:
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise CoverageInputError(f"{name} must be a model year") from None
    if not 1950 <= year <= 2100:
        raise CoverageInputError(f"{name} must be a model year between 1950 and 2100")
    return year


def arguments(year_from: Any, year_to: Any, per_year: Any = DEFAULT_PER_YEAR,
              manufacturer: Any = None) -> tuple[int, int, int, str | None]:
    """The validated (model_year_from, model_year_to, per_year, manufacturer); CoverageInputError otherwise."""
    low, high = _year(year_from, "model_year_from"), _year(year_to, "model_year_to")
    if low > high:
        raise CoverageInputError("model_year_from must not be after model_year_to")
    if high - low + 1 > MAX_YEARS:
        raise CoverageInputError(f"at most {MAX_YEARS} model years per call")
    try:
        n = int(DEFAULT_PER_YEAR if per_year is None else per_year)
    except (TypeError, ValueError):
        raise CoverageInputError("per_year must be a number") from None
    if not 1 <= n <= MAX_PER_YEAR:
        raise CoverageInputError(f"per_year must be between 1 and {MAX_PER_YEAR}")
    maker = str(manufacturer).strip() if manufacturer is not None else ""
    if len(maker) > 80:
        raise CoverageInputError("manufacturer is at most 80 characters")
    return low, high, n, maker or None


def admitted_fields(adm: dict | None = None) -> list[str]:
    adm = V.admission() if adm is None else adm
    return sorted({str(e["field"]) for e in adm.get("entries") or [] if isinstance(e, dict) and e.get("field")})


def _rank(values: list[float], q: float) -> float:
    """The nearest-rank percentile (q in 0..1) of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def latency(ms: list[float]) -> dict:
    """{p50, p95, n} of the per-key times (ms; nearest rank), {n: 0} without a key."""
    if not ms:
        return {"n": 0}
    return {"p50": round(_rank(ms, 0.50), 1), "p95": round(_rank(ms, 0.95), 1), "n": len(ms)}


class _Year:
    """The counters of one model year."""

    def __init__(self, year: int, fields: list[str]):
        self.year, self.fields = year, fields
        self.n = self.sampled = 0
        self.routes = {r: 0 for r in ROUTES}
        self.levels: dict[str, int] = {}
        self.with_open = self.with_eea = 0
        self.returned = {f: 0 for f in fields}
        self.withheld: dict[str, dict[str, int]] = {f: {} for f in fields}
        self.errors: dict[str, int] = {}
        self.ms: list[float] = []
        self.co2 = {"decisions": 0, "by_fallback": {}, "by_rule": {}, "noop": 0, "zero": 0, "withheld_disagree": 0,
                    "withheld_disagree_noop": 0, "withheld_disagree_zero": 0,
                    "withheld_disagree_after_selection": 0}

    def add(self, record: dict) -> bool:
        """Count one debug record; True when it has an EEA fact."""
        self.n += 1
        match = record.get("open_data_match") or {}
        route = match.get("route") if match.get("route") in ROUTES else "unknown"
        self.routes[route] += 1
        level = str(match.get("level") or "none")
        self.levels[level] = self.levels.get(level, 0) + 1
        facts = record.get("facts") or {}
        open_facts = {k: v for k, v in facts.items() if v.get("source_level") == "open_data"}
        eea = any(v.get("source") == EEA for v in open_facts.values())
        self.with_open += bool(open_facts)
        self.with_eea += eea
        for field in self.fields:
            if field in open_facts:
                self.returned[field] += 1
        for item in record.get("withheld") or []:
            field = item.get("field")
            if field in self.withheld and item.get("source") not in ("government", None):
                reasons = self.withheld[field]
                reasons[str(item.get("reason"))] = reasons.get(str(item.get("reason")), 0) + 1
            selection = item.get("co2_selection")
            if item.get("source") == EEA and item.get("reason") == "offer_survivors_disagree" and selection:
                self.co2["withheld_disagree"] += 1
                self.co2["withheld_disagree_noop"] += selection.get("selected") == selection.get("survivors")
                self.co2["withheld_disagree_zero"] += selection.get("selected") == 0
                after = (selection.get("selected_values") or {}).get(field) or []
                self.co2["withheld_disagree_after_selection"] += len(after) > 1
        decision = (record.get("co2_selection") or {}).get(EEA)
        if decision:
            self.co2["decisions"] += 1
            for key, value in (("by_fallback", decision.get("fallback")), ("by_rule", decision.get("rule"))):
                bucket = self.co2[key]
                bucket[str(value)] = bucket.get(str(value), 0) + 1
            self.co2["noop"] += decision.get("selected") == decision.get("survivors") and bool(decision["survivors"])
            self.co2["zero"] += decision.get("selected") == 0
        return eea

    def error(self, name: str) -> None:
        self.errors[name] = self.errors.get(name, 0) + 1

    def view(self, truncated: bool) -> dict:
        out = {"model_year": self.year, "n": self.n, "sampled": self.sampled, "route": dict(self.routes),
               "open_data_match_level": dict(sorted(self.levels.items())),
               "with_open_data_fact": self.with_open, "with_open_data_fact_pct": _pct(self.with_open, self.n),
               "with_eea_fact": self.with_eea, "with_eea_fact_pct": _pct(self.with_eea, self.n),
               "fields": {f: {"returned": self.returned[f], "returned_pct": _pct(self.returned[f], self.n),
                              "withheld": dict(sorted(self.withheld[f].items()))} for f in self.fields},
               "co2_selection": {k: dict(sorted(v.items())) if isinstance(v, dict) else v
                                 for k, v in self.co2.items()}}
        out["ms_per_key"] = latency(self.ms)
        if self.errors:
            out["errors"] = dict(sorted(self.errors.items()))
        if truncated:
            out["truncated"] = True
        return out


def facts_coverage(service, year_from: Any, year_to: Any, per_year: Any = DEFAULT_PER_YEAR, manufacturer: Any = None,
                   *, budget_s: float = BUDGET_S, clock: Callable[[], float] = time.monotonic,
                   timer: Callable[[], float] = time.perf_counter) -> dict:
    """The coverage of a FactsService over the sample (module docstring). Raises CoverageInputError (arguments) and
    CatalogUnavailable (no MILO, or a failed sample query)."""
    from ..facts.service import SnapshotsUnavailable, dumps

    low, high, n, maker = arguments(year_from, year_to, per_year, manufacturer)
    started = clock()
    fields = admitted_fields()
    years: list[dict] = []
    makers: dict[str, list[int]] = {}
    truncated = False
    for year in range(low, high + 1):
        if clock() - started >= budget_s:
            truncated = True
            break
        rows = service.sample_rows(year, n, maker)
        by_key: dict[str, dict] = {}
        for row in rows:                                  # one row per key (the facts API's rule: the last one read)
            by_key[str(row["variant_identity_key"])] = row
        stats = _Year(year, fields)
        stats.sampled = len(by_key)
        cut = False
        for key, row in by_key.items():
            if clock() - started >= budget_s:
                truncated = cut = True
                break
            started_key = timer()
            try:
                gov = government_facts(row)
                part, _ = service.open_part(key, row, gov[0])
            except SnapshotsUnavailable:
                stats.error("snapshots_unavailable")
                continue
            record = build_record(key, row, part, debug=True, government=gov)
            stats.ms.append((timer() - started_key) * 1000.0)
            eea = stats.add(record)
            name = str((record.get("identity") or {}).get("manufacturer") or "unknown")
            counts = makers.setdefault(name, [0, 0])
            counts[0] += 1
            counts[1] += eea
        years.append(stats.view(cut))
        if cut:
            break
    top = sorted(makers.items(), key=lambda item: (-item[1][0], item[0]))[:TOP_MANUFACTURERS]
    out = {"model_year_from": low, "model_year_to": high, "per_year": n, "manufacturer": maker,
           "truncated": truncated, "budget_s": budget_s, "elapsed_s": round(clock() - started, 1),
           "fields": fields, "years": years,
           "manufacturers": [{"manufacturer": name, "n": c[0], "with_eea_fact": c[1], "with_eea_fact_pct": _pct(c[1], c[0])}
                             for name, c in top],
           "versions": V.versions()}
    log.info("facts coverage %s", dumps({
        "years": [low, high], "per_year": n, "manufacturer_filter": maker is not None, "truncated": truncated,
        "elapsed_s": out["elapsed_s"], "n": {y["model_year"]: y["n"] for y in years},
        "with_open_data_fact": {y["model_year"]: y["with_open_data_fact"] for y in years},
        "withheld_disagree": {y["model_year"]: y["co2_selection"]["withheld_disagree"] for y in years},
        "ms_per_key": {y["model_year"]: y["ms_per_key"] for y in years}}))
    return out

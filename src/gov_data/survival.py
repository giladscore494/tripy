"""G3: road survival from the final cancellations (three files, so cancellations before 2010 are not missing from old
cohorts) and the active registry. Aggregates only: the rows are projected while the stream is read and only counts per
key are kept in memory; licence-plate, chassis and engine numbers are never read out of the parser's row.

Cohort: (tozeret_cd, degem_cd, shnat_yitzur): the model year, filled on every cancellation file (the datastore's
moed_aliya_lakvish of a cancellation is a year at most, and mostly empty: it is kept and its fill rate reported, never
used). Denominator: cohort = active vehicles + finally cancelled vehicles of the same key. Inactive vehicles that were
not finally cancelled are not in either file and are excluded; every output says so.

Age in whole years (S1): age = year(bitul_dt) - shnat_yitzur; a cancellation dated before its model year is counted in
`bad_dates` and has no age. Per key: cohort_size, cancelled_count, the age at final cancellation (p25 / median / p75,
whole years, nearest rank) and cancelled_share_by_age[a] for a = 3…20: the share of the cohort with age <= a, only when
reference_year - cohort_year >= a + 1 (the whole cohort has had a full a years; the reference year is the active
registry's last-modified year). A cohort under `min_cohort_size` (200) gets no survival fields.

Key fallback (S2): a cancellation without degem_cd (most pre-2010 rows) takes the degem_cd of its (tozeret_cd,
degem_nm normalized, shnat_yitzur) when the active registry and the keyed cancellations give that triple exactly one
degem_cd; otherwise it is unkeyed (`degem_nm_ambiguous` / `degem_nm_unknown`). Dropping those rows would make the old
cohorts look like they survive better.

Never a silent zero (checks, per resource, after the fallback):
    date_unparsed      under 95 % of a resource's rows parse shnat_yitzur (all rows) or bitul_dt (cancelled)
    unkeyed_rows       over 5 % of a cancellation resource's rows (1 % of the active registry's) still unkeyed
and per cohort: under 95 % of its cancellations with an age -> withheld `undated_cancellations`; the unkeyed
cancellations of its (tozeret_cd, shnat_yitzur) over 10 % of its cancellations -> withheld `unkeyed_cancellations`
(they could be its own: a conservative guard). Reports carry counts and value shapes (digits -> 9, letters -> a) only,
never a value.

Named road_survival / final_cancellation_rate / median_age_at_final_cancellation: the reason for a cancellation is
unknown (accident, total loss, export and failure are not distinguished).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .names import date_pattern, norm_code, norm_name, to_int, year_of, ym_of

DATASET = "road_survival"
BASIS_MODEL_YEAR = "shnat_yitzur"
BASIS_FIRST_ROAD = "first_road_year"             # read by facts.py for snapshots built before the model-year basis
COHORT_COLUMNS = {"tozeret_cd": "INTEGER", "degem_cd": "INTEGER", "cohort_year": "INTEGER", "cohort_size": "INTEGER",
                  "active_count": "INTEGER", "cancelled_count": "INTEGER", "dated_cancelled": "INTEGER",
                  "via_degem_nm": "INTEGER", "unkeyed_same_make_year": "INTEGER",
                  "age_p25": "REAL", "age_median": "REAL", "age_p75": "REAL", "shares": "TEXT", "withheld": "TEXT"}
DATE_MIN_SHARE = 0.95           # a resource's parsed share of each checked column, else date_unparsed
UNKEYED_MAX_SHARE = {"cancelled": 0.05, "active": 0.01}   # after the degem_nm fallback, else unkeyed_rows
COHORT_DATED_MIN_SHARE = 0.95   # a cohort's cancellations with an age, else undated_cancellations
COHORT_UNKEYED_MAX_SHARE = 0.10  # the unkeyed cancellations of its (tozeret_cd, year), else unkeyed_cancellations
CHECKED_COLUMNS = {"active": ("shnat_yitzur",), "cancelled": ("shnat_yitzur", "bitul_dt")}
UNKEYED_REASONS = ("no_tozeret_cd", "no_degem_cd", "degem_nm_ambiguous", "degem_nm_unknown")
WITHHELD_REASONS = ("small_cohort", "unkeyed_cancellations", "undated_cancellations")
SANITY_YEARS = (2005, 2014)
MAP_COLUMNS = {"tozeret_cd": "INTEGER", "degem_cd": "INTEGER", "shnat_yitzur": "INTEGER", "first_road_year": "INTEGER",
               "n": "INTEGER"}
MODEL_COLUMNS = {"tozeret_cd": "INTEGER", "tozeret_nm": "TEXT", "kinuy_mishari": "TEXT", "n": "INTEGER"}
AGE_HE = " הגיל מחושב בשנים שלמות: שנת הביטול פחות שנת הייצור."
DEFINITION_HE = {
    BASIS_FIRST_ROAD: "הקוהורט: כל כלי הרכב מאותו יצרן ודגם (קוד תוצר וקוד דגם) שעלו לכביש באותה שנה — הפעילים "
                      "ואלה שבוטלו ביטול סופי. השיעור לכל גיל הוא חלק הקוהורט שבוטל ביטול סופי עד אותו גיל, "
                      "רק כשכל הקוהורט כבר הגיע לגיל זה. סיבת הביטול אינה ידועה: תאונה, אובדן גמור, ייצוא ותקלה "
                      "אינם מובחנים. כלי רכב לא פעילים שלא בוטלו סופית אינם נכללים.",
    BASIS_MODEL_YEAR: "הקוהורט: כל כלי הרכב מאותו יצרן, דגם ושנת ייצור — הפעילים ואלה שבוטלו ביטול סופי. השיעור "
                      "לכל גיל הוא חלק הקוהורט שבוטל ביטול סופי עד אותו גיל, רק כשכל הקוהורט כבר הגיע לגיל זה. "
                      "סיבת הביטול אינה ידועה: תאונה, אובדן גמור, ייצוא ותקלה אינם מובחנים. כלי רכב לא פעילים "
                      "שלא בוטלו סופית אינם נכללים." + AGE_HE,
}
EXCLUSION = ("Inactive vehicles that were not finally cancelled are excluded: the cohort is active + finally "
             "cancelled vehicles of the same key.")


def _ym(value: Any) -> int | None:
    ym = ym_of(value)
    return ym[0] * 12 + ym[1] - 1 if ym else None


class ResourceStats:
    """Per resource: rows; per checked column (shnat_yitzur, bitul_dt) the parsed count and the shapes of the unparsed
    values; moed_aliya_lakvish filled / parsed (reported, never checked); rows keyed directly, via degem_nm and unkeyed
    by reason (with the shapes of the codes); the ages (whole years) of its cancellations. Counts and shapes only."""

    def __init__(self, role: str):
        self.role, self.rows = role, 0
        self.checked = {c: {"parsed": 0, "patterns": Counter()} for c in CHECKED_COLUMNS[role]}
        self.road = {"filled": 0, "parsed": 0, "patterns": Counter()}
        self.direct = self.via_name = 0
        self.unkeyed: Counter = Counter()
        self.code_patterns: Counter = Counter()
        self.ages: Counter = Counter()

    def check(self, column: str, raw, parsed) -> None:
        item = self.checked[column]
        if parsed is not None:
            item["parsed"] += 1
        else:
            item["patterns"][date_pattern(raw)] += 1

    def share(self, column: str) -> float:
        return round(self.checked[column]["parsed"] / self.rows, 4) if self.rows else 1.0

    def unkeyed_total(self) -> int:
        return sum(self.unkeyed.values())

    def unkeyed_share(self) -> float:
        return round(self.unkeyed_total() / self.rows, 4) if self.rows else 0.0

    def summary(self) -> dict:
        return {"role": self.role, "rows": self.rows, "keyed_directly": self.direct, "keyed_via_degem_nm": self.via_name,
                "unkeyed": self.unkeyed_total(), "unkeyed_share": self.unkeyed_share(),
                "unkeyed_by_reason": {r: self.unkeyed[r] for r in UNKEYED_REASONS if self.unkeyed[r]},
                "unkeyed_code_patterns": self.code_patterns.most_common(5),
                "checked": {c: {"parsed_share": self.share(c), "unparsed_patterns": d["patterns"].most_common(5)}
                            for c, d in self.checked.items()},
                "moed_aliya_lakvish": {"filled_share": round(self.road["filled"] / self.rows, 4) if self.rows else 0.0,
                                       "parsed_share": round(self.road["parsed"] / self.rows, 4) if self.rows else 0.0,
                                       "patterns": self.road["patterns"].most_common(5)},
                "age_years": age_distribution(self.ages) if self.role == "cancelled" else None}


class Survival:
    def __init__(self):
        self.active: Counter = Counter()        # (tc, dc, shnat, road_ym)
        self.cancelled: Counter = Counter()     # (tc, dc, shnat, bitul_year, via_degem_nm 0 | 1)
        self.models: Counter = Counter()        # (tc, tozeret_nm, kinuy_mishari)
        self.key_models: Counter = Counter()    # (tc, dc, shnat, kinuy_mishari): the registry's names per key (G1)
        self.names: dict[tuple, set] = {}       # (tc, degem_nm normalized, shnat) -> {degem_cd}
        self.pending: Counter = Counter()       # (resource, tc, degem_nm normalized, shnat, bitul_year): no degem_cd
        self.unkeyed_by_make_year: Counter = Counter()   # (tc, shnat) -> cancellations left unkeyed by the fallback
        self.rows = {"active": 0, "cancelled": 0}
        self.unkeyed = {"active": 0, "cancelled": 0}
        self.resources: dict[str, ResourceStats] = {}
        self.resolved = False

    def add(self, row: dict) -> None:
        role = "cancelled" if row.get("_role") == "cancelled" else "active"
        rid = row.get("_resource_id") or role
        stats = self.resources.get(rid)
        if stats is None:
            stats = self.resources[rid] = ResourceStats(role)
        self.rows[role] += 1
        stats.rows += 1
        shnat = year_of(row.get("shnat_yitzur"))
        stats.check("shnat_yitzur", row.get("shnat_yitzur"), shnat)
        raw_road = row.get("moed_aliya_lakvish")
        road = _ym(raw_road)
        if raw_road not in (None, ""):
            stats.road["filled"] += 1
            if road is not None:
                stats.road["parsed"] += 1
            else:
                stats.road["patterns"][date_pattern(raw_road)] += 1
        bitul = None
        if role == "cancelled":
            bitul = year_of(row.get("bitul_dt"))
            stats.check("bitul_dt", row.get("bitul_dt"), bitul)
            if shnat is not None and bitul is not None and bitul >= shnat:
                stats.ages[bitul - shnat] += 1
        tc, dc = to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd"))
        name = norm_code(row.get("degem_nm"))
        if tc is None or (dc is None and role == "active"):
            reason = "no_tozeret_cd" if tc is None else "no_degem_cd"
            self.unkeyed[role] += 1
            stats.unkeyed[reason] += 1
            stats.code_patterns[f"{date_pattern(row.get('tozeret_cd'))} / {date_pattern(row.get('degem_cd'))}"] += 1
            return
        if dc is None:                                   # a cancellation without degem_cd: resolved after the read
            self.pending[(rid, tc, name, shnat, bitul)] += 1
            return
        if name:
            self.names.setdefault((tc, name, shnat), set()).add(dc)
        stats.direct += 1
        if role == "cancelled":
            self.cancelled[(tc, dc, shnat, bitul, 0)] += 1
        else:
            self.active[(tc, dc, shnat, road)] += 1
            nm, kinuy = norm_name(row.get("tozeret_nm")), norm_name(row.get("kinuy_mishari"))
            if nm and kinuy:
                self.models[(tc, nm, kinuy)] += 1
            if shnat is not None and kinuy:
                self.key_models[(tc, dc, shnat, kinuy)] += 1

    def resolve(self) -> None:
        """S2: every cancellation without degem_cd takes the one degem_cd its (tozeret_cd, degem_nm, shnat_yitzur)
        maps to, else stays unkeyed (degem_nm_ambiguous / degem_nm_unknown). Once, after every row was read."""
        if self.resolved:
            return
        self.resolved = True
        for (rid, tc, name, shnat, bitul), n in sorted(self.pending.items(), key=lambda kv: tuple(map(str, kv[0]))):
            stats = self.resources[rid]
            found = self.names.get((tc, name, shnat)) if name else None
            if found and len(found) == 1:
                self.cancelled[(tc, next(iter(found)), shnat, bitul, 1)] += n
                stats.via_name += n
                continue
            reason = "degem_nm_ambiguous" if found else "degem_nm_unknown"
            stats.unkeyed[reason] += n
            self.unkeyed["cancelled"] += n
            self.unkeyed_by_make_year[(tc, shnat)] += n
        self.pending.clear()

    def check(self) -> tuple[str | None, list[str]]:
        """(failure reason or None, one line per failing resource): date_unparsed (shnat_yitzur or bitul_dt under 95 %
        parsed) before unkeyed_rows (over 5 % of a cancellation resource's rows, 1 % of the active registry's, still
        unkeyed after the fallback). Counts and shapes only."""
        self.resolve()
        dates, keys = [], []
        for rid, stats in self.resources.items():
            for column, item in stats.checked.items():
                share = stats.share(column)
                if stats.rows and share < DATE_MIN_SHARE:
                    dates.append(f"[{rid}] {column} parsed {share:.2%} of {stats.rows} rows (needs >= "
                                 f"{DATE_MIN_SHARE:.0%}); unparsed shapes {item['patterns'].most_common(10)}")
            limit = UNKEYED_MAX_SHARE[stats.role]
            if stats.unkeyed_share() > limit:
                reasons = {r: stats.unkeyed[r] for r in UNKEYED_REASONS if stats.unkeyed[r]}
                keys.append(f"[{rid}] unkeyed {stats.unkeyed_total()} of {stats.rows} rows "
                            f"({stats.unkeyed_share():.2%}, allowed {limit:.0%}) after the degem_nm fallback "
                            f"({stats.via_name} keyed via degem_nm); by reason {reasons}; code shapes "
                            f"{stats.code_patterns.most_common(10)}")
        if dates:
            return "date_unparsed", dates + keys
        if keys:
            return "unkeyed_rows", keys
        return None, []

    def key_names(self) -> dict[tuple, Counter]:
        """{(tozeret_cd, degem_cd, shnat_yitzur): Counter(kinuy_mishari)} of the active registry."""
        out: dict[tuple, Counter] = {}
        for (tc, dc, shnat, kinuy), n in self.key_models.items():
            out.setdefault((tc, dc, shnat), Counter())[kinuy] += n
        return out

    def year_coverage(self) -> float:
        total = sum(self.cancelled.values())
        filled = sum(n for k, n in self.cancelled.items() if k[2] is not None)
        return round(filled / total, 4) if total else 0.0

    def makes(self) -> dict[int, Counter]:
        out: dict[int, Counter] = {}
        for (tc, name, _), n in self.models.items():
            out.setdefault(tc, Counter())[name] += n
        return out

    def model_names(self) -> Counter:
        out: Counter = Counter()
        for (_, name, kinuy), n in self.models.items():
            out[(name, kinuy)] += n
        return out


def _rank(values: Counter, q: float):
    """The nearest-rank percentile (q in 0..1) of a Counter of numbers, None when empty."""
    total = sum(values.values())
    if not total:
        return None
    rank = max(1, -(-int(q * 1000) * total // 1000))
    seen = 0
    for value in sorted(values):
        seen += values[value]
        if seen >= rank:
            return value
    return None


def age_distribution(ages: Counter) -> dict:
    """{n, p10, p50, p90} of ages in whole years."""
    return {"n": sum(ages.values()), "p10": _rank(ages, 0.1), "p50": _rank(ages, 0.5), "p90": _rank(ages, 0.9)}


def reached(cohort_year: int, age: int, ref_year: int) -> bool:
    """The whole cohort has had a full `age` years by the reference year."""
    return ref_year - cohort_year >= age + 1


def cohorts(agg: Survival, ref_year: int, min_size: int, ages: tuple[int, int]) -> tuple[list[dict], dict]:
    agg.resolve()
    stats = {"active_without_year": 0, "cancelled_without_year": 0, "bad_dates": 0, "undated_cohorts": 0,
             "unkeyed_cohorts": 0}
    data: dict[tuple, dict] = {}

    def item_of(key: tuple) -> dict:
        return data.setdefault(key, {"active": 0, "cancelled": 0, "via": 0, "ages": Counter()})

    for (tc, dc, shnat, _), n in agg.active.items():
        if shnat is None:
            stats["active_without_year"] += n
            continue
        item_of((tc, dc, shnat))["active"] += n
    for (tc, dc, shnat, bitul, via), n in agg.cancelled.items():
        if shnat is None:
            stats["cancelled_without_year"] += n
            continue
        item = item_of((tc, dc, shnat))
        item["cancelled"] += n
        item["via"] += n if via else 0
        if bitul is None:
            continue
        if bitul >= shnat:
            item["ages"][bitul - shnat] += n
        else:
            stats["bad_dates"] += n
    out = []
    low, high = ages
    for (tc, dc, cy), item in data.items():
        size = item["active"] + item["cancelled"]
        nearby = agg.unkeyed_by_make_year.get((tc, cy), 0)
        row = {"tozeret_cd": tc, "degem_cd": dc, "cohort_year": cy, "cohort_size": size,
               "active_count": item["active"], "cancelled_count": item["cancelled"],
               "dated_cancelled": sum(item["ages"].values()), "via_degem_nm": item["via"],
               "unkeyed_same_make_year": nearby, "age_p25": None, "age_median": None, "age_p75": None,
               "shares": None, "withheld": None}
        if size < min_size:
            row["withheld"] = "small_cohort"
        elif nearby > COHORT_UNKEYED_MAX_SHARE * item["cancelled"]:
            row["withheld"] = "unkeyed_cancellations"
            stats["unkeyed_cohorts"] += 1
        elif item["cancelled"] and row["dated_cancelled"] / item["cancelled"] < COHORT_DATED_MIN_SHARE:
            row["withheld"] = "undated_cancellations"
            stats["undated_cohorts"] += 1
        else:
            row.update(age_p25=_rank(item["ages"], 0.25), age_median=_rank(item["ages"], 0.5),
                       age_p75=_rank(item["ages"], 0.75))
            row["shares"] = {str(a): round(sum(n for age, n in item["ages"].items() if age <= a) / size, 4)
                             for a in range(low, high + 1) if reached(cy, a, ref_year)}
        out.append(row)
    return out, stats


def resource_lines(agg: Survival) -> list[str]:
    """Per resource: rows, the parsed share of the checked columns, the moed_aliya_lakvish fill rate, rows keyed
    directly / via degem_nm / unkeyed by reason, and the age distribution (whole years); then the shapes of the
    unparsed values and of the unkeyed codes. Counts and shapes only, never a value."""
    lines = ["", "Per resource (counts only):", "",
             "| resource | role | rows | shnat_yitzur parsed | bitul_dt parsed | moed_aliya_lakvish filled (parsed) | "
             "keyed directly | keyed via degem_nm | unkeyed: no_tozeret_cd / no_degem_cd / degem_nm_ambiguous / "
             "degem_nm_unknown | age years p10 / p50 / p90 (n) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for rid, s in agg.resources.items():
        summary = s.summary()
        road = summary["moed_aliya_lakvish"]
        bitul = f"{s.share('bitul_dt') * 100:.2f} %" if "bitul_dt" in s.checked else "—"
        ages = summary["age_years"]
        age = f"{ages['p10']} / {ages['p50']} / {ages['p90']} ({ages['n']})" if ages else "—"
        unkeyed = " / ".join(str(s.unkeyed[r]) for r in UNKEYED_REASONS)
        lines.append(f"| `{rid}` | {s.role} | {s.rows} | {s.share('shnat_yitzur') * 100:.2f} % | {bitul} | "
                     f"{road['filled_share'] * 100:.2f} % ({road['parsed_share'] * 100:.2f} %) | {s.direct} | "
                     f"{s.via_name} | {unkeyed} ({s.unkeyed_share() * 100:.2f} %) | {age} |")
    shapes = []
    for rid, s in agg.resources.items():
        for column, d in s.checked.items():
            if d["patterns"]:
                shapes.append(f"- `{rid}` {column} unparsed shapes: {d['patterns'].most_common(5)}")
        if s.road["patterns"]:
            shapes.append(f"- `{rid}` moed_aliya_lakvish shapes that are not a month (not used): "
                          f"{s.road['patterns'].most_common(5)}")
        if s.code_patterns:
            shapes.append(f"- `{rid}` unkeyed code shapes (tozeret_cd / degem_cd): {s.code_patterns.most_common(5)}")
    return lines + ([""] + shapes if shapes else [])


def model_year_map(agg: Survival) -> list[tuple]:
    """The active registry's (tozeret_cd, degem_cd, shnat_yitzur, first-road year, n): reported, not used."""
    counts: Counter = Counter()
    for (tc, dc, shnat, road), n in agg.active.items():
        if shnat is not None and road is not None:
            counts[(tc, dc, shnat, road // 12)] += n
    return [(*k, n) for k, n in counts.items()]


def dominant(rows: list[dict]) -> tuple[int | None, float, int]:
    """(first-road year, its share, n) of one (tozeret_cd, degem_cd, shnat_yitzur) in the map."""
    total = sum(int(r["n"]) for r in rows)
    if not total:
        return None, 0.0, 0
    best = max(rows, key=lambda r: (int(r["n"]), -int(r["first_road_year"])))
    return int(best["first_road_year"]), round(int(best["n"]) / total, 4), total


def table_payload(cohort_rows: list[dict], map_rows: list[tuple], agg: Survival) -> dict:
    import json

    return {"cohorts": (COHORT_COLUMNS, [tuple(json.dumps(r[c], sort_keys=True) if c == "shares" and r[c] is not None
                                               else r[c] for c in COHORT_COLUMNS) for r in cohort_rows]),
            "model_year_map": (MAP_COLUMNS, map_rows),
            "models": (MODEL_COLUMNS, [(*k, n) for k, n in agg.models.items()])}


def _dist(values: list[float]) -> dict:
    """{n, p10, p50, p90} of a list of shares (nearest rank)."""
    counts = Counter(values)
    return {"n": len(values), "p10": _rank(counts, 0.1), "p50": _rank(counts, 0.5), "p90": _rank(counts, 0.9)}


def report(agg: Survival, coverage: float, cohort_rows: list[dict], stats: dict, map_rows: list[tuple],
           min_size: int, ref_year: int) -> tuple[list[str], dict]:
    basis = BASIS_MODEL_YEAR
    served = sum(1 for r in cohort_rows if r["shares"] is not None)
    withheld = {reason: sum(1 for r in cohort_rows if r["withheld"] == reason) for reason in WITHHELD_REASONS}
    via = _dist([round(r["via_degem_nm"] / r["cancelled_count"], 4) for r in cohort_rows if r["cancelled_count"]])
    low, high = SANITY_YEARS
    sanity = _dist([r["shares"]["10"] for r in cohort_rows
                    if r["shares"] and "10" in r["shares"] and low <= r["cohort_year"] <= high])
    unkeyed_by_reason = Counter()
    for s in agg.resources.values():
        unkeyed_by_reason.update(s.unkeyed)
    summary = {"rows": dict(agg.rows), "unkeyed": dict(agg.unkeyed),
               "unkeyed_by_reason": {r: unkeyed_by_reason[r] for r in UNKEYED_REASONS if unkeyed_by_reason[r]},
               "keyed_via_degem_nm": sum(s.via_name for s in agg.resources.values()),
               "shnat_yitzur_coverage": coverage, "cohort_basis": basis, "cohorts": len(cohort_rows),
               "cohorts_with_survival": served, "withheld": withheld, "reference_year": ref_year,
               "exclusion": EXCLUSION, "via_degem_nm_share_per_cohort": via,
               "share_by_10_model_years_2005_2014": sanity, **stats}
    lines = [f"- rows: active {agg.rows['active']}, finally cancelled {agg.rows['cancelled']}; keyed via degem_nm "
             f"{summary['keyed_via_degem_nm']}; still unkeyed: active {agg.unkeyed['active']}, cancelled "
             f"{agg.unkeyed['cancelled']} (by reason {summary['unkeyed_by_reason'] or 'none'})",
             f"- cohort basis **{basis}** (shnat_yitzur on the cancellation rows: {coverage * 100:.1f} %); age in "
             f"whole years = year(bitul_dt) − shnat_yitzur; reference year {ref_year}",
             f"- {EXCLUSION}",
             f"- rows without a model year: active {stats['active_without_year']}, cancelled "
             f"{stats['cancelled_without_year']}; cancellations dated before their model year (bad_dates, no age): "
             f"{stats['bad_dates']}",
             f"- cohorts {len(cohort_rows)}, with survival fields: {served}; withheld: small_cohort (< {min_size}) "
             f"{withheld['small_cohort']}, unkeyed_cancellations (the unkeyed cancellations of its (tozeret_cd, "
             f"shnat_yitzur) over {COHORT_UNKEYED_MAX_SHARE:.0%} of its cancellations) "
             f"{withheld['unkeyed_cancellations']}, undated_cancellations (under {COHORT_DATED_MIN_SHARE:.0%} of the "
             f"cancellations with an age) {withheld['undated_cancellations']}",
             f"- share of a cohort's cancellations keyed via degem_nm (cohorts with a cancellation, n {via['n']}): "
             f"p50 {via['p50']}, p90 {via['p90']}",
             f"- sanity: share by age 10 over the cohorts of model years {low}–{high} with survival fields (n "
             f"{sanity['n']}): p10 {sanity['p10']}, p50 {sanity['p50']}, p90 {sanity['p90']}"]
    lines += resource_lines(agg)
    summary["resources"] = {rid: s.summary() for rid, s in agg.resources.items()}
    by_my: dict[tuple, list[dict]] = {}
    for tc, dc, my, fry, n in map_rows:
        by_my.setdefault((tc, dc, my), []).append({"first_road_year": fry, "n": n})
    largest = sorted(cohort_rows, key=lambda r: (-r["cohort_size"], r["tozeret_cd"], r["degem_cd"],
                                                 r["cohort_year"]))[:20]
    if largest:
        lines += ["", "20 largest cohorts:", "",
                  f"| tozeret_cd | degem_cd | {basis} | cohort | active | cancelled | via degem_nm | median age | "
                  "share by 10 | withheld |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in largest:
            share = (r["shares"] or {}).get("10")
            lines.append(f"| {r['tozeret_cd']} | {r['degem_cd']} | {r['cohort_year']} | {r['cohort_size']} | "
                         f"{r['active_count']} | {r['cancelled_count']} | {r['via_degem_nm']} | "
                         f"{r['age_median'] if r['age_median'] is not None else '—'} | "
                         f"{share if share is not None else '—'} | {r['withheld'] or '—'} |")
    return lines, summary

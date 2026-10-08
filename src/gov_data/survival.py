"""G3: road survival from the final cancellations (three files, so cancellations before 2010 are not missing from old
cohorts) and the active registry. Aggregates only: the rows are projected while the stream is read and only counts per
key are kept in memory; licence-plate, chassis and engine numbers are never read out of the parser's row.

The year problem: shnat_yitzur is reported to be filled for only ~10 % of cancellation rows. The build measures its
coverage; under `year_coverage_min` (95 %) the cohort is the FIRST-ON-ROAD year (moed_aliya_lakvish) for both the
cancelled and the active vehicles, keyed (tozeret_cd, degem_cd, first_road_year); otherwise (tozeret_cd, degem_cd,
shnat_yitzur). The mapping from a catalogue model year to a first-road year is measured on the active registry
(`model_year_map`) and reported; a variant gets a cohort only when one first-road year holds at least
`model_year_min_share` of its model year's vehicles. It is never assumed.

Denominator: cohort = active vehicles + finally cancelled vehicles of the same key. Inactive vehicles that were not
finally cancelled are not in either file and are excluded; every output says so.

Per key: cohort_size, cancelled_count, the age at final cancellation (p25 / median / p75, years, from the first-on-road
month to the cancellation month) and cancelled_share_by_age[a] for a = 3…20: the share of the cohort cancelled at an
age <= a years, only when the whole cohort has reached age a by the reference month (the active registry's
last-modified month). A cohort under `min_cohort_size` (200) gets no survival fields.

Named road_survival / final_cancellation_rate / median_age_at_final_cancellation: the reason for a cancellation is
unknown (accident, total loss, export and failure are not distinguished).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .names import norm_name, to_int, ym_of

DATASET = "road_survival"
BASIS_MODEL_YEAR = "shnat_yitzur"
BASIS_FIRST_ROAD = "first_road_year"
COHORT_COLUMNS = {"tozeret_cd": "INTEGER", "degem_cd": "INTEGER", "cohort_year": "INTEGER", "cohort_size": "INTEGER",
                  "active_count": "INTEGER", "cancelled_count": "INTEGER", "dated_cancelled": "INTEGER",
                  "age_p25": "REAL", "age_median": "REAL", "age_p75": "REAL", "shares": "TEXT"}
MAP_COLUMNS = {"tozeret_cd": "INTEGER", "degem_cd": "INTEGER", "shnat_yitzur": "INTEGER", "first_road_year": "INTEGER",
               "n": "INTEGER"}
MODEL_COLUMNS = {"tozeret_cd": "INTEGER", "tozeret_nm": "TEXT", "kinuy_mishari": "TEXT", "n": "INTEGER"}
DEFINITION_HE = {
    BASIS_FIRST_ROAD: "הקוהורט: כל כלי הרכב מאותו יצרן ודגם (קוד תוצר וקוד דגם) שעלו לכביש באותה שנה — הפעילים "
                      "ואלה שבוטלו ביטול סופי. השיעור לכל גיל הוא חלק הקוהורט שבוטל ביטול סופי עד אותו גיל, "
                      "רק כשכל הקוהורט כבר הגיע לגיל זה. סיבת הביטול אינה ידועה: תאונה, אובדן גמור, ייצוא ותקלה "
                      "אינם מובחנים. כלי רכב לא פעילים שלא בוטלו סופית אינם נכללים.",
    BASIS_MODEL_YEAR: "הקוהורט: כל כלי הרכב מאותו יצרן, דגם ושנת ייצור — הפעילים ואלה שבוטלו ביטול סופי. השיעור "
                      "לכל גיל הוא חלק הקוהורט שבוטל ביטול סופי עד אותו גיל, רק כשכל הקוהורט כבר הגיע לגיל זה. "
                      "סיבת הביטול אינה ידועה: תאונה, אובדן גמור, ייצוא ותקלה אינם מובחנים. כלי רכב לא פעילים "
                      "שלא בוטלו סופית אינם נכללים.",
}
EXCLUSION = ("Inactive vehicles that were not finally cancelled are excluded: the cohort is active + finally "
             "cancelled vehicles of the same key.")


def _ym(value: Any) -> int | None:
    ym = ym_of(value)
    return ym[0] * 12 + ym[1] - 1 if ym else None


class Survival:
    def __init__(self):
        self.active: Counter = Counter()        # (tc, dc, shnat, road_ym)
        self.cancelled: Counter = Counter()     # (tc, dc, shnat, road_ym, bitul_ym)
        self.models: Counter = Counter()        # (tc, tozeret_nm, kinuy_mishari)
        self.rows = {"active": 0, "cancelled": 0}
        self.unkeyed = {"active": 0, "cancelled": 0}

    def add(self, row: dict) -> None:
        role = "cancelled" if row.get("_role") == "cancelled" else "active"
        self.rows[role] += 1
        tc, dc = to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd"))
        if tc is None or dc is None:
            self.unkeyed[role] += 1
            return
        shnat, road = to_int(row.get("shnat_yitzur")), _ym(row.get("moed_aliya_lakvish"))
        if role == "cancelled":
            self.cancelled[(tc, dc, shnat, road, _ym(row.get("bitul_dt")))] += 1
        else:
            self.active[(tc, dc, shnat, road)] += 1
            name, kinuy = norm_name(row.get("tozeret_nm")), norm_name(row.get("kinuy_mishari"))
            if name and kinuy:
                self.models[(tc, name, kinuy)] += 1

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


def _percentile(ages: Counter, q: float) -> float | None:
    """Nearest rank on the sorted ages (months), in years with one decimal."""
    total = sum(ages.values())
    if not total:
        return None
    rank = max(1, -(-int(q * 1000) * total // 1000))
    seen = 0
    for age in sorted(ages):
        seen += ages[age]
        if seen >= rank:
            return round(age / 12.0, 1)
    return None


def reached(cohort_year: int, age: int, ref_ym: int) -> bool:
    """The whole cohort (its youngest member entered in December of the cohort year) is at least `age` years old."""
    return ref_ym >= cohort_year * 12 + 11 + 12 * age


def cohorts(agg: Survival, basis: str, ref_ym: int, min_size: int, ages: tuple[int, int]) -> tuple[list[dict], dict]:
    index = 2 if basis == BASIS_MODEL_YEAR else 3
    stats = {"active_without_year": 0, "cancelled_without_year": 0, "bad_dates": 0}
    data: dict[tuple, dict] = {}

    def year(key: tuple) -> int | None:
        value = key[index]
        return value if basis == BASIS_MODEL_YEAR or value is None else value // 12

    for key, n in agg.active.items():
        cy = year(key)
        if cy is None:
            stats["active_without_year"] += n
            continue
        data.setdefault((key[0], key[1], cy), {"active": 0, "cancelled": 0, "ages": Counter()})["active"] += n
    for key, n in agg.cancelled.items():
        cy = year(key)
        if cy is None:
            stats["cancelled_without_year"] += n
            continue
        item = data.setdefault((key[0], key[1], cy), {"active": 0, "cancelled": 0, "ages": Counter()})
        item["cancelled"] += n
        road, bitul = key[3], key[4]
        if road is not None and bitul is not None:
            if bitul >= road:
                item["ages"][bitul - road] += n
            else:
                stats["bad_dates"] += n
    out = []
    low, high = ages
    for (tc, dc, cy), item in data.items():
        size = item["active"] + item["cancelled"]
        row = {"tozeret_cd": tc, "degem_cd": dc, "cohort_year": cy, "cohort_size": size,
               "active_count": item["active"], "cancelled_count": item["cancelled"],
               "dated_cancelled": sum(item["ages"].values()), "age_p25": None, "age_median": None, "age_p75": None,
               "shares": None}
        if size >= min_size:
            row.update(age_p25=_percentile(item["ages"], 0.25), age_median=_percentile(item["ages"], 0.5),
                       age_p75=_percentile(item["ages"], 0.75))
            shares = {}
            for a in range(low, high + 1):
                if reached(cy, a, ref_ym):
                    cancelled_by = sum(n for months, n in item["ages"].items() if months <= 12 * a)
                    shares[str(a)] = round(cancelled_by / size, 4)
            row["shares"] = shares
        out.append(row)
    return out, stats


def model_year_map(agg: Survival) -> list[tuple]:
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


def report(agg: Survival, coverage: float, basis: str, cohort_rows: list[dict], stats: dict, map_rows: list[tuple],
           min_size: int, min_share: float, ref_ym: int) -> tuple[list[str], dict]:
    by_my: dict[tuple, list[dict]] = {}
    for tc, dc, my, fry, n in map_rows:
        by_my.setdefault((tc, dc, my), []).append({"first_road_year": fry, "n": n})
    dominant_ok = sum(1 for rows in by_my.values() if dominant(rows)[1] >= min_share)
    served = sum(1 for r in cohort_rows if r["shares"] is not None)
    summary = {"rows": dict(agg.rows), "unkeyed": dict(agg.unkeyed), "shnat_yitzur_coverage": coverage,
               "cohort_basis": basis, "cohorts": len(cohort_rows), "cohorts_with_survival": served,
               "reference_month": f"{ref_ym // 12}-{ref_ym % 12 + 1:02d}", "exclusion": EXCLUSION,
               "model_year_keys": len(by_my), "model_year_keys_dominant": dominant_ok, **stats}
    lines = [f"- rows: active {agg.rows['active']}, finally cancelled {agg.rows['cancelled']} (unkeyed: active "
             f"{agg.unkeyed['active']}, cancelled {agg.unkeyed['cancelled']})",
             f"- shnat_yitzur coverage on the cancellation rows: {coverage * 100:.1f} % -> cohort basis **{basis}**",
             f"- {EXCLUSION}",
             f"- rows without a cohort year: active {stats['active_without_year']}, cancelled "
             f"{stats['cancelled_without_year']}; cancellations dated before their first-on-road month: "
             f"{stats['bad_dates']}",
             f"- cohorts {len(cohort_rows)}, with survival fields (cohort_size >= {min_size}): {served}; reference "
             f"month {summary['reference_month']}"]
    if basis == BASIS_FIRST_ROAD:
        pct = round(100.0 * dominant_ok / len(by_my), 1) if by_my else 0.0
        lines.append(f"- model year -> first-road year (active registry): {len(by_my)} (tozeret_cd, degem_cd, "
                     f"shnat_yitzur) keys; one first-road year holds >= {min_share * 100:.0f} % in {dominant_ok} "
                     f"({pct} %): only those variants get road_survival")
    largest = sorted(cohort_rows, key=lambda r: (-r["cohort_size"], r["tozeret_cd"], r["degem_cd"],
                                                 r["cohort_year"]))[:20]
    if largest:
        lines += ["", "20 largest cohorts:", "",
                  f"| tozeret_cd | degem_cd | {basis} | cohort | active | cancelled | median age | share by 10 |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in largest:
            share = (r["shares"] or {}).get("10")
            lines.append(f"| {r['tozeret_cd']} | {r['degem_cd']} | {r['cohort_year']} | {r['cohort_size']} | "
                         f"{r['active_count']} | {r['cancelled_count']} | {r['age_median'] if r['age_median'] is not None else '—'} | "
                         f"{share if share is not None else '—'} |")
    return lines, summary

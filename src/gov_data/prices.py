"""G1: the new-car price list (resource 39f455bf-...; licence as the package states it).

Key (tozeret_cd, degem_cd, shnat_yitzur). The price list has no trim (ramat_gimur) and no price date, while the
catalogue's variant identity includes the trim: one key can carry several prices (trims, or price updates). Nothing
is dropped: every distinct (mehir, kinuy_mishari, shem_yevuan, + the kept columns) of a key is stored with its row
count. A row whose key or price does not parse is counted (`unkeyed`), never stored.

Facts (facts.py): the key's degem_cd is not unique per model (19 / 10 / 2007: A3 and Q7), so the rows are first
filtered by model name (`by_model_name`): the rows whose norm_model(kinuy_mishari) equals the catalogue row's; none ->
the key's rows only when every row carries one single kinuy_mishari, else withheld `price_model_ambiguous`. Then one
distinct mehir -> {value}; several -> only {range: [min, max], n_prices}, never one value; `original_importer` only
when those rows have a single shem_yevuan. A list price, never a transaction price.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .names import norm_model, to_int, to_number

DATASET = "new_car_prices"
COLUMNS = {"tozeret_cd": "INTEGER", "degem_cd": "INTEGER", "shnat_yitzur": "INTEGER", "mehir": "INTEGER",
           "kinuy_mishari": "TEXT", "shem_yevuan": "TEXT", "semel_yevuan": "TEXT", "sug_degem": "TEXT",
           "tozeret_nm": "TEXT", "degem_nm": "TEXT", "rows": "INTEGER"}
TEXT = ("kinuy_mishari", "shem_yevuan", "semel_yevuan", "sug_degem", "tozeret_nm", "degem_nm")


def _text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


class Prices:
    def __init__(self):
        self.groups: Counter = Counter()
        self.rows = 0
        self.unkeyed = 0

    def add(self, row: dict) -> None:
        self.rows += 1
        key = (to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd")), to_int(row.get("shnat_yitzur")))
        number = to_number(row.get("mehir"))            # a price may be stated with thousands separators
        price = int(number) if number is not None and number.is_integer() else None
        if None in key or price is None or price <= 0:
            self.unkeyed += 1
            return
        self.groups[(*key, price, *(_text(row.get(c)) for c in TEXT))] += 1

    def table_rows(self) -> list[tuple]:
        return [(*group, n) for group, n in self.groups.items()]

    def by_key(self) -> dict[tuple, list[tuple]]:
        out: dict[tuple, list[tuple]] = {}
        for group, n in self.groups.items():
            out.setdefault(group[:3], []).append((*group[3:], n))
        return out

    def makes(self) -> dict[int, Counter]:
        """{tozeret_cd: Counter(tozeret_nm)} (for the recall code-agreement check)."""
        out: dict[int, Counter] = {}
        for group, n in self.groups.items():
            if group[8]:
                out.setdefault(group[0], Counter())[group[8]] += n
        return out

    def models(self) -> Counter:
        """Counter((tozeret_nm, kinuy_mishari)) (the catalogue model names the recall map is proposed from)."""
        out: Counter = Counter()
        for group, n in self.groups.items():
            if group[8] and group[4]:
                out[(group[8], group[4])] += n
        return out


BY_NAME, SINGLE_NAME_KEY, AMBIGUOUS = "name", "single_name_key", "price_model_ambiguous"


def by_model_name(rows: list, kinuy: Any, name_of) -> tuple[str, list]:
    """(how, rows) of one key for a catalogue model name: `name` (the rows of that normalized kinuy_mishari),
    `single_name_key` (no row of that name, and every row of the key carries one single kinuy_mishari: all of them) or
    `price_model_ambiguous` (no rows)."""
    want = norm_model(kinuy)
    if want:
        matched = [r for r in rows if norm_model(name_of(r)) == want]
        if matched:
            return BY_NAME, matched
    if len({norm_model(name_of(r)) for r in rows}) == 1:
        return SINGLE_NAME_KEY, list(rows)
    return AMBIGUOUS, []


def name_resolution(agg: Prices, registry_names: Counter) -> tuple[list[str], dict]:
    """F3: keys whose rows carry more than one kinuy_mishari, and how the registry's (key, kinuy_mishari) pairs that
    join the price list resolve (by name / by a single-name key / withheld): distinct pairs and vehicles."""
    by_key = agg.by_key()
    multi = sum(1 for groups in by_key.values() if len({norm_model(g[1]) for g in groups}) > 1)
    pairs = {BY_NAME: 0, SINGLE_NAME_KEY: 0, AMBIGUOUS: 0}
    vehicles = dict(pairs)
    for (tc, dc, shnat, kinuy), n in registry_names.items():
        groups = by_key.get((tc, dc, shnat))
        if not groups:
            continue
        how, _ = by_model_name(groups, kinuy, lambda g: g[1])
        pairs[how] += 1
        vehicles[how] += n

    def pct(part: int, counts: dict) -> str:
        total = sum(counts.values())
        return f"{part / total * 100:.1f} %" if total else "—"
    stats = {"keys_multi_kinuy": multi, "registry_pairs": pairs, "registry_vehicles": vehicles}
    lines = [f"- keys whose rows carry more than one kinuy_mishari: {multi} of {len(by_key)}",
             f"- the price per model name over the registry's (tozeret_cd, degem_cd, shnat_yitzur, kinuy_mishari) "
             f"pairs that join the price list: {sum(pairs.values())} pairs ({sum(vehicles.values())} vehicles):",
             "", "| resolved | pairs | share | vehicles | share |", "|---|---|---|---|---|"]
    lines += [f"| {how} | {pairs[how]} | {pct(pairs[how], pairs)} | {vehicles[how]} | {pct(vehicles[how], vehicles)} |"
              for how in (BY_NAME, SINGLE_NAME_KEY, AMBIGUOUS)]
    return lines, stats


def tables(agg: Prices) -> dict:
    return {"prices": (COLUMNS, agg.table_rows())}


def report(agg: Prices, registry_keys: set[str] | None) -> tuple[list[str], dict]:
    by_key = agg.by_key()
    distinct = {k: sorted({g[0] for g in groups}) for k, groups in by_key.items()}
    buckets = {"1": 0, "2-3": 0, ">3": 0}
    for prices in distinct.values():
        buckets["1" if len(prices) == 1 else "2-3" if len(prices) <= 3 else ">3"] += 1
    stats: dict[str, Any] = {"rows": agg.rows, "unkeyed_rows": agg.unkeyed, "keys": len(by_key),
                             "distinct_price_buckets": buckets}
    lines = [f"- rows {agg.rows}, unkeyed (key or price not parsed) {agg.unkeyed}, keys {len(by_key)}",
             f"- keys with 1 distinct price: {buckets['1']}; 2–3: {buckets['2-3']}; more than 3: {buckets['>3']}"]
    if registry_keys is not None:
        matched = sum(1 for k in by_key if "|".join(str(p) for p in k) in registry_keys)
        pct = round(100.0 * matched / len(by_key), 1) if by_key else 0.0
        stats["registry_join"] = {"matched": matched, "unmatched": len(by_key) - matched, "pct": pct}
        lines.append(f"- join against the gov registry index (tozeret_cd|degem_cd|shnat_yitzur): {matched} matched, "
                     f"{len(by_key) - matched} unmatched ({pct} %)")
    else:
        lines.append("- join against the gov registry index: the index is not available in this checkout")
    multi = sorted(((k, distinct[k]) for k in by_key if len(distinct[k]) > 1),
                   key=lambda kv: (-len(kv[1]), kv[0]))[:20]
    if multi:
        lines += ["", "20 multi-price keys (largest number of distinct prices first):", "",
                  "| tozeret_cd | degem_cd | shnat_yitzur | distinct prices | min – max | kinuy_mishari values |",
                  "|---|---|---|---|---|---|"]
        for key, prices in multi:
            names = sorted({g[1] for g in by_key[key] if g[1]})
            lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {len(prices)} | {prices[0]} – {prices[-1]} | "
                         f"{', '.join(names) or '—'} |")
    stats["multi_price_examples"] = [{"key": list(k), "n_prices": len(p), "kinuy_mishari":
                                      sorted({g[1] for g in by_key[k] if g[1]})} for k, p in multi]
    return lines, stats

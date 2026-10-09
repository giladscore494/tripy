"""G1: the new-car price list (resource 39f455bf-...; licence as the package states it).

Key (tozeret_cd, degem_cd, shnat_yitzur). The price list has no trim (ramat_gimur) and no price date, while the
catalogue's variant identity includes the trim: one key can carry several prices (trims, or price updates). Nothing
is dropped: every distinct (mehir, kinuy_mishari, shem_yevuan, + the kept columns) of a key is stored with its row
count. A row whose key or price does not parse is counted (`unkeyed`), never stored.

The price-list degem_cd is not unique per model (19/10/2007 carries A3 and Q7), so the facts side first selects the
key's rows by model name (`select_by_model`): the rows whose normalized kinuy_mishari equals the catalogue row's
(names.norm_model); if none does, all the key's rows only when every one of them carries one single kinuy_mishari;
otherwise the price is withheld (`price_model_ambiguous`). Then: one distinct mehir -> {value}; several -> only
{range: [min, max], n_prices}, never one value; `original_importer` only when the selected rows have a single
shem_yevuan. A list price, never a transaction price.

Over 1 % of the rows without a code key (tozeret_cd / degem_cd / shnat_yitzur) fails the dataset (`unkeyed_rows`,
provenance.py) with 10 raw code examples.
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
UNKEYED_MAX_SHARE = 0.01
BY_NAME, SINGLE_NAME_KEY, AMBIGUOUS = "kinuy_mishari", "single_name_key", "price_model_ambiguous"


def select_by_model(rows: list[dict], kinuy: Any) -> tuple[list[dict], str]:
    """(the rows of one key that price this catalogue model, how they were chosen): the rows whose normalized
    kinuy_mishari equals the catalogue's (BY_NAME); else all the rows when every one carries the same single name
    (SINGLE_NAME_KEY); else none (AMBIGUOUS)."""
    wanted = norm_model(kinuy)
    if wanted:
        named = [r for r in rows if norm_model(r.get("kinuy_mishari")) == wanted]
        if named:
            return named, BY_NAME
    names = {norm_model(r.get("kinuy_mishari")) for r in rows}
    if len(names) == 1 and "" not in names:
        return list(rows), SINGLE_NAME_KEY
    return [], AMBIGUOUS


def _text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


class Prices:
    def __init__(self):
        self.groups: Counter = Counter()
        self.rows = 0
        self.unkeyed = 0                    # key or price not parsed
        self.unkeyed_codes = 0              # key not parsed (the unkeyed_rows check)
        self.unkeyed_examples: list[dict] = []

    def add(self, row: dict) -> None:
        self.rows += 1
        key = (to_int(row.get("tozeret_cd")), to_int(row.get("degem_cd")), to_int(row.get("shnat_yitzur")))
        number = to_number(row.get("mehir"))
        price = int(number) if number is not None and number.is_integer() else None
        if None in key:
            self.unkeyed_codes += 1
            if len(self.unkeyed_examples) < 10:
                self.unkeyed_examples.append({c: row.get(c) for c in ("tozeret_cd", "degem_cd", "shnat_yitzur")})
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

    def check(self) -> str | None:
        """The unkeyed_rows failure detail, or None."""
        share = self.unkeyed_codes / self.rows if self.rows else 0.0
        if share > UNKEYED_MAX_SHARE:
            return (f"unkeyed {self.unkeyed_codes} of {self.rows} rows ({share:.2%}, allowed {UNKEYED_MAX_SHARE:.0%}); "
                    f"code examples {self.unkeyed_examples[:10]}")
        return None

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


def tables(agg: Prices) -> dict:
    return {"prices": (COLUMNS, agg.table_rows())}


def model_resolution(agg: Prices, key_names: dict[tuple, Counter] | None) -> dict | None:
    """How the registry's (key, kinuy_mishari) pairs of the price-list keys resolve under select_by_model: counts of
    pairs (and vehicles) by name, by single-name key and withheld."""
    if key_names is None:
        return None
    by_key = agg.by_key()
    pairs = {BY_NAME: 0, SINGLE_NAME_KEY: 0, AMBIGUOUS: 0}
    vehicles = dict.fromkeys(pairs, 0)
    for key, names in key_names.items():
        groups = by_key.get(key)
        if not groups:
            continue
        rows = [{"kinuy_mishari": g[1]} for g in groups]
        for kinuy, n in names.items():
            how = select_by_model(rows, kinuy)[1]
            pairs[how] += 1
            vehicles[how] += n
    total = sum(pairs.values())
    return {"pairs": pairs, "vehicles": vehicles, "pairs_total": total,
            "share": {k: round(v / total, 4) if total else 0.0 for k, v in pairs.items()}}


def report(agg: Prices, registry_keys: set[str] | None,
           key_names: dict[tuple, Counter] | None = None) -> tuple[list[str], dict]:
    by_key = agg.by_key()
    distinct = {k: sorted({g[0] for g in groups}) for k, groups in by_key.items()}
    buckets = {"1": 0, "2-3": 0, ">3": 0}
    for prices in distinct.values():
        buckets["1" if len(prices) == 1 else "2-3" if len(prices) <= 3 else ">3"] += 1
    multi_name = sum(1 for groups in by_key.values() if len({norm_model(g[1]) for g in groups if g[1]}) > 1)
    stats: dict[str, Any] = {"rows": agg.rows, "unkeyed_rows": agg.unkeyed, "unkeyed_codes": agg.unkeyed_codes,
                             "keys": len(by_key), "distinct_price_buckets": buckets, "multi_name_keys": multi_name}
    lines = [f"- rows {agg.rows}, unkeyed (key or price not parsed) {agg.unkeyed}, of which no code key "
             f"{agg.unkeyed_codes}, keys {len(by_key)}",
             f"- keys with 1 distinct price: {buckets['1']}; 2–3: {buckets['2-3']}; more than 3: {buckets['>3']}",
             f"- keys whose rows carry more than one kinuy_mishari: {multi_name}"]
    resolution = model_resolution(agg, key_names)
    if resolution is None:
        lines.append("- price per model name: the registry's names per key are not available (road_survival was not "
                     "read in this run)")
    else:
        stats["model_resolution"] = resolution
        p, v, sh = resolution["pairs"], resolution["vehicles"], resolution["share"]
        lines.append(f"- price per model name, over the active registry's {resolution['pairs_total']} (key, "
                     f"kinuy_mishari) pairs whose key is in the price list: by name {p[BY_NAME]} "
                     f"({sh[BY_NAME] * 100:.1f} %, {v[BY_NAME]} vehicles), by single-name key {p[SINGLE_NAME_KEY]} "
                     f"({sh[SINGLE_NAME_KEY] * 100:.1f} %, {v[SINGLE_NAME_KEY]} vehicles), withheld "
                     f"price_model_ambiguous {p[AMBIGUOUS]} ({sh[AMBIGUOUS] * 100:.1f} %, {v[AMBIGUOUS]} vehicles)")
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

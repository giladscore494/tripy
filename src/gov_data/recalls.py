"""G2: the recall notices (resource 2c33523f-...; licence as the package states it, CC BY at the time of writing).

Stored per notice row: RECALL_ID, the make (TOZAR_TEUR -> data/make_canonical.json, names.makes_of_name: exactly one
plain canonical make, else none), TOZAR_CD, DEGEM (as stated and normalized), the production range BUILD_BEGIN_A –
BUILD_END_A (as stated and its years), SHNAT_RECALL, SUG_RECALL, SUG_TAKALA, TEUR_TAKALA, OFEN_TIKUN, TKINA_EU,
YEVUAN_TEUR, and a match_status. TELEPHONE and WEBSITE are never read (schemas `never`).

TOZAR_CD is not assumed to be the registry's tozeret_cd: the build measures, over the distinct TOZAR_CD values, the
share that equal a registry tozeret_cd whose tozeret_nm normalizes to the same canonical make as the notice's
TOZAR_TEUR. Only at >= 98 % (tozar_cd_key_min_agreement) is the code used as a make key, and the measured rate is
recorded either way.

Matching, exact only (no fuzzy matching, no model):
    1. make            TOZAR_TEUR normalized through data/make_canonical.json (or the code key, when proven)
    2. model           data/gov/recall_model_map.json, reviewed: (canonical make, DEGEM normalized) -> catalogue
                       kinuy_mishari values. The build adds an entry only when a catalogue kinuy_mishari of that make
                       is exactly equal to DEGEM after normalization (names.norm_model); everything else is listed
                       under `unresolved` for the reviewer
    3. production      BUILD_BEGIN_A – BUILD_END_A overlapping the variant's model year (both years stated, else the
                       variant's recalls are not resolved)
A notice without a resolved make or a map entry is match_status `unresolved`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .names import makes_of_name, norm_model, norm_name, to_int, year_of

DATASET = "recall_notices"
MAP_VERSION = "recall-model-map-v1"
COLUMNS = {"recall_id": "TEXT", "make": "TEXT", "tozar_teur": "TEXT", "tozar_cd": "INTEGER", "degem": "TEXT",
           "degem_norm": "TEXT", "build_begin": "TEXT", "build_end": "TEXT", "begin_year": "INTEGER",
           "end_year": "INTEGER", "recall_year": "INTEGER", "sug_recall": "TEXT", "affected_system": "TEXT",
           "fault_description": "TEXT", "repair_method": "TEXT", "tkina_eu": "TEXT", "importer": "TEXT",
           "match_status": "TEXT", "rows": "INTEGER"}


def _text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


class Recalls:
    def __init__(self):
        self.rows: Counter = Counter()
        self.count = 0

    def add(self, row: dict) -> None:
        self.count += 1
        self.rows[(_text(row.get("RECALL_ID")), _text(row.get("TOZAR_TEUR")), to_int(row.get("TOZAR_CD")),
                   _text(row.get("DEGEM")), _text(row.get("BUILD_BEGIN_A")), _text(row.get("BUILD_END_A")),
                   to_int(row.get("SHNAT_RECALL")), _text(row.get("SUG_RECALL")), _text(row.get("SUG_TAKALA")),
                   _text(row.get("TEUR_TAKALA")), _text(row.get("OFEN_TIKUN")), _text(row.get("TKINA_EU")),
                   _text(row.get("YEVUAN_TEUR")))] += 1


def make_of(tozar_teur: Any, table: dict | None = None) -> str | None:
    makes = makes_of_name(tozar_teur, table)
    return makes[0] if len(makes) == 1 else None


def code_agreement(agg: Recalls, registry_names: dict[int, Counter], table: dict | None = None) -> dict:
    """{codes, agreeing, rate, disagreeing: [...]} over the distinct TOZAR_CD values."""
    makes_by_code: dict[int, set] = {}
    for key in agg.rows:
        if key[2] is not None:
            makes_by_code.setdefault(key[2], set()).add(make_of(key[1], table))
    agreeing, disagreeing = 0, []
    for code in sorted(makes_by_code):
        notice = makes_by_code[code]
        names = registry_names.get(code) or Counter()
        registry = {m for name in names for m in makes_of_name(name, table)}
        ok = len(notice) == 1 and None not in notice and len(registry) == 1 and notice == registry
        if ok:
            agreeing += 1
        else:
            disagreeing.append({"tozar_cd": code, "notice_makes": sorted(m or "unresolved" for m in notice),
                                "registry_names": [n for n, _ in names.most_common(3)]})
    codes = len(makes_by_code)
    return {"codes": codes, "agreeing": agreeing, "rate": round(agreeing / codes, 4) if codes else 0.0,
            "disagreeing": disagreeing}


def catalogue_models(model_names: Counter, table: dict | None = None) -> dict[str, dict[str, set]]:
    """{canonical make: {norm_model(kinuy): {kinuy as stated}}} from Counter((tozeret_nm, kinuy_mishari))."""
    out: dict[str, dict[str, set]] = {}
    for (name, kinuy), _ in model_names.items():
        for make in makes_of_name(name, table):
            key = norm_model(kinuy)
            if key:
                out.setdefault(make, {}).setdefault(key, set()).add(norm_name(kinuy))
    return out


def load_map(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def map_pairs(data: dict) -> dict[tuple[str, str], list[str]]:
    """{(make, DEGEM normalized): [catalogue kinuy_mishari]} of the reviewed map."""
    out: dict[tuple[str, str], list[str]] = {}
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and entry.get("make") and entry.get("degem") and entry.get("kinuy_mishari"):
            out[(str(entry["make"]), norm_model(entry["degem"]))] = [str(k) for k in entry["kinuy_mishari"]]
    return out


def propose_map(agg: Recalls, previous: dict, models: dict[str, dict[str, set]], table: dict | None = None) -> dict:
    """The map with the build's proposals: reviewed entries kept as they are; a new (make, DEGEM) entry only from exact
    normalized equality with a catalogue kinuy_mishari of that make; the rest listed under `unresolved`."""
    entries = [e for e in previous.get("entries") or [] if isinstance(e, dict)]
    known = map_pairs(previous)
    counts: Counter = Counter()
    teur: dict[tuple, Counter] = {}
    for key, n in agg.rows.items():
        pair = (make_of(key[1], table), norm_model(key[3]))
        counts[pair] += n
        teur.setdefault(pair, Counter())[key[1] or ""] += n
    unresolved = []
    for (make, degem), n in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if make and degem and (make, degem) in known:
            continue
        exact = sorted((models.get(make) or {}).get(degem) or []) if make and degem else []
        if exact:
            entries.append({"make": make, "degem": degem, "kinuy_mishari": exact, "origin": "exact_normalized_equality"})
            known[(make, degem)] = exact
            continue
        reason = "make_unresolved" if not make else "degem_empty" if not degem else "no_exact_catalogue_model"
        unresolved.append({"make": make, "tozar_teur": teur[(make, degem)].most_common(1)[0][0] or None,
                           "degem": degem or None, "notice_rows": n, "reason": reason})
    entries.sort(key=lambda e: (str(e.get("make")), norm_model(e.get("degem"))))
    unresolved.sort(key=lambda u: (-u["notice_rows"], str(u["make"]), str(u["degem"])))
    return {"_about": "Reviewed mapping (canonical make, DEGEM normalized) -> catalogue kinuy_mishari values for the "
                      "recall notices (src/gov_data/recalls.py). The build only ADDS entries whose DEGEM equals a "
                      "catalogue kinuy_mishari of that make after normalization (origin exact_normalized_equality); "
                      "the pull request is the review. A reviewer may add entries by hand (origin reviewer) or delete "
                      "a wrong one. `unresolved` lists every (make, DEGEM) the build could not map, largest first: "
                      "no recall is served for a model until its entry is here.",
            "version": MAP_VERSION, "entries": entries, "unresolved": unresolved}


def table_rows(agg: Recalls, pairs: dict, code_key: bool, table: dict | None = None) -> list[tuple]:
    out = []
    for key, n in agg.rows.items():
        make = make_of(key[1], table)
        degem = norm_model(key[3])
        status = "resolved" if make and degem and (make, degem) in pairs else "unresolved"
        out.append((key[0], make, key[1], key[2], key[3], degem or None, key[4], key[5], year_of(key[4]),
                    year_of(key[5]), key[6], key[7], key[8], key[9], key[10], key[11], key[12], status, n))
    return out


def report(agg: Recalls, agreement: dict, code_key: bool, new_map: dict, min_rate: float) -> tuple[list[str], dict]:
    unresolved = new_map.get("unresolved") or []
    stats = {"rows": agg.count, "distinct_rows": len(agg.rows),
             "recall_ids": len({k[0] for k in agg.rows if k[0]}), "tozar_cd_agreement": {
                 k: agreement[k] for k in ("codes", "agreeing", "rate")}, "tozar_cd_used_as_key": code_key,
             "map_entries": len(new_map.get("entries") or []), "unresolved_models": len(unresolved)}
    lines = [f"- rows {agg.count} ({len(agg.rows)} distinct), recall ids {stats['recall_ids']}",
             f"- TOZAR_CD agreement with the registry tozeret_cd (same canonical make): {agreement['agreeing']} of "
             f"{agreement['codes']} codes = {agreement['rate'] * 100:.1f} % -> "
             + ("**used as a make key**" if code_key else f"not used as a key (needs >= {min_rate * 100:.0f} %)"),
             f"- model map: {stats['map_entries']} entries; unresolved (make, DEGEM): {len(unresolved)}"]
    if unresolved:
        lines += ["", "Unresolved models (top 50 by notice rows):", "",
                  "| make | TOZAR_TEUR | DEGEM | notice rows | reason |", "|---|---|---|---|---|"]
        for u in unresolved[:50]:
            lines.append(f"| {u['make'] or '—'} | {u['tozar_teur'] or '—'} | {u['degem'] or '—'} | "
                         f"{u['notice_rows']} | {u['reason']} |")
    return lines, stats

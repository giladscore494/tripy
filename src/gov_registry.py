"""Government vehicle registry layer (PR #44, P1): tyre sizes of the target's own registered vehicles. No search, no
network at runtime, no database.

data.gov.il publishes the registry of private and commercial vehicles at licence-plate level, with the government
codes of each vehicle (tozeret_cd, degem_cd, shnat_yitzur, ramat_gimur) and its front / rear tyre sizes (zmig_kidmi,
zmig_ahori). scripts/build_gov_registry_index.py aggregates it offline into data/gov_registry_index.json:

    key     tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur        (the Level 1.5 record's government codes; the trim
                                                                       normalized identically on both sides: upper
                                                                       case, "-" / "_" / "." as a space, collapsed
                                                                       whitespace; PR #45)
    value   {"front": {majority, share, n, top: [{size, n}]}, "rear": {...}}

At the start of a run (src/agent.run_vehicle) the target's entry becomes GOVERNMENT EVIDENCE (source_authority
government_registry, market IL, binding exact_market_trim through the government model code), fail-closed:

    N < MIN_VEHICLES (20)               nothing
    share >= MIN_SHARE (0.8)            tire_size_front / tire_size_rear = the majority size; rim_diameter_front_in /
                                        rim_diameter_rear_in = each axle's R number; rim_diameter_in = the R number only
                                        when front and rear agree on it (a staggered set has no single rim)
    share < MIN_SHARE                   alternative_tire_sizes = the two most common sizes, never a standard size
    an index not marked complete        nothing (the shipped file is empty until the workflow fills it)

Model-year fallback: a hierarchical fallback for a trim key without an entry (n < 20, or no registered vehicle under
that trim), fail-closed. The level used is stored on every emitted item (`registry_level`):

    A   tozeret_cd | degem_cd | year | trim        the rules above; binding exact_market_trim (basis gov_model_code)
    B   tozeret_cd | degem_cd | year, all trims    only when A has no entry. The pool (`model_years` of the index)
                                                   emits a standard size only at n >= MIN_VEHICLES, share >=
                                                   LEVEL_B_MIN_SHARE (0.95) AND every trim with n >= LEVEL_B_TRIM_MIN
                                                   (3) has that same majority; binding exact_technical_variant (basis
                                                   gov_registry_model_year), raised to exact_market_trim (basis
                                                   gov_registry_model_year_trim_confirmed) only when the target's own
                                                   trim is in the pool with n >= LEVEL_B_TRIM_MIN on that axle (the rim:
                                                   on both) and its majority is the pooled majority (target_trim_n /
                                                   target_trim_majority are recorded on the item). When the trims'
                                                   majorities differ, no
                                                   standard size: the trims with their majority sizes become
                                                   alternative_tire_sizes evidence. Never pooled across years or degem.

The evidence cites a document of the shared cache (kind `registry`) holding the aggregate statement, so Binding Replay
reads it like any other source; its binding is the registry rule (registry_binding), never the text binding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote


INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "gov_registry_index.json"
REGISTRY_VERSION = "gov-registry-v1"
SOURCE_AUTHORITY = "government_registry"
DATASET_URL = "https://data.gov.il/dataset/private-and-commercial-vehicles"
MIN_VEHICLES = 20
MIN_SHARE = 0.8
LEVEL_B_MIN_SHARE = 0.95       # the model-year pool (level B) needs a near-unanimous size
LEVEL_B_TRIM_MIN = 3           # trims with fewer vehicles on an axle do not vote on the level B agreement
LEVEL_A, LEVEL_B = "A", "B"
KEY_PARTS = ("tozeret_cd", "degem_cd", "shnat_yitzur", "ramat_gimur")
_INDEX: dict[str, tuple[float, dict]] = {}


# PR #45 (R7): the registry's tyre strings. One size, metric "W/A [Z]R RIM" with optional spaces, a load / speed index
# ("98Y", "101/99V", glued to the rim: "R1998Y"), reinforcement / run-flat / service markers; anything else is garbage.
# The registry's own spellings: "\" as "/" ("195\60 R15"), a slash before or after the R ("205/55/R16",
# "225/55R/17"), and a bare third number as the rim ("235/45/18", only for a rim of BARE_RIM).
REGISTRY_TIRE = re.compile(r"^P?(?P<width>\d{3})\s*/\s*(?P<aspect>\d{2})\s*"
                           r"(?:(?:/\s*)?(?:Z\s*R|R|Z)\s*F?\s*(?:/\s*)?|-|(?P<bare>/))\s*"
                           r"(?P<rim>\d{2})(?=$|[^\d]|\d{2,3}[A-Z])(?P<tail>.*)$")
TIRE_TAIL_TOKEN = re.compile(r"\d{2,3}(?:/\d{2,3})?[A-Z]{1,2}(?![A-Z\d])|[A-Z][A-Z+&]*|\d+|\S")
TIRE_TAIL_MARKS = set("*()[],.-:+&;")
TIRE_RANGES = {"width": (125, 395), "aspect": (20, 95), "rim": (10, 24)}
BARE_RIM = (12, 24)


def normalize_tire(raw: Any) -> str | None:
    """A registry tyre string as "W/A RDD" (the tire_size matcher's form), None when it is not ONE metric size:
    "235/50R19", "235/50 R19", "235/50ZR19", "235/50 ZR 19", "245/40R20XL", "245/45R19 98Y", "245/45 R19 98W XL",
    "225/45R17 RFT" / "ROF" / "RF", "195\\60 R15", "225/55R/17", "205/55/R16", "235/45/18", Hebrew words and extra
    whitespace around it -> "235/50 R19" ... Garbage (no size, two sizes, another number after the size, an implausible
    width / aspect / rim, a bare third number outside BARE_RIM) -> None."""
    text = re.sub(r"[^\x20-\x7e]+", " ", str(raw or "")).upper().replace("\\", "/")
    text = re.sub(r"\s+", " ", text).strip()
    m = REGISTRY_TIRE.match(text)
    if not m:
        return None
    width, aspect, rim = int(m.group("width")), int(m.group("aspect")), int(m.group("rim"))
    for value, (low, high) in zip((width, aspect, rim), TIRE_RANGES.values()):
        if not low <= value <= high:
            return None
    if m.group("bare") and not BARE_RIM[0] <= rim <= BARE_RIM[1]:
        return None
    # the rest: load / speed indexes, words (XL, RF / RFT / ROF, M+S, a pattern name) and marks; any other number or a
    # second size ("235/50R19 / 255/45R19") is garbage
    for token in TIRE_TAIL_TOKEN.findall(m.group("tail")):
        if not (re.fullmatch(r"\d{2,3}(?:/\d{2,3})?[A-Z]{1,2}", token) or re.fullmatch(r"[A-Z][A-Z+&]{0,11}", token)
                or token in TIRE_TAIL_MARKS):
            return None
    return f"{width}/{aspect:02d} R{rim}"


def normalize_trim(raw: Any) -> str:
    """The registry join key's trim (R7): upper case, "-" / "_" / "." as a space, whitespace collapsed, trimmed.
    "S-LINE" = "S LINE" = " s  line ". Applied identically by the index build and the runtime lookup; nothing fuzzier."""
    return " ".join(re.sub(r"[-_.]", " ", str(raw or "")).upper().split())


def registry_key(tozeret_cd: Any, degem_cd: Any, shnat_yitzur: Any, ramat_gimur: Any) -> str | None:
    try:
        parts = [int(str(tozeret_cd).strip()), int(str(degem_cd).strip()), int(str(shnat_yitzur).strip())]
    except (TypeError, ValueError):
        return None
    return "|".join([*(str(p) for p in parts), normalize_trim(ramat_gimur)])


def payload_key(payload: dict | None) -> str | None:
    """The target's registry key from its Level 1.5 record (identity.government_codes + year + trim)."""
    ident = (payload or {}).get("identity") or {}
    codes = ident.get("government_codes") if isinstance(ident.get("government_codes"), dict) else {}
    return registry_key(codes.get("tozeret_cd"), codes.get("degem_cd"), ident.get("year"), ident.get("trim"))


def index(path: Path | str | None = None) -> dict:
    """data/gov_registry_index.json (INDEX_PATH), loaded once per file version. Configuration is the data file, never
    the environment."""
    path = Path(path or INDEX_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _INDEX.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    _INDEX[str(path)] = (mtime, data)
    return data


_NORMALIZED: dict[int, tuple[dict, dict]] = {}


def normalize_key(key: str | None) -> str | None:
    """A registry key with its trim part normalized (normalize_trim); None when it is not a 4-part key."""
    parts = str(key or "").split("|")
    if len(parts) != 4:
        return None
    return "|".join([*(p.strip() for p in parts[:3]), normalize_trim(parts[3])])


def _normalized_entries(entries: dict) -> dict:
    """The entries by normalized key. An index built before PR #45 may hold two raw trims of one normalized key: their
    summaries cannot be merged, so such a key resolves to nothing (fail-closed; a rebuild merges the counts)."""
    cached = _NORMALIZED.get(id(entries))
    if cached is not None and cached[0] is entries:
        return cached[1]
    view, clash = {}, set()
    for raw, entry in entries.items():
        key = normalize_key(raw)
        if key is None:
            continue
        if key in view:
            clash.add(key)
        else:
            view[key] = entry
    for key in clash:
        view.pop(key, None)
    if len(_NORMALIZED) > 8:
        _NORMALIZED.clear()
    _NORMALIZED[id(entries)] = (entries, view)
    return view


def entry_for(key: str | None, data: dict | None = None) -> dict | None:
    data = index() if data is None else data
    if not key or data.get("complete") is not True:
        return None
    entries = data.get("entries") or {}
    norm = normalize_key(key)
    if norm is None:
        return None
    # Always use the normalized view. Direct lookup would let an already-normalized raw key bypass a collision with
    # another legacy raw spelling (for example both "S-LINE" and "S LINE"), violating the fail-closed rule above.
    return _normalized_entries(entries).get(norm)


def rim_of(size: str | None) -> int | None:
    m = re.search(r"R(\d{2})$", str(size or ""))
    return int(m.group(1)) if m else None


def facts(entry: dict | None) -> list[dict]:
    """[{field, value, statement}] the registry entry states, by the rules of the module doc."""
    out: list[dict] = []
    if not entry:
        return out
    standard: dict[str, str] = {}
    alternatives: list[str] = []
    for axle, field in (("front", "tire_size_front"), ("rear", "tire_size_rear")):
        stats = entry.get(axle) or {}
        n, share, majority = int(stats.get("n") or 0), float(stats.get("share") or 0), stats.get("majority")
        if n < MIN_VEHICLES or not majority:
            continue
        top = [t["size"] for t in stats.get("top") or [] if t.get("size")]
        label = "צמיג קדמי" if axle == "front" else "צמיג אחורי"
        if share >= MIN_SHARE:
            standard[axle] = majority
            out.append({"field": field, "value": majority,
                        "statement": f"{label} | {majority} ({round(share * 100)}% of {n} registered vehicles)"})
        else:
            alternatives += [s for s in top[:2] if s not in alternatives]
    # D2: each axle's rim from its own standard size (rim_diameter_front_in / _rear_in); rim_diameter_in only when
    # BOTH axles independently passed the registry confidence thresholds and agree on the same R diameter (a
    # staggered set is never one rim). One reliable axle is enough for that axle's facts, never for rim_diameter_in.
    out += _axle_rims(standard, "")
    if set(standard) == {"front", "rear"}:
        rims = {rim_of(standard["front"]), rim_of(standard["rear"])}
        if len(rims) == 1 and None not in rims:
            rim = rims.pop()
            size = standard["front"]
            out.append({"field": "rim_diameter_in", "value": rim,
                        "statement": f"קוטר חישוק | {rim} (R{rim} of {size})"})
    if alternatives:
        joined = ", ".join(alternatives[:2])
        out.append({"field": "alternative_tire_sizes", "value": joined,
                    "statement": f"מידות צמיגים חלופיות | {joined} (no size has {round(MIN_SHARE * 100)}% of the "
                                 "registered vehicles)"})
    return out


def _axle_rims(standard: dict[str, str], scope: str) -> list[dict]:
    """rim_diameter_front_in / rim_diameter_rear_in rows of the axles with a standard size."""
    out = []
    for axle, field, label in (("front", "rim_diameter_front_in", "קוטר חישוק קדמי"),
                               ("rear", "rim_diameter_rear_in", "קוטר חישוק אחורי")):
        rim = rim_of(standard.get(axle))
        if rim is not None:
            out.append({"field": field, "value": rim, "statement": f"{label} | {rim} (R{rim} of {standard[axle]}{scope})"})
    return out


def model_year_key(key: str | None) -> str | None:
    """tozeret_cd | degem_cd | year of a 4-part registry key (the level B pool; never wider)."""
    norm = normalize_key(key)
    return norm.rsplit("|", 1)[0] if norm else None


def model_year_for(key: str | None, data: dict | None = None) -> dict | None:
    """The index's model-year pool of a registry key (`model_years`), None for an index without it."""
    data = index() if data is None else data
    group = model_year_key(key)
    if not group or data.get("complete") is not True:
        return None
    pools = data.get("model_years")
    return pools.get(group) if isinstance(pools, dict) else None


def _trim_majorities(pool: dict, axle: str) -> list[tuple[str, str, int, int]]:
    """[(trim, majority, majority n, n)] of the pool's trims with at least LEVEL_B_TRIM_MIN vehicles on this axle."""
    out = []
    for trim, info in sorted((pool.get("trims") or {}).items()):
        row = (info or {}).get(axle)
        if isinstance(row, list) and len(row) == 3 and row[0] and int(row[2] or 0) >= LEVEL_B_TRIM_MIN:
            out.append((trim, str(row[0]), int(row[1]), int(row[2])))
    return out


def model_year_facts(pool: dict | None) -> tuple[list[dict], str | None]:
    """(rows, refusal) of a level B pool, by the rules of the module doc. The refusal (None when a standard size is
    emitted) is one of no_pool / pooled_n_below / trims_disagree / below_thresholds."""
    if not pool:
        return [], "no_pool"
    axles = (("front", "tire_size_front", "צמיג קדמי"), ("rear", "tire_size_rear", "צמיג אחורי"))
    if max(int((pool.get(axle) or {}).get("n") or 0) for axle, _, _ in axles) < MIN_VEHICLES:
        return [], "pooled_n_below"
    votes = {axle: _trim_majorities(pool, axle) for axle, _, _ in axles}
    split = [axle for axle, _, _ in axles if len({v[1] for v in votes[axle]}) > 1]
    if split:
        # the trims' majorities differ: never a standard size; the trims and their sizes as alternatives
        sizes: list[str] = []
        parts = []
        for axle in split:
            ranked = sorted(votes[axle], key=lambda v: (-v[3], v[0]))
            sizes += [v[1] for v in ranked if v[1] not in sizes]
            parts.append(f"{axle}: " + "; ".join(f"{t} {size} ({k} of {n})" for t, size, k, n in ranked))
        joined = ", ".join(sizes)
        return [{"field": "alternative_tire_sizes", "value": joined,
                 "statement": f"מידות צמיגים חלופיות | {joined} (the trims of this model year differ, by trim: "
                              + " | ".join(parts) + ")"}], "trims_disagree"
    out: list[dict] = []
    standard: dict[str, str] = {}
    for axle, field, label in axles:
        stats = pool.get(axle) or {}
        n, share, majority = int(stats.get("n") or 0), float(stats.get("share") or 0), stats.get("majority")
        if n and stats.get("majority_n") is not None:
            share = int(stats["majority_n"]) / n            # exact, not the rounded share (0.94996 is not 0.95)
        voted = {v[1] for v in votes[axle]}
        if n < MIN_VEHICLES or share < LEVEL_B_MIN_SHARE or not majority or (voted and voted != {majority}):
            continue
        standard[axle] = majority
        out.append({"field": field, "value": majority,
                    "statement": f"{label} | {majority} ({round(share * 100)}% of {n} registered vehicles of this "
                                 "model year, all trims agree)"})
    out += _axle_rims(standard, ", model year, all trims")
    if set(standard) == {"front", "rear"}:
        rims = {rim_of(standard["front"]), rim_of(standard["rear"])}
        if len(rims) == 1 and None not in rims:
            rim = rims.pop()
            out.append({"field": "rim_diameter_in", "value": rim,
                        "statement": f"קוטר חישוק | {rim} (R{rim} of {standard['front']}, model year, all trims)"})
    return out, None if out else "below_thresholds"


def lookup(key: str | None, data: dict | None = None) -> dict:
    """The registry's answer for a 4-part key: {level (A / B / None), doc_key, entry, rows, reason}. Level A is the
    trim's own entry (unchanged); only when it has none, level B, the key's model-year pool."""
    data = index() if data is None else data
    entry = entry_for(key, data)
    if entry is not None:
        rows = facts(entry)
        return {"level": LEVEL_A, "doc_key": normalize_key(key), "entry": entry, "rows": rows,
                "reason": None if rows else "below_thresholds"}
    pool = model_year_for(key, data)
    rows, refusal = model_year_facts(pool)
    if not rows:
        # an index built before the model-year pools has no level B at all: `no_entry` as before
        legacy = not isinstance(data.get("model_years"), dict)
        return {"level": None, "doc_key": None, "entry": None, "rows": [],
                "reason": "no_entry" if legacy else f"level_b_{refusal}"}
    return {"level": LEVEL_B, "doc_key": model_year_key(key), "entry": pool, "rows": rows,
            "reason": None if refusal is None else f"level_b_{refusal}"}


def document_url(key: str) -> str:
    parts = dict(zip(KEY_PARTS, key.split("|")))
    return DATASET_URL + "#" + "&".join(f"{k}={quote(v)}" for k, v in parts.items())


def _document(cache, key: str, entry: dict, rows: list[dict], data: dict, level: str = LEVEL_A) -> str:
    """The registry statement as a cached document (kind `registry`): what the evidence cites, what replay re-reads.
    Level B documents carry the 3-part model-year key."""
    scope = "קוד דגם ושנת ייצור, כל רמות הגימור" if level == LEVEL_B else "קוד דגם"
    text = "\n".join([f"מאגר כלי רכב פרטיים ומסחריים (data.gov.il) - צמיגים לפי {scope}",
                      f"key: {key}", f"level: {level}", *[r["statement"] for r in rows]])
    meta = {"status": 200, "final_url": document_url(key), "doc_type": "text", "content_type": "text/plain",
            "title": "Government vehicle registry: tyre sizes", "registry_key": key, "registry_level": level,
            "registry_version": REGISTRY_VERSION, "registry_entry": entry,
            "registry_generated_at": data.get("generated_at"), "registry_source": data.get("source")}
    return cache.put("registry", document_url(key), text.encode("utf-8"), meta, text)["document_id"]


FIELD_AXLES = {"tire_size_front": ("front",), "tire_size_rear": ("rear",), "rim_diameter_in": ("front", "rear"),
               "rim_diameter_front_in": ("front",), "rim_diameter_rear_in": ("rear",)}


def target_trim_check(pool: dict | None, trim_words, field: str | None) -> dict:
    """Does the target's own trim confirm a level B fact? {confirmed, target_trim_n, target_trim_majority}: the trim of
    the pool whose words are the target's (exactly one, else absent), with n >= LEVEL_B_TRIM_MIN on the fact's axle (the
    rim: both axles) and that axle's majority equal to the pooled majority. A one-axle fact records an int and a size;
    the rim and alternative_tire_sizes (never confirmed) record {front, rear}."""
    pool = pool or {}
    words = _words(normalize_trim(" ".join(trim_words or ())))       # the pool's trims are normalize_trim'd
    matches = [info for trim, info in (pool.get("trims") or {}).items() if words and _words(trim) == words]
    info = (matches[0] or {}) if len(matches) == 1 else {}
    axles = FIELD_AXLES.get(field or "")
    checked = {}
    for axle in axles or ("front", "rear"):
        row = info.get(axle) if isinstance(info.get(axle), list) and len(info.get(axle)) == 3 else [None, 0, 0]
        pooled = (pool.get(axle) or {}).get("majority")
        checked[axle] = (row[0], int(row[2] or 0), bool(row[0]) and int(row[2] or 0) >= LEVEL_B_TRIM_MIN
                         and row[0] == pooled)
    confirmed = bool(axles) and all(ok for _, _, ok in checked.values())
    if axles and len(axles) == 1:
        majority, n, _ = checked[axles[0]]
        return {"confirmed": confirmed, "target_trim_n": n, "target_trim_majority": majority}
    return {"confirmed": confirmed, "target_trim_n": {a: n for a, (_, n, _) in checked.items()},
            "target_trim_majority": {a: m for a, (m, _, _) in checked.items()}}


def registry_binding(meta: dict, identity, requirement: str | None, field: str | None = None) -> dict:
    """The binding of a registry fact: exact_market_trim (basis gov_model_code) when the document's key is the
    target's own government key; for a level B (model-year pool) document whose degem_cd and model year are the
    target's, exact_market_trim (basis gov_registry_model_year_trim_confirmed) when the target's own trim confirms the
    fact (target_trim_check), else exact_technical_variant (basis gov_registry_model_year); else `different` (another
    model code / year / trim). variant_match is `exact` only when the level reaches the requirement
    (src/document_binding.bind). Deterministic: a replay re-reads the pool stored on the document."""
    pooled = meta.get("registry_level") == LEVEL_B
    own = (str(identity.gov_model_code), str(identity.year), tuple(identity.trim_words)) \
        if identity.gov_model_code and identity.year else None
    key = str(meta.get("registry_key") or "")
    check = None
    if pooled:
        same = bool(key) and own is not None and _comparable_model_year(key) == own[:2]
        check = target_trim_check(meta.get("registry_entry"), own[2] if own else (), field) if same else None
        level, basis = ("unknown", None) if not same else \
            ("exact_market_trim", "gov_registry_model_year_trim_confirmed") if check["confirmed"] else \
            ("exact_technical_variant", "gov_registry_model_year")
    else:
        same = bool(key) and own is not None and _comparable(key) == own
        level, basis = ("exact_market_trim", "gov_model_code") if same else ("unknown", None)
    required = requirement or "exact_market_trim"
    from .document_binding import level_index

    match = "different" if not same else "exact" if level_index(level) >= level_index(required) else "unclear"
    dimensions = {"gov_model_code": {"status": "match" if same else "mismatch", "basis": "government_registry"}}
    if check is not None:
        dimensions["trim"] = {"status": "match" if check["confirmed"] else "absent", "basis": basis,
                              "target_trim_n": check["target_trim_n"],
                              "target_trim_majority": check["target_trim_majority"]}
    return {"binding_level": level, "variant_match": match,
            "binding_requirement": required, "binding_veto": [] if same else ["gov_model_code_mismatch"],
            "binding_dimensions": dimensions, "binding_basis": basis, "binding_version": REGISTRY_VERSION}


def _comparable_model_year(key: str) -> tuple:
    """(degem_cd, year) of a level B document's 3-part key."""
    parts = key.split("|")
    return (parts[1], parts[2]) if len(parts) == 3 else ()


def _comparable(key: str) -> tuple:
    """(degem_cd, year, trim words). The target identity carries no tozeret_cd: the document was written for the run's
    own full key (emit), and a replay compares its degem_cd, model year and trim with the target's."""
    parts = key.split("|")
    if len(parts) != 4:
        return ()
    return parts[1], parts[2], _words(parts[3])


def _words(trim: str) -> tuple:
    """A trim's words as the target identity holds them (src/document_binding._trim_words)."""
    return tuple(w for w in re.split(r"[^\wא-ת]+", str(trim or "").lower()) if w)


def emit(ctx, payload: dict | None, data: dict | None = None) -> dict:
    """Admit the target's registry facts into the run's evidence store (events `evidence`, phase government_registry).
    Returns {key, facts, stored, reason}. Never raises."""
    from .evidence_admission import ADMISSION_VERSION
    from .typed_values import typed_value

    out: dict[str, Any] = {"version": REGISTRY_VERSION, "key": payload_key(payload), "stored": 0}
    try:
        data = index() if data is None else data
        if data.get("complete") is not True:
            return {**out, "reason": "index_not_complete"}
        found = lookup(out["key"], data)
        out["level"] = found["level"]
        if found["level"] is None:
            return {**out, "reason": found["reason"]}
        rows = found["rows"]
        out["facts"] = [{k: r[k] for k in ("field", "value")} for r in rows]
        if found["reason"]:
            out["reason"] = found["reason"]
        if not rows:
            return out
        adm = ctx.admission
        doc = _document(ctx.cache, found["doc_key"], found["entry"], rows, data, found["level"])
        meta = ctx.cache.get(doc) or {}
        for row in rows:
            spec = adm.spec(row["field"])
            if spec.get("applicable") is False:
                continue
            binding = registry_binding(meta, adm.identity, spec.get("binding_requirement"), row["field"])
            trim_check = (binding.get("binding_dimensions") or {}).get("trim") or {}
            unit = spec.get("normalized_unit")
            record = {"field": row["field"], "value": row["value"], "unit": unit, "source_url": meta.get("final_url"),
                      "document_id": doc, "quote": row["statement"], "market": "IL", "market_basis": "government_registry",
                      "variant_match": binding["variant_match"], "admission_status": "accepted",
                      "admission_version": ADMISSION_VERSION,
                      "admission_checks": {"provenance": "government_registry_index", "entailment": "registry_aggregate"},
                      "entailment": "registry_aggregate", "typed_value": typed_value(
                          row["value"], unit=unit, value_type=spec.get("value_type"), matcher=spec.get("matcher")),
                      **{k: v for k, v in binding.items() if k != "variant_match" and v not in (None, [], {})},
                      "source_authority": SOURCE_AUTHORITY, "authority_basis": "gov_registry_index",
                      "registry_level": found["level"],
                      **({k: trim_check.get(k) for k in ("target_trim_n", "target_trim_majority")}
                         if found["level"] == LEVEL_B else {}),
                      "source_domain": "data.gov.il", "observed_at": meta.get("fetched_at"),
                      "source_date": data.get("generated_at"), "source_date_basis": "gov_registry_index"}
            item, reused, _ = ctx.evidence.add_or_reuse({k: v for k, v in record.items() if v not in (None, "", [])})
            ctx.note_document(doc)
            if not reused:
                out["stored"] += 1
                ctx.emit("evidence", evidence=item, phase="government_registry")
        ctx.counters["gov_registry_evidence"] += out["stored"]
    except Exception as exc:  # noqa: BLE001 - the registry layer never costs the run
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return out

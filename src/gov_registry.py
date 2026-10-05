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
    share >= MIN_SHARE (0.8)            tire_size_front / tire_size_rear = the majority size; rim_diameter_in = its R
                                        number (front and rear agree on it, else no rim)
    share < MIN_SHARE                   alternative_tire_sizes = the two most common sizes, never a standard size
    an index not marked complete        nothing (the shipped file is empty until the workflow fills it)

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
KEY_PARTS = ("tozeret_cd", "degem_cd", "shnat_yitzur", "ramat_gimur")
_INDEX: dict[str, tuple[float, dict]] = {}


# PR #45 (R7): the registry's tyre strings. One size, metric "W/A [Z]R RIM" with optional spaces, a load / speed index
# ("98Y", "101/99V", glued to the rim: "R1998Y"), reinforcement / run-flat / service markers; anything else is garbage.
REGISTRY_TIRE = re.compile(r"^P?(\d{3})\s*/\s*(\d{2})\s*(?:Z\s*R|R|-|Z)\s*F?\s*(\d{2})(?=$|[^\d]|\d{2,3}[A-Z])(.*)$")
TIRE_TAIL_TOKEN = re.compile(r"\d{2,3}(?:/\d{2,3})?[A-Z]{1,2}(?![A-Z\d])|[A-Z][A-Z+&]*|\d+|\S")
TIRE_TAIL_MARKS = set("*()[],.-:+&;")
TIRE_RANGES = {"width": (125, 395), "aspect": (20, 95), "rim": (10, 24)}


def normalize_tire(raw: Any) -> str | None:
    """A registry tyre string as "W/A RDD" (the tire_size matcher's form), None when it is not ONE metric size:
    "235/50R19", "235/50 R19", "235/50ZR19", "235/50 ZR 19", "245/40R20XL", "245/45R19 98Y", "245/45 R19 98W XL",
    "225/45R17 RFT" / "ROF" / "RF", Hebrew words and extra whitespace around it -> "235/50 R19" ... Garbage (no size,
    two sizes, another number after the size, an implausible width / aspect / rim) -> None."""
    text = re.sub(r"[^\x20-\x7e]+", " ", str(raw or "")).upper()
    text = re.sub(r"\s+", " ", text).strip()
    m = REGISTRY_TIRE.match(text)
    if not m:
        return None
    width, aspect, rim = int(m.group(1)), int(m.group(2)), int(m.group(3))
    for value, (low, high) in zip((width, aspect, rim), TIRE_RANGES.values()):
        if not low <= value <= high:
            return None
    # the rest: load / speed indexes, words (XL, RF / RFT / ROF, M+S, a pattern name) and marks; any other number or a
    # second size ("235/50R19 / 255/45R19") is garbage
    for token in TIRE_TAIL_TOKEN.findall(m.group(4)):
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


_NORMALIZED: dict[int, dict] = {}


def normalize_key(key: str | None) -> str | None:
    """A registry key with its trim part normalized (normalize_trim); None when it is not a 4-part key."""
    parts = str(key or "").split("|")
    if len(parts) != 4:
        return None
    return "|".join([*(p.strip() for p in parts[:3]), normalize_trim(parts[3])])


def _normalized_entries(entries: dict) -> dict:
    """The entries by normalized key. An index built before PR #45 may hold two raw trims of one normalized key: their
    summaries cannot be merged, so such a key resolves to nothing (fail-closed; a rebuild merges the counts)."""
    view = _NORMALIZED.get(id(entries))
    if view is None:
        view, clash = {}, set()
        for raw, entry in entries.items():
            key = normalize_key(raw)
            if key is None:
                continue
            if key in view and raw != key:
                clash.add(key)
            view[key] = entry
        for key in clash:
            view.pop(key, None)
        if len(_NORMALIZED) > 8:
            _NORMALIZED.clear()
        _NORMALIZED[id(entries)] = view
    return view


def entry_for(key: str | None, data: dict | None = None) -> dict | None:
    data = index() if data is None else data
    if not key or data.get("complete") is not True:
        return None
    entries = data.get("entries") or {}
    norm = normalize_key(key)
    if norm is None:
        return None
    return entries.get(norm) or _normalized_entries(entries).get(norm)


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
    # A rim diameter is only strong enough to emit when BOTH axles independently passed the
    # registry confidence thresholds and agree on the same R diameter. One reliable axle is
    # enough for that axle's tyre-size fact, but never enough to promote a trim-level rim fact.
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


def document_url(key: str) -> str:
    parts = dict(zip(KEY_PARTS, key.split("|")))
    return DATASET_URL + "#" + "&".join(f"{k}={quote(v)}" for k, v in parts.items())


def _document(cache, key: str, entry: dict, rows: list[dict], data: dict) -> str:
    """The registry statement as a cached document (kind `registry`): what the evidence cites, what replay re-reads."""
    text = "\n".join(["מאגר כלי רכב פרטיים ומסחריים (data.gov.il) - צמיגים לפי קוד דגם",
                      f"key: {key}", *[r["statement"] for r in rows]])
    meta = {"status": 200, "final_url": document_url(key), "doc_type": "text", "content_type": "text/plain",
            "title": "Government vehicle registry: tyre sizes", "registry_key": key,
            "registry_version": REGISTRY_VERSION, "registry_entry": entry,
            "registry_generated_at": data.get("generated_at"), "registry_source": data.get("source")}
    return cache.put("registry", document_url(key), text.encode("utf-8"), meta, text)["document_id"]


def registry_binding(meta: dict, identity, requirement: str | None) -> dict:
    """The binding of a registry fact: exact_market_trim (basis gov_model_code) when the document's key is the
    target's own government key, else `different` (another model code / year / trim). Deterministic."""
    own = (str(identity.gov_model_code), str(identity.year), tuple(identity.trim_words)) \
        if identity.gov_model_code and identity.year else None
    key = str(meta.get("registry_key") or "")
    same = bool(key) and own is not None and _comparable(key) == own
    level = "exact_market_trim" if same else "unknown"
    return {"binding_level": level, "variant_match": "exact" if same else "different",
            "binding_requirement": requirement or "exact_market_trim", "binding_veto": [] if same else
            ["gov_model_code_mismatch"], "binding_dimensions": {"gov_model_code": {"status": "match" if same else
                                                                                   "mismatch",
                                                                                   "basis": "government_registry"}},
            "binding_basis": "gov_model_code" if same else None, "binding_version": REGISTRY_VERSION}


def _comparable(key: str) -> tuple:
    """(degem_cd, year, trim words). The target identity carries no tozeret_cd: the document was written for the run's
    own full key (emit), and a replay compares its degem_cd, model year and trim with the target's."""
    parts = key.split("|")
    if len(parts) != 4:
        return ()
    return parts[1], parts[2], tuple(w for w in re.split(r"[^\wא-ת]+", parts[3].lower()) if w)


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
        entry = entry_for(out["key"], data)
        if entry is None:
            return {**out, "reason": "no_entry"}
        rows = facts(entry)
        out["facts"] = [{k: r[k] for k in ("field", "value")} for r in rows]
        if not rows:
            return {**out, "reason": "below_thresholds"}
        adm = ctx.admission
        doc = _document(ctx.cache, out["key"], entry, rows, data)
        meta = ctx.cache.get(doc) or {}
        for row in rows:
            spec = adm.spec(row["field"])
            if spec.get("applicable") is False:
                continue
            binding = registry_binding(meta, adm.identity, spec.get("binding_requirement"))
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

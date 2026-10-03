"""Server-side variant binding: does a document (and a fact inside it) describe the TARGET variant?

The model may say `variant_match="exact"`; it is kept only as `model_variant_claim`. The effective binding is
computed here, deterministically, from what the text itself names:

    target identity (Level 1.5): manufacturer · model family · model year · body · propulsion ·
                                 engine displacement · power · drivetrain · model code · trim · market
            ↓
    document profile: which values of each dimension the whole document mentions (computed once per
                      document and target)
            ↓
    fact context layers, most specific first: the clause around the value in the quote → the quote →
                      the document line holding the quote → the table column header of the value
                      (+ the model's own `variant` text, which can only VETO, never raise)
            ↓
    per dimension: match | mismatch | mixed | absent   (the first fact layer that names the dimension
                      decides; otherwise the document)
            ↓
    binding level   unknown < model_family < generation < body_powertrain < exact_technical_variant
                    < exact_market_trim
    veto            an explicit contradiction (another body, propulsion, displacement, drivetrain or
                    system power; a longer model family such as "corolla cross" for a "corolla")
            ↓
    variant_match   different (veto) | unbound (not even the model family) | exact (level >= the field's
                    binding_requirement) | unclear

Two rules make `exact_market_trim` reachable when the government trim does not identify itself in prose
(binding-v2), both deterministic and fail-closed, each recorded as `binding_basis`:

    qualified_trim_phrase   a government trim made only of generic words (MAX, PRO, BASE EDITION) matches only as
                            the family followed by those words ("g6 max", "g6-max", "xpeng g6 max"), never alone
                            ("max power" is not the MAX trim)
    single_trim_catalog     a fact already bound at exact_technical_variant (no veto, no mixed dimension) from a
                            target-market official source (or a target-market document whose identity zone names the
                            family) that names no other trim, when the government catalog index
                            (data/catalog_trim_index.json) lists exactly ONE trim for that technical variant

A multi-variant document (1.8 AND 2.0 Hybrid) is never exact by itself: the fact context must name the
target's technical variant. A powertrain mismatch vetoes exact binding even when manufacturer, model,
body and year all match. DocumentBinding is not Evidence and never a truth score: it only says which
variant a source talks about. Vocabulary lives in data/identity_vocabulary.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .candidate_harvest import compile_terms, normalize_text, parse_number

BINDING_VERSION = "binding-v2"
VOCAB_PATH = Path(__file__).resolve().parent.parent / "data" / "identity_vocabulary.json"
TRIM_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog_trim_index.json"
OFFICIAL_AUTHORITIES = ("government", "official_manufacturer", "official_importer", "official_media")
LEVELS = ("unknown", "model_family", "generation", "body_powertrain", "exact_technical_variant", "exact_market_trim")
DEFAULT_REQUIREMENT = "exact_technical_variant"
# Level a veto on this dimension caps the binding at.
VETO_CAP = {"model": "unknown", "body": "generation", "propulsion": "generation", "displacement": "body_powertrain",
            "drivetrain": "body_powertrain", "power": "body_powertrain", "trim": "exact_technical_variant"}
NON_TARGET_MATCHES = ("different", "unbound")
_VOCAB: dict[str, tuple[float, dict]] = {}
_TRIM_INDEX: dict[str, tuple[float, dict]] = {}
_COMPILED: dict[tuple, Any] = {}
# "<X> version / trim", "version / trim / גרסת / רמת גימור <X>": the text names a trim
NAMED_TRIM = re.compile(r"(?:\b(?:version|trim)\s*:?\s*|(?:גרסת|גרסה|רמת גימור)\s*:?\s*(?:ה-?)?)([a-zא-ת][\w-]{2,})"
                        r"|\b([a-z][\w-]{2,})\s+(?:version|trim)\b")
GENERIC_NAMED = {"the", "this", "that", "each", "every", "all", "base", "entry", "top", "new", "old", "other", "same",
                 "hybrid", "היברידית", "היברידי", "חשמלית", "הבסיס", "הבסיסית", "העליונה", "החדשה", "level", "line"}
# between the model family and a generic trim word: nothing, a space / hyphen, or a Hebrew "version" connector
TRIM_CONNECTOR = r"(?:\s*(?:בגרסת|גרסת|ברמת גימור|רמת גימור|ב-|ה-)\s*|[\s\-]*)"
# a generic trim word's written forms ("business edi" is the catalog's "business edition")
TRIM_WORD_FORMS = {"edi": ("edi", "edition"), "edition": ("edition", "edi")}
# A generic MAX immediately modifying a metric label is not a trim, even when the model family precedes it:
# "G6 MAX power 486 hp" means maximum power. Fail closed rather than promote the fact to exact_market_trim.
MAX_METRIC_FOLLOWERS = ("power", "output", "speed", "range", "torque", "charge", "charging", "current", "voltage",
                        "capacity", "הספק", "מהירות", "טווח", "מומנט", "טעינה", "זרם", "מתח", "קיבולת")
NEGATED_TRIM = re.compile(r"(?:\bnot\b|\bno\b|\bexcept\b|\bexcluding\b|\bwithout\b|(?<![א-ת])לא(?![א-ת])|ללא|למעט|"
                          r"חוץ מ|פרט ל)[^.;|\n]{0,20}$")
YEAR = re.compile(r"(?<![\d.,/-])(20[0-3]\d)(?![\d])(?!\s*[-–]\s*\d)(?!\s*(?:rpm|סל|mm|מ\"מ|ממ|cm|ס\"מ|kg|ק\"ג|nm|נ\"מ|cc|סמ|km|ק\"מ|"
                  r"l\b|ליטר|kw|hp|כ\"ס|ש\"ח|₪|€|\$|lb|wh))")


def vocabulary(path: Path | str | None = None) -> dict:
    path = Path(path or os.environ.get("IDENTITY_VOCABULARY_PATH") or VOCAB_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _VOCAB.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text("utf-8"))
    _VOCAB[str(path)] = (mtime, data)
    return data


def level_index(level: str | None) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def _terms(key: tuple, terms: Iterable[str]):
    compiled = _COMPILED.get(key)
    if compiled is None:
        compiled = _COMPILED[key] = compile_terms(terms)
    return compiled


def _compact(text: str) -> str:
    return re.sub(r"[\s\-]+", "", str(text or "").lower())


# --- target identity -----------------------------------------------------------------------------------

@dataclass
class TargetIdentity:
    manufacturer: str | None = None
    family: str | None = None
    year: int | None = None
    body: str | None = None
    propulsion: str | None = None
    displacement_l: float | None = None
    displacement_cc: float | None = None
    power_hp: float | None = None
    drivetrain: str | None = None
    model_code_tokens: list[str] = field(default_factory=list)
    trim_tokens: list[str] = field(default_factory=list)
    trim_words: list[str] = field(default_factory=list)      # every word of the government trim (generic ones too)
    # a trim made only of generic words matches only qualified by the family ("g6 max"); see _qualified_phrases
    qualified_trim_phrases: list[str] = field(default_factory=list)
    target_market: str = "IL"
    # identity-only parts (never used for document binding): the FULL normalized government model code, whose letter
    # suffixes separate variants the engine figures cannot, and the transmission (Level 1.5 `automatic` flag)
    model_code: str | None = None
    transmission: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    def scope_key(self, level: str = "exact_technical_variant") -> str:
        """Stable identity key of the target at a binding level (used for scoped reuse)."""
        parts = {"model_family": ("manufacturer", "family"),
                 "generation": ("manufacturer", "family", "year"),
                 "body_powertrain": ("manufacturer", "family", "year", "body", "propulsion"),
                 "exact_technical_variant": ("manufacturer", "family", "year", "body", "propulsion", "displacement_l",
                                             "power_hp", "drivetrain"),
                 "exact_market_trim": ("manufacturer", "family", "year", "body", "propulsion", "displacement_l",
                                       "power_hp", "drivetrain", "trim_tokens", "target_market")}[level]
        data = self.as_dict()
        # Generic-only trims (MAX / PRO / BASE ...) intentionally have no trim_tokens. Once binding-v2 can reach
        # exact_market_trim for them, an empty trim component would make different trims share the same fact-cache /
        # research-memory scope. Preserve the old key for normal trims, but fall back to the full trim words here.
        if level == "exact_market_trim" and not data.get("trim_tokens"):
            data["trim_tokens"] = data.get("trim_words")
        return level + ":" + json.dumps([data.get(k) for k in parts], ensure_ascii=False, sort_keys=True)


def _family_of(commercial: str | None, vocab: dict) -> str | None:
    if not commercial:
        return None
    name = _compact(commercial)
    families = vocab.get("model_families") or {}
    best = None
    for family in families:
        if name.startswith(_compact(family)) and (best is None or len(_compact(family)) > len(_compact(best))):
            best = family
    if best:
        return best
    stop = {w.lower() for w in vocab.get("model_stop_words") or []}
    words = [w for w in re.split(r"\s+", str(commercial).lower()) if w and w not in stop]
    return words[0] if words else None


def _code_tokens(model_code: str | None) -> list[str]:
    out: list[str] = []
    for token in re.split(r"[^a-z0-9]+", str(model_code or "").lower()):
        if len(token) >= 4 and re.search(r"[a-z]", token) and re.search(r"\d", token):
            out.append(token)
            if re.search(r"\d[a-z]$", token):
                out.append(token[:-1])
    return list(dict.fromkeys(out))


def _trim_tokens(trim: str | None, vocab: dict) -> list[str]:
    generic = {w.lower() for w in vocab.get("generic_trim_words") or []}
    propulsion = {t.lower() for terms in (vocab.get("propulsion_terms") or {}).values() for t in terms}
    return [t for t in re.split(r"[^\wא-ת]+", str(trim or "").lower())
            if len(t) >= 3 and not t.isdigit() and t not in generic and t not in propulsion]


def _trim_words(trim: str | None) -> list[str]:
    return [t for t in re.split(r"[^\wא-ת]+", str(trim or "").lower()) if t]


def _qualified_phrases(family: str | None, trim_words: list[str], trim_tokens: list[str], vocab: dict) -> list[str]:
    """A government trim no word of which identifies it (every word generic: MAX, PRO, BASE EDITION) is matched only
    as the family followed by its words ("g6 max"; the manufacturer may precede it). A multi-word trim of generic
    words only also as its whole phrase with its written forms ("pro max", "base edition"). Never one word alone."""
    if trim_tokens or not trim_words or not family:
        return []
    aliases = (vocab.get("model_families") or {}).get(family) or [family]
    phrases = [f"{normalize_text(alias)} {' '.join(trim_words)}" for alias in aliases]
    if _bare_phrase_ok(trim_words, vocab):
        phrases.append(" ".join(trim_words))
    return list(dict.fromkeys(phrases))


def _bare_phrase_ok(trim_words: list[str], vocab: dict) -> bool:
    generic = {w.lower() for w in vocab.get("generic_trim_words") or []}
    return len(trim_words) > 1 and all(w in generic for w in trim_words)


def _qualified_pattern(identity: "TargetIdentity", vocab: dict):
    """The regex of the target's qualified trim phrases (family aliases + connector + trim words), or None."""
    if not identity.qualified_trim_phrases:
        return None
    key = ("qualified_trim", identity.family, tuple(identity.trim_words))
    compiled = _COMPILED.get(key)
    if compiled is None:
        def word(w: str) -> str:
            return "(?:" + "|".join(re.escape(f) for f in TRIM_WORD_FORMS.get(w, (w,))) + ")"
        words = r"[\s\-]+".join(word(w) for w in identity.trim_words)
        aliases = (vocab.get("model_families") or {}).get(identity.family or "") or [identity.family or ""]
        family = "|".join(r"[\s\-]*".join(re.escape(part) for part in normalize_text(a).split())
                          for a in sorted(aliases, key=len, reverse=True) if a)
        metric_guard = ""
        if identity.trim_words == ["max"]:
            followers = "|".join(re.escape(w) for w in MAX_METRIC_FOLLOWERS)
            metric_guard = rf"(?![\s:;,.\-–—]*(?:{followers})\b)"
        options = [rf"(?:{family}){TRIM_CONNECTOR}{words}{metric_guard}"]
        if _bare_phrase_ok(identity.trim_words, vocab):
            options.append(words)
        compiled = _COMPILED[key] = re.compile(r"(?<![\wא-ת])(?:" + "|".join(options) + r")(?![\wא-ת])")
    return compiled


def _drivetrain(value: Any) -> str | None:
    text = str(value or "").lower()
    if text in ("awd", "4x4", "four_wheel_drive", "4wd"):
        return "awd"
    if text in ("two_wheel_drive", "4x2", "fwd", "rwd", "2wd"):
        return "two_wheel_drive"
    return None


def target_identity(payload: dict | None, vehicle: dict | None = None, target_market: str = "IL") -> TargetIdentity:
    """The target's identity from the Level 1.5 record (vehicle metadata fills gaps). Fixed context."""
    vocab = vocabulary()
    payload, vehicle = payload or {}, vehicle or {}
    ident, engine = payload.get("identity") or {}, payload.get("engine_drivetrain") or {}
    structure = payload.get("structure") or {}
    cc = engine.get("engine_cc")
    try:
        cc_value = float(cc) if cc and float(cc) > 0 else None
    except (TypeError, ValueError):
        cc_value = None
    displacement = round(cc_value / 1000, 1) if cc_value else None
    try:
        power = float(engine.get("power_hp")) if engine.get("power_hp") else None
    except (TypeError, ValueError):
        power = None
    year = ident.get("year") or vehicle.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    family = _family_of(ident.get("commercial_name") or vehicle.get("model"), vocab)
    trim = ident.get("trim") or vehicle.get("trim")
    trim_tokens, trim_words = _trim_tokens(trim, vocab), _trim_words(trim)
    return TargetIdentity(
        manufacturer=ident.get("manufacturer") or vehicle.get("manufacturer"),
        family=family,
        year=year,
        body=structure.get("body_normalized") or vehicle.get("body"),
        propulsion=engine.get("propulsion_normalized") or vehicle.get("propulsion"),
        displacement_l=displacement,
        displacement_cc=cc_value,
        power_hp=power,
        drivetrain=_drivetrain(engine.get("drivetrain_normalized") or vehicle.get("drivetrain")),
        model_code_tokens=_code_tokens(ident.get("model_code") or vehicle.get("model_code")),
        trim_tokens=trim_tokens,
        trim_words=trim_words,
        qualified_trim_phrases=_qualified_phrases(family, trim_words, trim_tokens, vocab),
        target_market=target_market or "IL",
        model_code=" ".join(t for t in re.split(r"[^0-9a-z]+", str(ident.get("model_code") or vehicle.get("model_code")
                                                                 or "").lower()) if t) or None,
        transmission={1: "automatic", 0: "manual", "1": "automatic", "0": "manual", True: "automatic",
                      False: "manual"}.get(engine.get("automatic")))


# --- mentions ------------------------------------------------------------------------------------------

def _alternation(terms: Iterable[str]) -> str:
    return "|".join(re.escape(normalize_text(t)) for t in sorted(set(terms), key=lambda t: -len(t)) if t)


def _displacements(text: str, vocab: dict) -> set[float]:
    suffixes = _alternation(vocab.get("displacement_suffixes") or [])
    engine = _alternation(vocab.get("engine_words") or [])
    not_engine = r"(?!\s*(?:kwh?|קוט|קילוואט|times|x\b|פעמים|מיליון|million))"
    cc = _alternation(vocab.get("cc_units") or [])
    found: set[float] = set()
    if suffixes:
        # "4.5 l/100km", "4.5 ליטר ל-100 ק"מ" are consumption figures, not displacements
        for m in re.finditer(rf"(?<![\d.,])([1-9]\.\d)\s*-?\s*(?:{suffixes})(?![\w/])"
                             rf"(?!\s*(?:/|ל-?\s*100|למאה|per\s*100|per\s*hundred|לכל\s*100))", text):
            found.add(float(m.group(1)))
    if engine:
        for m in re.finditer(rf"(?:{engine})\s*:?\s*([1-9]\.\d)(?![\d]){not_engine}", text):
            found.add(float(m.group(1)))
    if cc:
        for m in re.finditer(rf"(?<![\d.,])(\d,\d{{3}}|\d{{3,4}})\s*(?:{cc})(?![\w])", text):
            value = parse_number(m.group(1))
            if value and 600 <= value <= 8500:
                found.add(round(value / 1000, 1))
    return {v for v in found if 0.6 <= v <= 8.5}


def _powers(text: str, vocab: dict) -> set[float]:
    """Power figures in hp (PS and, next to a power word, kW converted). Charging kW is never power."""
    units = _alternation(vocab.get("power_units") or [])
    found: set[float] = set()
    if units:
        for m in re.finditer(rf"(?<![\d.,])(\d{{2,4}})\s*(?:{units})(?![a-z])", text):
            value = float(m.group(1))
            unit = text[m.end(1):m.end()].strip()
            found.add(round(value * 0.986, 1) if unit.startswith("ps") else value)
    words = _alternation(vocab.get("power_words") or [])
    blockers = _terms(("charging_words",), vocab.get("charging_words") or [])
    if words:
        for m in re.finditer(rf"(?:{words})[^\d;|\n]{{0,25}}?(?<![\d.,])(\d{{2,4}})\s*(?:kw|קילוואט|קוט\"ס)(?![a-z])",
                             text):
            if blockers and blockers.search(m.group(0)):
                continue
            found.add(round(float(m.group(1)) * 1.341, 1))
    return {v for v in found if 40 <= v <= 2000}


def _keys_found(text: str, group: str, vocab: dict, mask_order: list[str] | None = None) -> set[str]:
    """Which keys of a term group (body / propulsion / drivetrain) the text names. With mask_order, the
    terms of earlier keys are removed before later keys are searched ('plug-in hybrid' is not 'hybrid')."""
    entries = vocab.get(group) or {}
    found: set[str] = set()
    work = text
    for key in (mask_order or list(entries)):
        pattern = _terms((group, key), entries.get(key) or [])
        if pattern and pattern.search(work):
            found.add(key)
            if mask_order:
                work = pattern.sub(" ", work)
    return found


def _family_status(text: str, identity: TargetIdentity, vocab: dict) -> set[str]:
    families = vocab.get("model_families") or {}
    if not identity.family:
        return set()
    own = families.get(identity.family) or [identity.family]
    longer = [f for f in families if f != identity.family
              and _compact(f).startswith(_compact(identity.family))]
    found: set[str] = set()
    work = text
    for other in longer:
        pattern = _terms(("family", other), families[other])
        if pattern and pattern.search(work):
            found.add("other_family")
            work = pattern.sub(" ", work)
    pattern = _terms(("family", identity.family), own)
    if pattern and pattern.search(work):
        found.add("target")
    return found


def other_trims_named(text: str, identity: TargetIdentity) -> list[str]:
    """Trim names the text gives ("the Premium version", "גרסת Business") that are not the target's own words."""
    named = [w for groups in NAMED_TRIM.findall(normalize_text(text or "")) for w in groups if w]
    return sorted({w for w in named if w not in identity.trim_words and w not in GENERIC_NAMED})


def mentions(text: str, identity: TargetIdentity) -> dict[str, Any]:
    """Every identity value a text names (normalized, case-insensitive)."""
    vocab = vocabulary()
    norm = normalize_text(text or "")
    manufacturer_terms = (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])
    man = _terms(("manufacturer", identity.manufacturer), manufacturer_terms) if manufacturer_terms else None
    codes = [c for c in identity.model_code_tokens if re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", norm)]
    phrase = " ".join(identity.trim_tokens)
    # a generic government trim (MAX) only as a qualified phrase ("g6 max"), never the word alone ("max power")
    trims = _terms(("trim", phrase), [phrase]) if phrase else _qualified_pattern(identity, vocab)
    trim = ""
    named = [w for w in NAMED_TRIM.findall(norm) for w in w if w]
    if identity.trim_words and any(w not in identity.trim_words and w not in GENERIC_NAMED for w in named):
        trim = "negated"        # "the Premium version", "גרסת ה-Premium": a fact about ANOTHER named trim
    for m in trims.finditer(norm) if trims else ():
        # "not available on Business", "לא בגרסת Business": the trim is named to EXCLUDE it
        trim = "negated" if NEGATED_TRIM.search(norm[max(0, m.start() - 25):m.start()]) else "match"
        if trim == "match":
            break
    return {
        "manufacturer": bool(man and man.search(norm)),
        "model": _family_status(norm, identity, vocab),
        "year": {int(y) for y in YEAR.findall(norm)},
        "body": _keys_found(norm, "body_terms", vocab),
        "propulsion": _keys_found(norm, "propulsion_terms", vocab,
                                  ["plug_in", "battery_electric", "hybrid", "conventional"])
        | {f"weak:{k}" for k in _keys_found(norm, "propulsion_weak_terms", vocab)},
        "displacement": _displacements(norm, vocab),
        "power": _powers(norm, vocab),
        "drivetrain": _keys_found(norm, "drivetrain_terms", vocab),
        "model_code": bool(codes),
        "trim": trim,
    }


def _close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= max(0.051, rel * abs(b))


def dimension_status(dim: str, found: Any, identity: TargetIdentity) -> str:
    """match | mismatch | mixed | absent for one dimension of one text."""
    if dim == "model":
        if not found:
            return "absent"
        if found == {"target"}:
            return "match"
        return "mixed" if "target" in found else "mismatch"
    if dim == "trim":
        return {"match": "match", "negated": "mismatch"}.get(found or "", "absent")
    if dim in ("model_code", "manufacturer"):
        return "match" if found else "absent"
    if not found:
        return "absent"
    if dim == "year":
        target = identity.year
        if target is None:
            return "absent"
        return ("match" if found == {target} else "mixed") if target in found else "mismatch"
    if dim == "displacement":
        # marketed litres vs exact cc: 1950 cc is a "2.0", 1798 cc a "1.8"
        target = identity.displacement_cc / 1000 if identity.displacement_cc else identity.displacement_l
        if target is None:
            return "absent"
        hits = {v for v in found if abs(v - target) <= 0.06}
        if not hits:
            return "mismatch"
        return "match" if hits == found else "mixed"
    elif dim == "power":
        target, rel = identity.power_hp, 0.03
    elif dim == "propulsion":
        target = identity.propulsion
        if target is None:
            return "absent"
        conflicts = set((vocabulary().get("propulsion_conflicts") or {}).get(target) or [])
        strong = {k for k in found if not k.startswith("weak:")}
        bad = strong & conflicts
        if target in strong:
            return "mixed" if bad else "match"
        if bad:
            return "mismatch"
        return "match" if f"weak:{target}" in found and not strong else "absent"
    else:   # body / drivetrain
        target = identity.body if dim == "body" else identity.drivetrain
        if target is None:
            return "absent"
        if target in found:
            return "match" if found == {target} else "mixed"
        return "mismatch"
    if target is None:
        return "absent"
    hits = {v for v in found if _close(v, target, rel)}
    if not hits:
        return "mismatch"
    return "match" if hits == found else "mixed"


DIMENSIONS = ("model", "year", "body", "propulsion", "displacement", "power", "drivetrain", "model_code", "trim",
              "manufacturer")


def statuses(text: str, identity: TargetIdentity) -> dict[str, str]:
    found = mentions(text, identity)
    return {dim: dimension_status(dim, found[dim], identity) for dim in DIMENSIONS}


# How a dimension the identity zone does not name is read from the rest of the document. Full text includes
# navigation menus, related-model teasers and comparisons, so it can CONFIRM a value but never veto one: another
# body or propulsion there is ignored, another displacement / power / drivetrain only leaves the dimension unresolved.
FULL_TEXT_MISMATCH = {"body": "absent", "propulsion": "absent", "model": "absent", "year": "absent",
                      "displacement": "mixed", "power": "mixed", "drivetrain": "mixed"}


def identity_zone(*, title: str | None, url: str | None, headings: list[str] | None = None,
                  text: str | None = None, identity: "TargetIdentity | None" = None, lines: int = 3) -> str:
    """The part of a document that says WHAT it is about: title, URL words and main headings (HTML; `headings` is
    a list, possibly empty) or the first lines (text / PDF; `headings` is None). Split into list segments ("|", "•")
    and, when any segment names the target model family, only those segments: a navigation menu ("Yaris Hybrid |
    Corolla | RAV4 Plug-in Hybrid | Hilux") never speaks for the page."""
    url_words = re.sub(r"[/_\-.?=&]+", " ", str(url or ""))
    head = [h for h in headings or [] if h and h.strip()]
    if headings is None:
        head = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:lines]
    segments = [seg.strip() for part in [title or "", url_words, *head] for seg in re.split(r"\s[|•·]\s|\|", part)
                if seg.strip()]
    if identity is not None:
        named = [seg for seg in segments if "target" in _family_status(normalize_text(seg), identity, vocabulary())]
        if named:
            segments = named
    return "\n".join(segments)


def zone_names_target(zone: str, identity: TargetIdentity) -> bool:
    return "target" in _family_status(normalize_text(zone), identity, vocabulary())


def _other_family_pattern(identity: TargetIdentity):
    families = vocabulary().get("model_families") or {}
    terms = [t for name, aliases in families.items() if name != identity.family for t in aliases]
    return _terms(("other_families", identity.family), terms) if terms else None


def about_target(text: str, identity: TargetIdentity) -> str:
    """The lines of a text that do not name ANOTHER model family without naming the target (menus, teasers and
    related-model boxes say nothing about this vehicle)."""
    other = _other_family_pattern(identity)
    if other is None:
        return text
    kept = []
    for line in (text or "").splitlines():
        norm = normalize_text(line)
        if other.search(norm) and "target" not in _family_status(norm, identity, vocabulary()):
            continue
        kept.append(line)
    return "\n".join(kept)


def document_profile(*, text: str, title: str | None, url: str | None, identity: TargetIdentity,
                     headings: list[str] | None = None) -> dict:
    """Document-level dimension statuses. The identity zone (title, URL, headings) decides first; the full text
    only confirms (see FULL_TEXT_MISMATCH). The trim counts only when the identity zone names it."""
    zone = identity_zone(title=title, url=url, headings=headings, text=text, identity=identity)
    named = zone_names_target(zone, identity)
    zone_found = mentions(zone, identity)
    full_found = mentions(f"{zone}\n{about_target(text or '', identity)}", identity)
    combined: dict[str, str] = {}
    zone_statuses = {dim: dimension_status(dim, zone_found[dim], identity) for dim in DIMENSIONS}
    full_statuses = {dim: dimension_status(dim, full_found[dim], identity) for dim in DIMENSIONS}
    for dim in DIMENSIONS:
        if dim == "trim":
            combined[dim] = zone_statuses[dim] if named else "absent"
        elif zone_statuses[dim] != "absent" and (named or zone_statuses[dim] != "mismatch"):
            # a zone that never names the target family (a price list headed by another model) cannot veto
            combined[dim] = zone_statuses[dim]
        elif full_statuses[dim] == "mismatch":
            combined[dim] = FULL_TEXT_MISMATCH.get(dim, "absent")
        else:
            combined[dim] = full_statuses[dim]
    return {"statuses": combined, "zone_statuses": zone_statuses, "full_statuses": full_statuses,
            "trim_named_in_document": bool(full_found["trim"]),
            # another named trim anywhere in the document (single_trim_catalog never applies to such a document)
            "other_trims_named": other_trims_named(f"{zone}\n{about_target(text or '', identity)}", identity),
            "mentions": {k: sorted(v) if isinstance(v, set) else v for k, v in full_found.items()}}


# --- government catalog trim index (single_trim_catalog) -------------------------------------------------

def power_bucket(power_hp: float | None) -> int | None:
    """Power rounded to the nearest 5 hp (the catalog index key part)."""
    return None if power_hp is None else int(float(power_hp) / 5 + 0.5) * 5


def catalog_key(*, manufacturer: Any, family: Any, year: Any, body: Any, propulsion: Any, drivetrain: Any,
                power: int | None, displacement_l: float | None) -> str:
    """The catalog index key of a technical variant (scripts/build_trim_index.py builds keys with this function)."""
    parts = [manufacturer, family, year, body, propulsion, drivetrain, power,
             None if displacement_l is None else f"{float(displacement_l):.1f}"]
    return "|".join("" if v is None else str(v) for v in parts)


def normalize_catalog_trim(trim: Any) -> str:
    return " ".join(str(trim or "").upper().split())


def trim_index(path: Path | str | None = None) -> dict:
    """data/catalog_trim_index.json (CATALOG_TRIM_INDEX_PATH overrides), loaded once per file version."""
    path = Path(path or os.environ.get("CATALOG_TRIM_INDEX_PATH") or TRIM_INDEX_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _TRIM_INDEX.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    _TRIM_INDEX[str(path)] = (mtime, data)
    return data


def single_catalog_trim(identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """{key, trim, records} when the government catalog lists exactly ONE trim -- the target's own -- for the target's
    technical variant in its model year; None otherwise (fail-closed: an unknown identity part, a key missing from the
    index, an entry marked incomplete, more than one trim, or another trim in a neighbouring power bucket the binding
    cannot tell apart (within the 3 % power tolerance))."""
    index = trim_index() if index is None else index
    entries = index.get("entries") or {}
    needed = (identity.manufacturer, identity.family, identity.year, identity.body, identity.propulsion,
              identity.drivetrain, identity.power_hp)
    if not entries or any(v in (None, "") for v in needed) or not identity.trim_words:
        return None
    if identity.displacement_l is None and identity.propulsion != "battery_electric":
        return None
    default_complete = index.get("complete", False) is True
    parts = dict(manufacturer=identity.manufacturer, family=identity.family, year=identity.year, body=identity.body,
                 propulsion=identity.propulsion, drivetrain=identity.drivetrain,
                 displacement_l=identity.displacement_l)
    own = power_bucket(identity.power_hp)
    key = catalog_key(**parts, power=own)
    if key not in entries:
        return None
    low, high = power_bucket(identity.power_hp * 0.97), power_bucket(identity.power_hp * 1.03)
    trims: set[str] = set()
    records: list[str] = []
    for bucket in range(low, high + 5, 5):
        entry = entries.get(catalog_key(**parts, power=bucket))
        if entry is None:
            continue
        if entry.get("complete", default_complete) is not True:
            return None
        trims |= {normalize_catalog_trim(t) for t in entry.get("trims") or [""]}
        records += [str(r) for r in entry.get("records") or []]
    if len(trims) != 1:
        return None
    (trim,) = trims
    if _trim_words(trim) != identity.trim_words:
        return None
    return {"key": key, "trim": trim, "records": records[:20]}


# --- binding --------------------------------------------------------------------------------------------

def _veto_dims(identity: TargetIdentity) -> tuple[str, ...]:
    dims = ["model", "body", "propulsion", "displacement", "drivetrain"]
    # Government power is the system power for conventional cars and BEVs; for hybrids it may be engine-only.
    if identity.propulsion in ("battery_electric", "conventional"):
        dims.append("power")
    return tuple(dims)


def _layer_statuses(name: str, text: str, identity: TargetIdentity, trim_named_in_document: bool) -> dict[str, str]:
    st = statuses(text, identity)
    st["year"] = "absent"        # a year inside a fact (a price-list date, "since 2019") is not a model year
    if name == "column_header":
        found = mentions(text, identity)
        technical = found["displacement"] or found["power"] or found["propulsion"]
        # a header with words beyond technical terms and the target trim's own words names ANOTHER trim
        # ("Premium", "1.8 Hybrid Premium", "Business Plus"); a technical header (1.8 Hybrid 140) names none
        if _header_extra_words(text, identity):
            st["trim"] = "mismatch"
        elif st["trim"] == "absent" and trim_named_in_document and not technical \
                and not _header_names_own_trim(text, identity):
            st["trim"] = "mismatch"
    return st


def _header_names_own_trim(header: str, identity: TargetIdentity) -> bool:
    """A header made of the target's own generic trim words ("MAX" for the G6 MAX): not another trim (and, alone,
    not a trim match either: a generic word is never the trim by itself)."""
    if not identity.qualified_trim_phrases:
        return False
    words = [w for w in re.split(r"[^\wא-ת]+", normalize_text(header)) if w and not re.search(r"\d", w)]
    return bool(words) and set(identity.trim_words) <= set(words)


def _header_extra_words(header: str, identity: TargetIdentity) -> list[str]:
    vocab = vocabulary()
    known: set[str] = set(identity.trim_words) | {w.lower() for w in vocab.get("header_noise_words") or []}
    own_names = [*(vocab.get("model_families") or {}).get(identity.family or "", []),
                 *(vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])]
    for terms in [own_names] + [t for group in ("propulsion_terms", "propulsion_weak_terms", "body_terms",
                                                "drivetrain_terms") for t in (vocab.get(group) or {}).values()]:
        for term in terms:
            known |= set(re.split(r"[^\wא-ת]+", normalize_text(term)))
    for key in ("displacement_suffixes", "power_units", "cc_units", "engine_words", "generic_trim_words"):
        for term in vocab.get(key) or []:
            known |= set(re.split(r"[^\wא-ת]+", normalize_text(term)))
    words = [w for w in re.split(r"[^\wא-ת]+", normalize_text(header)) if w and not re.search(r"\d", w)]
    return [w for w in words if w not in known and len(w) > 1]


def bind(identity: TargetIdentity, doc_statuses: dict[str, str], layers: list[tuple[str, str]] | None = None,
         veto_layers: list[tuple[str, str]] | None = None, *, market: str | None = None,
         requirement: str | None = None, model_declared_different: bool = False,
         trim_named_in_document: bool = False, source_authority: str | None = None,
         document_names_family: bool = False, other_trims_named: list[str] | None = None) -> dict:
    """The effective binding of a fact (or, with no layers, of the whole document). `source_authority`,
    `document_names_family` and `other_trims_named` (the document profile's) feed the single_trim_catalog rule only;
    `other_trims_named=None` (unknown) never lets it apply."""
    effective: dict[str, dict] = {}
    layer_statuses = [(name, _layer_statuses(name, text, identity, trim_named_in_document))
                      for name, text in layers or [] if text]
    for dim in DIMENSIONS:
        chosen = None
        for name, st in layer_statuses:
            if st[dim] != "absent":
                chosen = {"status": st[dim], "basis": name}
                break
        effective[dim] = chosen or {"status": doc_statuses.get(dim, "absent"), "basis": "document"}
    vetoes: list[str] = []
    veto_dims = _veto_dims(identity)
    required = requirement if requirement in LEVELS else DEFAULT_REQUIREMENT
    if required == "exact_market_trim" and effective["trim"]["status"] == "mismatch":
        vetoes.append(f"trim_mismatch@{effective['trim']['basis']}")
    for dim in veto_dims:
        if effective[dim]["status"] == "mismatch":
            vetoes.append(f"{dim}_mismatch@{effective[dim]['basis']}")
    for name, text in veto_layers or []:
        if not text:
            continue
        st = statuses(text, identity)
        for dim in veto_dims:
            if st[dim] == "mismatch" and f"{dim}_mismatch@{name}" not in vetoes:
                vetoes.append(f"{dim}_mismatch@{name}")
    s = {dim: effective[dim]["status"] for dim in DIMENSIONS}
    level, basis = "unknown", None
    if s["model"] in ("match", "mixed"):
        level = "model_family"
        if s["year"] != "mismatch":
            level = "generation"
            propulsion_ok = s["propulsion"] == "match" or (s["propulsion"] == "absent"
                                                          and identity.propulsion in (None, "conventional"))
            if s["body"] in ("match", "absent") and propulsion_ok and s["model"] == "match":
                level = "body_powertrain"
                technical = (s["displacement"] == "match" or s["model_code"] == "match"
                             or (identity.displacement_l is None and s["power"] == "match"))
                unresolved = (s["displacement"] == "mixed" or s["drivetrain"] == "mixed"
                              or ("power" in veto_dims and s["power"] == "mixed"))
                if technical and not unresolved:
                    level = "exact_technical_variant"
                    if s["trim"] == "match" and market and market == identity.target_market:
                        level = "exact_market_trim"
                        # a generic government trim (MAX) can only match as its qualified phrase ("g6 max")
                        basis = "qualified_trim_phrase" if identity.qualified_trim_phrases else None
    catalog = None
    if (required == "exact_market_trim" and level == "exact_technical_variant" and not vetoes
            and not model_declared_different and "mixed" not in s.values() and s["trim"] == "absent"
            and market and market == identity.target_market
            and (source_authority in OFFICIAL_AUTHORITIES or document_names_family)
            and other_trims_named is not None and not other_trims_named):
        catalog = single_catalog_trim(identity)
        if catalog is not None:
            level, basis = "exact_market_trim", "single_trim_catalog"
    for veto in vetoes:
        cap = VETO_CAP[veto.split("_mismatch")[0]]
        if level_index(level) > level_index(cap):
            level = cap
    if vetoes or model_declared_different:
        variant_match = "different"
    elif level == "unknown":
        variant_match = "unbound"
    elif level_index(level) >= level_index(required):
        variant_match = "exact"
    else:
        variant_match = "unclear"
    if model_declared_different and "model_declared_different" not in vetoes:
        vetoes.append("model_declared_different")
    dimensions = {d: effective[d] for d in DIMENSIONS if effective[d]["status"] != "absent"}
    if basis == "qualified_trim_phrase" and "trim" in dimensions:
        dimensions["trim"] = {**dimensions["trim"], "rule": "qualified_trim_phrase"}
    if catalog is not None:
        dimensions["trim"] = {"status": "match", "basis": "single_trim_catalog", "catalog_key": catalog["key"],
                              "catalog_trim": catalog["trim"], "catalog_records": catalog["records"]}
    out = {"binding_level": level, "variant_match": variant_match, "binding_requirement": required,
           "binding_veto": vetoes, "binding_dimensions": dimensions, "binding_version": BINDING_VERSION}
    if basis and level == "exact_market_trim":
        out["binding_basis"] = basis
    return out


# --- binding gap diagnosis (observational) ----------------------------------------------------------------

def binding_gaps(item: dict, requirement: str | None = None) -> list[str]:
    """Why an evidence item's binding stopped below its requirement, from its own binding_veto / binding_dimensions:
    `veto:<dim>`, or the dimension(s) that kept it from the next level (`trim_absent`, `power_absent`, `power_mixed`,
    `model_mixed`, ...), or `market` (bound at the trim but outside the target market). Never changes anything."""
    vetoes = item.get("binding_veto") or []
    if vetoes:
        return sorted({"veto:" + str(v).split("_mismatch")[0].split("@")[0].replace("model_declared_different",
                                                                                    "model_declared")
                       for v in vetoes})
    level = item.get("binding_level")
    required = requirement if requirement in LEVELS else item.get("binding_requirement") or DEFAULT_REQUIREMENT
    if level not in LEVELS:
        return []
    dims = {d: str((item.get("binding_dimensions") or {}).get(d, {}).get("status") or "absent") for d in DIMENSIONS}
    if level_index(level) >= level_index(required):
        return [] if str(item.get("variant_match") or "") == "exact" else ["market"]
    gaps: list[str] = []
    if level == "unknown":
        gaps.append(f"model_{dims['model']}")
    elif level == "model_family":
        gaps.append(f"year_{dims['year']}")
    elif level == "generation":
        gaps += [f"{d}_{dims[d]}" for d, ok in (("model", ("match",)), ("body", ("match", "absent")),
                                                ("propulsion", ("match",))) if dims[d] not in ok]
    elif level == "body_powertrain":
        mixed = [f"{d}_mixed" for d in ("displacement", "drivetrain", "power") if dims[d] == "mixed"]
        if mixed:
            gaps += mixed
        elif dims["power"] == "match":
            gaps.append(f"displacement_{dims['displacement']}")
        else:
            gaps += [f"{d}_{dims[d]}" for d in ("displacement", "power") if dims[d] != "match"]
    elif level == "exact_technical_variant":
        gaps.append("trim_absent" if dims["trim"] == "absent" else "market" if dims["trim"] == "match"
                    else f"trim_{dims['trim']}")
    return sorted(set(gaps)) or ["unresolved"]


def is_trim_gap(gap: str) -> bool:
    return gap.startswith("trim_") or gap == "veto:trim"

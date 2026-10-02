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

BINDING_VERSION = "binding-v1"
VOCAB_PATH = Path(__file__).resolve().parent.parent / "data" / "identity_vocabulary.json"
LEVELS = ("unknown", "model_family", "generation", "body_powertrain", "exact_technical_variant", "exact_market_trim")
DEFAULT_REQUIREMENT = "exact_technical_variant"
# Level a veto on this dimension caps the binding at.
VETO_CAP = {"model": "unknown", "body": "generation", "propulsion": "generation", "displacement": "body_powertrain",
            "drivetrain": "body_powertrain", "power": "body_powertrain"}
NON_TARGET_MATCHES = ("different", "unbound")
_VOCAB: dict[str, tuple[float, dict]] = {}
_COMPILED: dict[tuple, Any] = {}
YEAR = re.compile(r"(?<![\d.,/])(20[0-3]\d)(?![\d])")


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
    power_hp: float | None = None
    drivetrain: str | None = None
    model_code_tokens: list[str] = field(default_factory=list)
    trim_tokens: list[str] = field(default_factory=list)
    target_market: str = "IL"

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
        displacement = round(float(cc) / 1000, 1) if cc and float(cc) > 0 else None
    except (TypeError, ValueError):
        displacement = None
    try:
        power = float(engine.get("power_hp")) if engine.get("power_hp") else None
    except (TypeError, ValueError):
        power = None
    year = ident.get("year") or vehicle.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return TargetIdentity(
        manufacturer=ident.get("manufacturer") or vehicle.get("manufacturer"),
        family=_family_of(ident.get("commercial_name") or vehicle.get("model"), vocab),
        year=year,
        body=structure.get("body_normalized") or vehicle.get("body"),
        propulsion=engine.get("propulsion_normalized") or vehicle.get("propulsion"),
        displacement_l=displacement,
        power_hp=power,
        drivetrain=_drivetrain(engine.get("drivetrain_normalized") or vehicle.get("drivetrain")),
        model_code_tokens=_code_tokens(ident.get("model_code") or vehicle.get("model_code")),
        trim_tokens=_trim_tokens(ident.get("trim") or vehicle.get("trim"), vocab),
        target_market=target_market or "IL")


# --- mentions ------------------------------------------------------------------------------------------

def _alternation(terms: Iterable[str]) -> str:
    return "|".join(re.escape(normalize_text(t)) for t in sorted(set(terms), key=lambda t: -len(t)) if t)


def _displacements(text: str, vocab: dict) -> set[float]:
    suffixes = _alternation(vocab.get("displacement_suffixes") or [])
    engine = _alternation(vocab.get("engine_words") or [])
    cc = _alternation(vocab.get("cc_units") or [])
    found: set[float] = set()
    if suffixes:
        # "4.5 l/100km", "4.5 ליטר ל-100 ק"מ" are consumption figures, not displacements
        for m in re.finditer(rf"(?<![\d.,])([1-9]\.\d)\s*-?\s*(?:{suffixes})(?![\w/])"
                             rf"(?!\s*(?:/|ל-?\s*100|per\s*100|לכל\s*100))", text):
            found.add(float(m.group(1)))
    if engine:
        for m in re.finditer(rf"(?:{engine})\s*:?\s*([1-9]\.\d)(?![\d])", text):
            found.add(float(m.group(1)))
    if cc:
        for m in re.finditer(rf"(?<![\d.,])(\d,\d{{3}}|\d{{3,4}})\s*(?:{cc})(?![\w])", text):
            value = parse_number(m.group(1))
            if value and 600 <= value <= 8500:
                found.add(round(value / 1000, 1))
    return {v for v in found if 0.6 <= v <= 8.5}


def _powers(text: str, vocab: dict) -> set[float]:
    units = _alternation(vocab.get("power_units") or [])
    found: set[float] = set()
    if units:
        for m in re.finditer(rf"(?<![\d.,])(\d{{2,4}})\s*(?:{units})(?![a-z])", text):
            value = float(m.group(1))
            unit = text[m.end(1):m.end()].strip()
            found.add(round(value * 0.986, 1) if unit.startswith("ps") else value)
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


def mentions(text: str, identity: TargetIdentity) -> dict[str, Any]:
    """Every identity value a text names (normalized, case-insensitive)."""
    vocab = vocabulary()
    norm = normalize_text(text or "")
    manufacturer_terms = (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])
    man = _terms(("manufacturer", identity.manufacturer), manufacturer_terms) if manufacturer_terms else None
    codes = [c for c in identity.model_code_tokens if re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", norm)]
    trims = _terms(("trim", tuple(identity.trim_tokens)), identity.trim_tokens) if identity.trim_tokens else None
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
        "trim": bool(trims and trims.search(norm)),
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
    if dim in ("model_code", "trim", "manufacturer"):
        return "match" if found else "absent"
    if not found:
        return "absent"
    if dim == "year":
        target = identity.year
        if target is None:
            return "absent"
        return ("match" if found == {target} else "mixed") if target in found else "mismatch"
    if dim == "displacement":
        target, rel = identity.displacement_l, 0.0
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


def document_profile(*, text: str, title: str | None, url: str | None, identity: TargetIdentity) -> dict:
    """Document-level dimension statuses (title + URL words + full text). Computed once per document."""
    url_words = re.sub(r"[/_\-.?=&]+", " ", str(url or ""))
    blob = f"{title or ''}\n{url_words}\n{text or ''}"
    found = mentions(blob, identity)
    return {"statuses": {dim: dimension_status(dim, found[dim], identity) for dim in DIMENSIONS},
            "mentions": {k: sorted(v) if isinstance(v, set) else v for k, v in found.items()}}


# --- binding --------------------------------------------------------------------------------------------

def _veto_dims(identity: TargetIdentity) -> tuple[str, ...]:
    dims = ["model", "body", "propulsion", "displacement", "drivetrain"]
    # Government power is the system power for conventional cars and BEVs; for hybrids it may be engine-only.
    if identity.propulsion in ("battery_electric", "conventional"):
        dims.append("power")
    return tuple(dims)


def bind(identity: TargetIdentity, doc_statuses: dict[str, str], layers: list[tuple[str, str]] | None = None,
         veto_layers: list[tuple[str, str]] | None = None, *, market: str | None = None,
         requirement: str | None = None, model_declared_different: bool = False) -> dict:
    """The effective binding of a fact (or, with no layers, of the whole document)."""
    effective: dict[str, dict] = {}
    layer_statuses = [(name, statuses(text, identity)) for name, text in layers or [] if text]
    for dim in DIMENSIONS:
        chosen = None
        for name, st in layer_statuses:
            if st[dim] != "absent":
                chosen = {"status": st[dim], "basis": name}
                break
        effective[dim] = chosen or {"status": doc_statuses.get(dim, "absent"), "basis": "document"}
    vetoes: list[str] = []
    veto_dims = _veto_dims(identity)
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
    level = "unknown"
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
    for veto in vetoes:
        cap = VETO_CAP[veto.split("_mismatch")[0]]
        if level_index(level) > level_index(cap):
            level = cap
    required = requirement if requirement in LEVELS else DEFAULT_REQUIREMENT
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
    return {"binding_level": level, "variant_match": variant_match, "binding_requirement": required,
            "binding_veto": vetoes, "binding_dimensions": {d: effective[d] for d in DIMENSIONS
                                                            if effective[d]["status"] != "absent"},
            "binding_version": BINDING_VERSION}

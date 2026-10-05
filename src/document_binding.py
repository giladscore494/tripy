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

binding-v4 (PR #40) adds deterministic, catalog-driven rules, each recorded on the fact (`binding_rules`, and
`binding_basis` = the rule that raised the level last) and fail-closed:

    single_body_catalog        body `mixed` (the target body plus other models' bodies on the page) counts as a match
                               when every catalog entry of the target's manufacturer / family / model year is complete
                               and has the target body (a body mismatch stays a veto)
    single_propulsion_catalog  propulsion `absent` counts as a match when every catalog entry of the target's
                               manufacturer / family / model year is complete and has the target propulsion, and no
                               fact layer or the document names another propulsion
    discriminating power       the power tolerance is min(3 %, half the relative gap to the nearest other catalog
                               power of the same manufacturer / family / year / body / propulsion / drivetrain); it
                               only tightens and is recorded as binding_dimensions.power.tolerance
    brand_policy               a brand-wide warranty statement (fields with `brand_policy_scope`) of an official
                               source of the target manufacturer in the target market that names no model binds at
                               body_powertrain (eligibility is decided by the caller, evidence_admission.fact_binding)
    dvm_region / dvm_shared    the fact's region in the Document Variant Map (src/variant_map.py) is assigned to the
                               target's technical variant, or the fact is a shared row of a document whose variant
                               inventory contains the target; a region whose identity contradicts the target vetoes
                               (`<dim>_mismatch@dvm_region`). Only absent / mixed dimensions not decided by the value's
                               own clause are completed; a veto of the fact itself always wins

binding-v5 (PR #43), each recorded as a `binding_flags` entry (and as the fact's binding gap while it holds it below its
requirement), deterministic and fail-closed; nothing here loosens a veto or a binding-v4 rule:

    system_power_unmapped        hybrid / plug-in target (government power = engine power): the value's clause, quote,
                                 column or section states another power. Not a veto, but displacement alone never makes
                                 the technical variant
    several_powertrain_versions  hybrid / plug-in target, the document states >= 2 distinct powers or >= 2 version
                                 designations (identity vocabulary `variant_designations`): the same; only the model code
                                 or a target DVM region that names the version (designation / model code / catalog trim)
                                 proves it (binding_dimensions.version)
    relative_variant_reference   the value's own clause refers to another version ("הבכיר יותר", "the more powerful";
                                 vocabulary `relative_variant_terms`): at most body_powertrain
    stale_publication            the document was published >= 2 years before the target model year (metadata / URL date;
                                 a non-official source also by its article date line) and never states the target model
                                 year: at most generation, unless a DVM region assigned to the target by power + designation
                                 holds the value. An official page without a date is never stale

binding-v6 (PR #44, Israeli source playbook), deterministic and fail-closed:

    il_version_page              a single-version Israeli publisher page whose identity (title / H1 / URL slug / spec
                                 rows: src/il_version_pages.py) matches the target's year, displacement, drivetrain,
                                 propulsion, body and power (or, for a power the catalog cannot map, the catalog's only
                                 entry for manufacturer / family / year / propulsion / drivetrain) binds every value at
                                 exact_technical_variant (binding_basis il_version_page); its market trim only when the
                                 page names the target trim. A single-version page's identity replaces the full-text
                                 statuses of the dimensions it names
    system_power_unmapped (P4.2) also a hybrid single-version page that states another power, unless the catalog has
                                 exactly one entry for manufacturer / family / year / propulsion / drivetrain
    engine_invariant (P4.1)      a field with variant_invariance "engine" (gearbox, body dimensions) of a hybrid target
                                 is not held by system_power_unmapped when the document's displacement matches the
                                 target's (several_powertrain_versions, relative references and stale publications
                                 still hold it)

binding-v7 (PR #45), deterministic and fail-closed; nothing here loosens an earlier rule:

    body sub-variants (R3)       a body sub-variant word (Sportback, Avant, Coupé / קופה, Cabrio, Gran Coupé, Touring,
                                 Shooting Brake, ...; vocabulary `body_subvariants`) in the identity zone that the
                                 target's commercial name / government model name does not contain is a body mismatch
                                 (veto at the body cap); an Israeli single-version page also needs the target's own
                                 sub-variant in its title / H1 / URL. The catalog family key does not separate them
    single_value_multi_version   (R4) a value stated once in a document whose inventory names >= 2 technical versions
                                 (powers, designations or displacements) binds at most body_powertrain unless its own
                                 context layers or a target DVM region name the version, or it is repeated identically
                                 for every version; a fact whose DVM region is `unresolved / inventory_without_target`
                                 never binds the technical variant of such a document (evidence_admission asserts it)
    page_inconsistent (R5)       a single-version Israeli page whose stated power contradicts its own displacement
                                 against the catalog (1984 cc next to 341 hp while every 2.0 l entry of the family-year
                                 is <= 265 hp): its values bind at most body_powertrain
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .candidate_harvest import compile_terms, normalize_text, parse_number

BINDING_VERSION = "binding-v7"
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
# These words are harmless descriptors for legacy trim matching, but for a generic-only target (MAX/PRO/BASE) an
# explicit "Base version" / "Top version" is a real competing tier and must not inherit the target trim from the page.
GENERIC_TIER_WORDS = {"base", "entry", "top", "הבסיס", "הבסיסית", "העליונה"}
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
# --- model-year statements (binding-v3) ---
# A 4-digit year counts for the year dimension only as a MODEL-YEAR STATEMENT (see year_context): next to the target's
# manufacturer / family name, after an explicit label, inside an identity-zone segment that names the family, or in an
# explicit family-year URL slug. Copyright, publication / update dates, price-list validity, URLs and free-standing
# years in prose are recorded as ignored contexts (telemetry) and never feed the dimension.
ANY_YEAR = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")
YEAR_LABEL = re.compile(r"(?:שנת[\s\-]*(?:ה)?דגם|שנתון|model[\s\-]*year)\s*[:\-–]?\s*$|(?<![\w])my-?$")
SHORT_MY = re.compile(r"(?<![\w])my-?(\d{2})(?![\w])")
COPYRIGHT = re.compile(r"©|\(c\)|copyright|כל הזכויות|all rights reserved")
PUBLISHED = re.compile(r"פורסם|עודכן|תאריך|published|updated|posted")
PRICE_VALIDITY = re.compile(r"בתוקף|valid\s+(?:from|until|as\s+of)|החל\s*מ")
MONTHS = re.compile(r"(?<![\wא-ת])(?:[בלמ]-?)?(?:ינואר|פברואר|מרץ|מרס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|"
                    r"דצמבר|january|february|march|april|may|june|july|august|september|october|november|december|"
                    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)(?![\wא-ת])")
DATE_BEFORE = re.compile(r"(?<![\w.])\d{1,2}\s*[./-]\s*(?:\d{1,2}\s*[./-]\s*)?$")
DATE_AFTER = re.compile(r"\s*[./-]\s*\d{1,2}(?!\d)")
URL_TOKEN = re.compile(r"://|www\.|@|[a-z]/|/[a-z]|\.(?:com|co|org|net|il|de|uk)\b")
YEAR_UNIT = re.compile(r"\s*(?:rpm|סל|mm|מ\"מ|ממ|cm|ס\"מ|kg|ק\"ג|nm|נ\"מ|cc|סמ|km|ק\"מ|l\b|ליטר|kw|hp|כ\"ס|ש\"ח|₪|€|\$|lb|wh)")
SEGMENT_BREAK = re.compile(r"[\n;|•·!?]|\.(?:\s|$)")


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
    # the government model code (Level 1.5 identity.government_codes.degem_cd): the number Israeli importers publish
    # as "קוד דגם" in their mandatory safety-equipment table (verified, PR #42); identity-only, never a scope key
    gov_model_code: int | None = None
    # PR #45 (R3): the body sub-variant words (identity vocabulary `body_subvariants`) the target's commercial name /
    # government model name contains ("sportback" for an A1 SPORTBACK); identity-only, never a scope key
    body_subvariants: list[str] = field(default_factory=list)

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
                      False: "manual"}.get(engine.get("automatic")),
        gov_model_code=_gov_code((ident.get("government_codes") or {}).get("degem_cd")
                                 if isinstance(ident.get("government_codes"), dict) else None),
        body_subvariants=sorted(body_subvariants(" ".join(str(v or "") for v in (
            ident.get("commercial_name") or vehicle.get("model"), ident.get("model_code") or vehicle.get("model_code"))),
            vocab)))


def _gov_code(value: Any) -> int | None:
    try:
        code = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return code if code > 0 else None


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
    for _, start, _ in designation_spans(text, vocab):
        # "2.0 40TFSI", "3.0 55 TFSI e": the litres right before a version designation
        m = re.search(r"(?<![\d.,])([1-9]\.\d)\s*$", text[max(0, start - 6):start])
        if m:
            found.add(float(m.group(1)))
    if cc:
        for m in re.finditer(rf"(?<![\d.,])(\d,\d{{3}}|\d{{3,4}})\s*(?:{cc})(?![\w])", text):
            value = parse_number(m.group(1))
            if value and 600 <= value <= 8500:
                found.add(round(value / 1000, 1))
    return {v for v in found if 0.6 <= v <= 8.5}


_DESIGNATIONS: dict[str, Any] = {}


def _designation_pattern(vocab: dict):
    patterns = (vocab.get("variant_designations") or {}).get("patterns") or []
    key = json.dumps(patterns)
    if key not in _DESIGNATIONS:
        _DESIGNATIONS.clear()
        _DESIGNATIONS[key] = re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None
    return _DESIGNATIONS[key]


def designation_spans(text: str, vocab: dict | None = None) -> list[tuple[str, int, int]]:
    """(compact designation, start, end) of every engine / powertrain version designation of a normalized text
    ("55 TFSI e" -> "55tfsie", "40TFSI" -> "40tfsi"; identity vocabulary `variant_designations`, PR #43)."""
    pattern = _designation_pattern(vocabulary() if vocab is None else vocab)
    if pattern is None:
        return []
    return [(_canonical_designation(m.group(0)), m.start(), m.end()) for m in pattern.finditer(text)]


def _canonical_designation(raw: str) -> str:
    """The compact form of a designation; the reversed order of Israeli used-car titles ("TFSI 45") is "45tfsi"."""
    compact = re.sub(r"[\s\-]+", "", raw)
    m = re.fullmatch(r"([a-z]+)(\d{2})", compact)
    return f"{m.group(2)}{m.group(1)}" if m else compact


def designations(text: str, vocab: dict | None = None) -> set[str]:
    return {d for d, _, _ in designation_spans(text, vocab)}


def body_subvariants(text: str, vocab: dict | None = None) -> set[str]:
    """The body sub-variant keys a text names (identity vocabulary `body_subvariants`, PR #45 R3): "Q3 Sportback" ->
    {"sportback"}; a longer key's terms are masked before shorter ones are searched ("gran coupe" is not "coupe")."""
    vocab = vocabulary() if vocab is None else vocab
    cfg = vocab.get("body_subvariants") or {}
    terms = cfg.get("terms") or {}
    order = [k for k in cfg.get("order") or [] if k in terms] + [k for k in terms if k not in (cfg.get("order") or [])]
    work, found = normalize_text(text or ""), set()
    for key in order:
        pattern = _terms(("body_subvariant", key), terms.get(key) or [])
        if pattern and pattern.search(work):
            found.add(key)
            work = _masked(pattern, work)
    return found


def subvariant_status(text: str, identity: "TargetIdentity", *, reverse: bool = False) -> dict | None:
    """R3: {status: mismatch | match, subvariant, direction} of an identity text (a title / H1 / URL zone that names
    the target family), None when it decides nothing. `mismatch` (direction page_only): the text names a sub-variant
    the target's names do not contain; with `reverse` also (target_only) when the target's names contain one the text
    does not name. A sub-variant whose implied body (`implies_body`) is the target's own body never mismatches."""
    vocab = vocabulary()
    implied = (vocab.get("body_subvariants") or {}).get("implies_body") or {}
    named = body_subvariants(text, vocab)
    own = set(identity.body_subvariants or [])
    extra = sorted(k for k in named - own if not (implied.get(k) and implied.get(k) == identity.body))
    if extra:
        return {"status": "mismatch", "subvariant": extra[0], "direction": "page_only"}
    missing = sorted(k for k in own - named if not (implied.get(k) and implied.get(k) == identity.body))
    if reverse and missing:
        return {"status": "mismatch", "subvariant": missing[0], "direction": "target_only"}
    if named & own:
        return {"status": "match", "subvariant": sorted(named & own)[0], "direction": "both"}
    return None


def distinct_powers(powers: Iterable[float], rel: float = 0.03) -> list[float]:
    """Powers grouped within `rel` of each other ("250 kW (340 hp)" states one power, not two)."""
    out: list[float] = []
    for p in sorted(float(x) for x in powers or []):
        if not out or abs(p - out[-1]) > rel * out[-1]:
            out.append(p)
    return out


KW_TO_PS = 1.35962


# where the clause of a power statement starts: a line / cell / list break, a sentence end, a comma
CLAUSE_START = re.compile(r"[\n|;•·]|[.,!?](?:\s|$)")
CHARGING_CONTEXT_CHARS = 40


def charging_context(text: str, start: int, end: int, vocab: dict | None = None) -> bool:
    """Is the power statement text[start:end] about CHARGING (טעינה, charging, DC / AC, a charger, V2L / L2V)? The
    statement itself and its own clause before it (at most CHARGING_CONTEXT_CHARS back, never past a line, cell, list
    or sentence break) are read: "טעינה מהירה בהספק מרבי של 451 קילוואט" is a charging power, never a motor power."""
    vocab = vocabulary() if vocab is None else vocab
    blockers = _terms(("charging_words",), vocab.get("charging_words") or [])
    if blockers is None:
        return False
    lead = text[max(0, start - CHARGING_CONTEXT_CHARS):start]
    breaks = list(CLAUSE_START.finditer(lead))
    lead = lead[breaks[-1].end():] if breaks else lead
    return bool(blockers.search(lead + text[start:end]))


def _powers(text: str, vocab: dict) -> set[float]:
    """Power figures in hp (PS and, next to a power word, kW converted). Charging kW is never power: a kW statement
    in a charging context (charging_context: its own words or its clause before it) and battery kWh are skipped."""
    units = _alternation(vocab.get("power_units") or [])
    found: set[float] = set()
    if units:
        for m in re.finditer(rf"(?<![\d.,])(\d{{2,4}})\s*(?:{units})(?![a-z])", text):
            value = float(m.group(1))
            # PS is metric horsepower, the unit of the government's כ"ס: taken as is (never x0.986)
            found.add(value)
    words = _alternation(vocab.get("power_words") or [])
    if words:
        for m in re.finditer(rf"(?:{words})[^\d;|\n]{{0,25}}?(?<![\d.,])(\d{{2,4}})\s*(?:kw|קילוואט|קוט\"ס)(?![a-z])",
                             text):
            if charging_context(text, m.start(), m.end(), vocab):
                continue
            # government power (כ"ס) is metric horsepower: 1 kW = 1.35962 PS (357 kW is the catalog's 486 כ"ס)
            found.add(round(float(m.group(1)) * KW_TO_PS, 1))
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


def _named_trim_noise(identity: TargetIdentity) -> set[str]:
    """Words that do not identify a competing trim. Generic tier labels become meaningful for generic-only targets."""
    return GENERIC_NAMED - GENERIC_TIER_WORDS if identity.qualified_trim_phrases else GENERIC_NAMED


def other_trims_named(text: str, identity: TargetIdentity) -> list[str]:
    """Trim names the text gives ("the Premium version", "Base version", "גרסת Business") that are not the target."""
    named = [w for groups in NAMED_TRIM.findall(normalize_text(text or "")) for w in groups if w]
    noise = _named_trim_noise(identity)
    return sorted({w for w in named if w not in identity.trim_words and w not in noise})


def _masked(pattern, text: str) -> str:
    """The text with every match of `pattern` blanked out, positions kept."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text) if pattern else text


def _year_anchors(norm: str, identity: TargetIdentity, vocab: dict) -> list[tuple[int, int]]:
    """Spans of the target's model family (a longer family such as "corolla cross" masked first) and of its
    manufacturer when no other model family follows it ("XPeng G9" anchors nothing for the G6)."""
    families = vocab.get("model_families") or {}
    anchors: list[tuple[int, int]] = []
    if identity.family:
        work = norm
        for other in families:
            if other != identity.family and _compact(other).startswith(_compact(identity.family)):
                work = _masked(_terms(("family", other), families[other]), work)
        pattern = _terms(("family", identity.family), families.get(identity.family) or [identity.family])
        anchors += [m.span() for m in pattern.finditer(work)] if pattern else []
    terms = (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])
    man = _terms(("manufacturer", identity.manufacturer), terms) if terms else None
    other = _other_family_pattern(identity)
    for m in man.finditer(norm) if man else ():
        after = re.compile(r"[\s\-]*").match(norm, m.end()).end()
        if other is None or other.match(norm, after) is None:
            anchors.append(m.span())
    return anchors


def _short_gap(gap: str, max_tokens: int, other) -> bool:
    """A gap between a name and a year that keeps them one statement: no line / sentence / list break, at most
    `max_tokens` words, and no other model family named in it."""
    if SEGMENT_BREAK.search(gap) or (other is not None and other.search(gap)):
        return False
    return len(re.findall(r"[^\s\-–—(),:]+", gap)) <= max_tokens


def _ignored_kind(norm: str, start: int, end: int) -> str | None:
    """Why a year at norm[start:end] is not a model-year statement whatever its neighbours (B2), else None."""
    line_start = norm.rfind("\n", 0, start) + 1
    line_end = norm.find("\n", end)
    line_end = len(norm) if line_end < 0 else line_end
    before, after = norm[line_start:start], norm[end:line_end]
    token_start = max(norm.rfind(" ", 0, start), norm.rfind("\n", 0, start)) + 1
    token_end = min([i for i in (norm.find(" ", end), norm.find("\n", end)) if i >= 0] or [len(norm)])
    if URL_TOKEN.search(norm[token_start:token_end]):
        return "url"
    if DATE_BEFORE.search(before) or DATE_AFTER.match(after):
        return "date"
    marks = list(COPYRIGHT.finditer(norm, line_start, line_end))
    if any(m.group(0) == "all rights reserved" or abs(m.start() - start) <= 40 for m in marks):
        return "copyright"
    if PUBLISHED.search(before[-40:]):
        return "publication_date"
    if MONTHS.search(before[-15:]) or MONTHS.search(after[:15]):
        return "date"
    if PRICE_VALIDITY.search(before[-40:]):
        return "price_validity"
    return None


def year_context(text: str, identity: TargetIdentity, *, segment: bool = False) -> dict:
    """The model-year statements of a text (normalized or not) and the years it ignores:
    {years: {int}, statements: [{year, kind}], ignored: [{year, kind}]}.

    A statement (kind): `label` (שנת דגם / שנתון / model year / MY2026 / MY26 before the year), `family_adjacent`
    (the target's family or manufacturer name next to the year: "G6 2026", "2026 XPeng G6", "XPeng G6 (2026)", "Corolla
    Touring Sports 2024"), or with `segment=True` (a title / H1 / H2 / first-PDF-line segment that names the target
    family) `identity_segment`. Ignored (kind): `date` (day-month-year forms, a month name within 15 characters),
    `publication_date`, `copyright`, `price_validity`, `url` (a year inside a URL or e-mail), `range`, `prose` (any
    other free-standing year). Deterministic; fail-closed rules for the dimension live in dimension_status."""
    vocab = vocabulary()
    norm = normalize_text(text or "")
    statements: list[tuple[int, str]] = []
    ignored: list[tuple[int, str]] = []
    anchors = _year_anchors(norm, identity, vocab)
    other = _other_family_pattern(identity)
    named = segment and "target" in _family_status(norm, identity, vocab)
    for m in ANY_YEAR.finditer(norm):
        year, start, end = int(m.group(1)), m.start(1), m.end(1)
        if re.search(r"\d[.,]$", norm[max(0, start - 2):start]) or YEAR_UNIT.match(norm, end):
            continue                                  # a figure (12,025 / 2000 rpm / 2010 mm), not a year at all
        kind = _ignored_kind(norm, start, end)
        if kind is None and (re.match(r"\s*[-–]\s*\d", norm[end:end + 4])
                             or re.search(r"\d\s*[-–]\s*$", norm[max(0, start - 4):start])):
            kind = "range"                            # "2019-2024": a production span, not one model year
        if kind:
            ignored.append((year, kind))
            continue
        line_start = norm.rfind("\n", 0, start) + 1
        if YEAR_LABEL.search(norm[max(line_start, start - 24):start]):
            statements.append((year, "label"))
        elif named:
            statements.append((year, "identity_segment"))
        elif any((a_end <= start and _short_gap(norm[a_end:start], 3, other))
                 or (end <= a_start and _short_gap(norm[end:a_start], 1, other)) for a_start, a_end in anchors):
            statements.append((year, "family_adjacent"))
        else:
            ignored.append((year, "prose"))
    for m in SHORT_MY.finditer(norm):
        year = 2000 + int(m.group(1))
        if year <= 2039:
            statements.append((year, "label"))

    def rows(items: list[tuple[int, str]]) -> list[dict]:
        return [{"year": y, "kind": k} for y, k in dict.fromkeys(items)]
    return {"years": {y for y, _ in statements}, "statements": rows(statements), "ignored": rows(ignored)}


def url_year_context(url: str | None, identity: TargetIdentity) -> dict:
    """Model-year statements of a document's own URL: only an explicit family-year slug ("/g6-2026/",
    "/2026-xpeng-g6/", "g6_2026"). Every other year in it (a date directory "/2025/03/...") is ignored (`url`)."""
    vocab = vocabulary()
    path = re.sub(r"^[a-z]+://[^/]+", "", str(url or "").lower().split("#")[0])
    statements: list[tuple[int, str]] = []
    families = [a for a in (vocab.get("model_families") or {}).get(identity.family or "", [identity.family or ""])
                if a and re.fullmatch(r"[a-z0-9 \-+]+", a)]
    makers = [t for t in (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])
              if re.fullmatch(r"[a-z0-9 \-]+", t)]

    def slug(term: str) -> str:
        return r"[-_]?".join(re.escape(p) for p in re.split(r"[\s\-]+", term) if p)
    if families:
        fam = "(?:" + "|".join(slug(f) for f in sorted(families, key=len, reverse=True)) + ")"
        man = "(?:(?:" + "|".join(slug(t) for t in makers) + ")[-_])?" if makers else ""
        edge, end = r"(?:^|[/_\-.])", r"(?=$|[/_\-.?&=])"
        for pattern in (rf"{edge}{man}{fam}[-_](20[0-3]\d){end}", rf"{edge}(20[0-3]\d)[-_]{man}{fam}{end}"):
            statements += [(int(m.group(1)), "url_slug") for m in re.finditer(pattern, path)]
    found = {y for y, _ in statements}
    ignored = [(int(y), "url") for y in ANY_YEAR.findall(path) if int(y) not in found]
    return {"years": found, "statements": [{"year": y, "kind": k} for y, k in dict.fromkeys(statements)],
            "ignored": [{"year": y, "kind": k} for y, k in dict.fromkeys(ignored)]}


def merge_year_contexts(*contexts: dict) -> dict:
    out = {"years": set(), "statements": [], "ignored": []}
    for ctx in contexts:
        out["years"] |= set(ctx.get("years") or ())
        for key in ("statements", "ignored"):
            out[key] += [r for r in ctx.get(key) or [] if r not in out[key]]
    return out


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
    named_noise = _named_trim_noise(identity)
    year_ctx = year_context(norm, identity)
    if identity.trim_words and any(w not in identity.trim_words and w not in named_noise for w in named):
        trim = "negated"        # "the Premium version", "גרסת ה-Premium": a fact about ANOTHER named trim
    for m in trims.finditer(norm) if trims else ():
        # "not available on Business", "לא בגרסת Business": the trim is named to EXCLUDE it
        trim = "negated" if NEGATED_TRIM.search(norm[max(0, m.start() - 25):m.start()]) else "match"
        if trim == "match":
            break
    return {
        "manufacturer": bool(man and man.search(norm)),
        "model": _family_status(norm, identity, vocab),
        "year": year_ctx["years"],
        "year_context": {"statements": year_ctx["statements"], "ignored": year_ctx["ignored"]},
        "body": _keys_found(norm, "body_terms", vocab),
        "propulsion": _keys_found(norm, "propulsion_terms", vocab,
                                  ["plug_in", "battery_electric", "hybrid", "conventional"])
        | {f"weak:{k}" for k in _keys_found(norm, "propulsion_weak_terms", vocab)},
        "displacement": _displacements(norm, vocab),
        "power": _powers(norm, vocab),
        "drivetrain": _keys_found(norm, "drivetrain_terms", vocab),
        "model_code": bool(codes),
        "trim": trim,
        # PR #43 identity parts (never a binding dimension by themselves): version designations ("55tfsie") and a
        # gearbox named with the version
        "designation": designations(norm, vocab),
        "gearbox": _keys_found(norm, "gearbox_terms", vocab, ["automatic", "manual"]),
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
        # found = the recognized model-year statements (year_context). Fail-closed: any statement >= 2 years away
        # without the target is a mismatch; statements only one year off are `adjacent` (an importer markets the same
        # car as the neighbouring model year), which never satisfies anything: the level treats it as absent
        target = identity.year
        if target is None:
            return "absent"
        if target in found:
            return "match" if found == {target} else "mixed"
        return "adjacent" if all(abs(int(y) - target) <= 1 for y in found) else "mismatch"
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
        target, rel = identity.power_hp, power_tolerance(identity)["tolerance"]
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


def _zone_segments(*, title: str | None, url: str | None, headings: list[str] | None = None,
                   text: str | None = None, identity: "TargetIdentity | None" = None,
                   lines: int = 3) -> list[tuple[str, str]]:
    """(channel, segment) of identity_zone: channel "url" for the URL words, "heading" for the title, the main headings
    and (text / PDF) the first lines."""
    url_words = re.sub(r"[/_\-.?=&]+", " ", str(url or ""))
    head = [h for h in headings or [] if h and h.strip()]
    if headings is None:
        head = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:lines]
    parts = [("heading", title or ""), ("url", url_words), *[("heading", h) for h in head]]
    segments = [(channel, seg.strip()) for channel, part in parts for seg in re.split(r"\s[|•·]\s|\|", part)
                if seg.strip()]
    if identity is not None:
        named = [(c, seg) for c, seg in segments
                 if "target" in _family_status(normalize_text(seg), identity, vocabulary())]
        if named:
            segments = named
    return segments


def identity_zone(*, title: str | None, url: str | None, headings: list[str] | None = None,
                  text: str | None = None, identity: "TargetIdentity | None" = None, lines: int = 3) -> str:
    """The part of a document that says WHAT it is about: title, URL words and main headings (HTML; `headings` is
    a list, possibly empty) or the first lines (text / PDF; `headings` is None). Split into list segments ("|", "•")
    and, when any segment names the target model family, only those segments: a navigation menu ("Yaris Hybrid |
    Corolla | RAV4 Plug-in Hybrid | Hilux") never speaks for the page."""
    return "\n".join(seg for _, seg in _zone_segments(title=title, url=url, headings=headings, text=text,
                                                       identity=identity, lines=lines))


def zone_year_context(*, title: str | None, url: str | None, identity: TargetIdentity,
                      headings: list[str] | None = None, subheadings: list[str] | None = None,
                      text: str | None = None) -> dict:
    """The identity zone's model-year statements, per channel: a title / H1 / first-line segment of the zone (an H2
    `subheadings` segment only when it names the target family) whose segment names the family counts every year it
    states (`identity_segment`, B2 contexts aside); other zone segments only through the prose rules; the URL only
    through an explicit family-year slug (url_year_context: "/g6-2026/", never "/2025/03/...")."""
    vocab = vocabulary()
    segments = _zone_segments(title=title, url=url, headings=headings, text=text, identity=identity)
    h2 = [seg.strip() for part in subheadings or [] for seg in re.split(r"\s[|•·]\s|\|", part or "") if seg.strip()]
    texts = [seg for channel, seg in segments if channel == "heading"]
    texts += [seg for seg in h2 if "target" in _family_status(normalize_text(seg), identity, vocab)]
    return merge_year_contexts(url_year_context(url, identity),
                               *[year_context(seg, identity, segment=True) for seg in texts])


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
                     headings: list[str] | None = None, subheadings: list[str] | None = None,
                     body_text: str | None = None) -> dict:
    """Document-level dimension statuses. The identity zone (title, URL, headings) decides first; the full text
    only confirms (see FULL_TEXT_MISMATCH). The trim counts only when the identity zone names it.

    `body_text` (HTML: the page without its chrome, structure_harvest.page_text) is the full text's body; default
    `text`. The year dimension reads model-year statements only (year_context): in the zone per channel
    (zone_year_context, `subheadings` = H2s), in the full text through the prose rules and without the URL words."""
    zone = identity_zone(title=title, url=url, headings=headings, text=text, identity=identity)
    named = zone_names_target(zone, identity)
    body = about_target(text if body_text is None else body_text, identity)
    zone_found = mentions(zone, identity)
    zone_year = zone_year_context(title=title, url=url, identity=identity, headings=headings,
                                  subheadings=subheadings, text=text)
    zone_found["year"] = zone_year["years"]
    # R3 (PR #45): a body sub-variant the zone names and the target's names do not ("Q3 Sportback" for a Q3) is another
    # model of the same catalog family
    subvariant = subvariant_status(zone, identity) if named else None
    full_found = mentions(f"{zone}\n{body}", identity)
    zone_text = identity_zone(title=title, url=None, headings=headings, text=text, identity=identity)
    full_year = year_context(f"{zone_text}\n{body}", identity)
    full_found["year"] = full_year["years"]
    full_found["year_context"] = {k: full_year[k] for k in ("statements", "ignored")}
    combined: dict[str, str] = {}
    zone_statuses = {dim: dimension_status(dim, zone_found[dim], identity) for dim in DIMENSIONS}
    if subvariant and subvariant["status"] == "mismatch":
        zone_statuses["body"] = "mismatch"
    full_statuses = {dim: dimension_status(dim, full_found[dim], identity) for dim in DIMENSIONS}
    year_basis = "none"
    for dim in DIMENSIONS:
        if dim == "trim":
            combined[dim] = zone_statuses[dim] if named else "absent"
        elif zone_statuses[dim] != "absent" and (named or zone_statuses[dim] != "mismatch"):
            # a zone that never names the target family (a price list headed by another model) cannot veto
            combined[dim] = zone_statuses[dim]
            year_basis = "identity_zone" if dim == "year" else year_basis
        elif full_statuses[dim] == "mismatch":
            combined[dim] = FULL_TEXT_MISMATCH.get(dim, "absent")
            year_basis = "full_text_mismatch_ignored" if dim == "year" else year_basis
        else:
            combined[dim] = full_statuses[dim]
            year_basis = "full_text" if dim == "year" and combined[dim] != "absent" else year_basis
    decided = zone_year if year_basis == "identity_zone" else full_year
    ignored = merge_year_contexts(zone_year, full_year)["ignored"]
    return {"statuses": combined, "zone_statuses": zone_statuses, "full_statuses": full_statuses,
            "trim_named_in_document": bool(full_found["trim"]),
            # another named trim anywhere in the document (single_trim_catalog never applies to such a document)
            "other_trims_named": other_trims_named(f"{zone}\n{body}", identity),
            # telemetry only (never read by bind): the model-year statements that decided the year dimension and every
            # year the rules ignored (copyright, publication date, URL, prose, ...)
            "year_context": {"status": combined["year"], "basis": year_basis,
                             "statements": decided["statements"] if year_basis != "none" else [],
                             "ignored": ignored},
            "mentions": {k: sorted(v) if isinstance(v, set) else v for k, v in full_found.items()},
            # PR #43 (H1): how many powertrain versions the document names (distinct powers, version designations)
            "powertrain_versions": {"powers": distinct_powers(full_found["power"]),
                                    "designations": sorted(full_found["designation"])},
            # PR #43 (H3): does the document state the target's model year anywhere (zone or full text)?
            "states_target_year": identity.year is not None
            and identity.year in (set(zone_year["years"]) | set(full_year["years"])),
            # PR #45 (R3): the identity zone's body sub-variant verdict (None: it decides nothing)
            "body_subvariant": subvariant}


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


# --- catalog family entries (R1 / R1b / R3 / Document Variant Map) ----------------------------------------------------

CATALOG_PARTS = ("manufacturer", "family", "year", "body", "propulsion", "drivetrain", "power", "displacement_l")
DEFAULT_POWER_TOLERANCE = 0.03
_FAMILY_ENTRIES: dict[tuple, list[dict]] = {}


def parse_catalog_key(key: str) -> dict | None:
    """The parts of a catalog index key (catalog_key); power as an int (None when empty)."""
    parts = str(key).split("|")
    if len(parts) != len(CATALOG_PARTS):
        return None
    out = dict(zip(CATALOG_PARTS, parts))
    try:
        out["power"] = int(out["power"]) if out["power"] else None
    except ValueError:
        out["power"] = None
    return out


def catalog_family_entries(identity: TargetIdentity, index: dict | None = None) -> list[dict]:
    """Every catalog index entry of the target's manufacturer / model family / model year: [{key, parts, trims,
    records, complete}] (an entry without "complete" takes the index's own default). [] when an identity part is
    unknown or the index has no entry for it. data/catalog_trim_index.json only (no runtime database)."""
    index = trim_index() if index is None else index
    entries = index.get("entries") or {}
    if not entries or identity.manufacturer in (None, "") or not identity.family or identity.year is None:
        return []
    key = (id(entries), str(identity.manufacturer), str(identity.family), str(identity.year))
    cached = _FAMILY_ENTRIES.get(key)
    if cached is not None:
        return cached
    prefix = f"{identity.manufacturer}|{identity.family}|{identity.year}|"
    default_complete = index.get("complete", False) is True
    out = []
    for name in sorted(k for k in entries if k.startswith(prefix)):
        entry = entries[name] or {}
        parts = parse_catalog_key(name)
        if parts is None:
            continue
        out.append({"key": name, "parts": parts, "trims": [normalize_catalog_trim(t) for t in entry.get("trims") or []],
                    "records": [str(r) for r in entry.get("records") or []],
                    "complete": entry.get("complete", default_complete) is True})
    if len(_FAMILY_ENTRIES) > 4096:
        _FAMILY_ENTRIES.clear()
    _FAMILY_ENTRIES[key] = out
    return out


def _single_catalog_value(identity: TargetIdentity, part: str, target: Any, index: dict | None) -> dict | None:
    """{entries, records} when the target's manufacturer / family / model year has catalog entries, ALL of them
    complete, and every one has `target` as its `part`; None otherwise (fail-closed: no entry, an incomplete entry, an
    unknown target value, or another value)."""
    if target in (None, ""):
        return None
    entries = catalog_family_entries(identity, index)
    if not entries or any(not e["complete"] for e in entries):
        return None
    if any(e["parts"][part] != str(target) for e in entries):
        return None
    return {"entries": len(entries), "records": [r for e in entries for r in e["records"]][:20]}


def single_propulsion_catalog(identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """R1: every (complete) catalog entry of the target's manufacturer / family / model year has its propulsion."""
    return _single_catalog_value(identity, "propulsion", identity.propulsion, index)


def single_body_catalog(identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """R1b: every (complete) catalog entry of the target's manufacturer / family / model year has its body."""
    return _single_catalog_value(identity, "body", identity.body, index)


_SIBLINGS: dict[tuple, Any] = {}


def catalog_sibling_families(identity: TargetIdentity, index: dict | None = None):
    """A pattern of the OTHER model families the catalog lists for the target's manufacturer (any year), or None. The
    identity vocabulary knows only some families; the catalog knows the whole range ("g9", "x9", "p7 plus" for the
    G6). Used only as a fail-closed safeguard of R1 / R1b: a noisy family name can only stop a rule."""
    index = trim_index() if index is None else index
    entries = index.get("entries") or {}
    if not entries or not identity.manufacturer or not identity.family:
        return None
    key = (id(entries), identity.manufacturer, identity.family)
    if key not in _SIBLINGS:
        prefix = f"{identity.manufacturer}|"
        own = {_compact(identity.family)} | {_compact(t) for t in (vocabulary().get("manufacturers") or {})
                                              .get(str(identity.manufacturer), [])}
        names = {k.split("|")[1] for k in entries if k.startswith(prefix)}
        names = {n for n in names if n and re.search(r"[a-zא-ת]", n) and len(_compact(n)) >= 2
                 and _compact(n) not in own and not _compact(identity.family).startswith(_compact(n))}
        _SIBLINGS[key] = compile_terms(sorted(names)) if names else None
    return _SIBLINGS[key]


SIBLING_LAYERS = ("value_clause", "column_header", "column_identity", "quote", "section_heading")


def _names_sibling_family(identity: TargetIdentity, layers: list[tuple[str, str]] | None,
                          context: Iterable[str] = ()) -> bool:
    """Does the value's own clause, its column, its quote, the heading above it or a safeguard-only `context` line (the
    row label above it) name another model family of the manufacturer (catalog or identity vocabulary)?"""
    texts = [text for name, text in layers or [] if name in SIBLING_LAYERS and text] + [t for t in context if t]
    patterns = [p for p in (catalog_sibling_families(identity), _other_family_pattern(identity)) if p is not None]
    return any(p.search(normalize_text(text)) for p in patterns for text in texts)


_TOLERANCES: dict[tuple, dict] = {}


def power_tolerance(identity: TargetIdentity, index: dict | None = None) -> dict:
    """R3: {tolerance, neighbour_hp} of the power dimension: min(3 %, 0.5 x the relative gap between the target's power
    and the nearest OTHER catalog power (bucket) of the same manufacturer / family / year / body / propulsion /
    drivetrain). No catalog neighbour (or an unknown identity part): 3 %. It only ever tightens."""
    if identity.power_hp is None:
        return {"tolerance": DEFAULT_POWER_TOLERANCE, "neighbour_hp": None}
    index = trim_index() if index is None else index
    key = (id(index.get("entries") or {}), identity.manufacturer, identity.family, identity.year, identity.body,
           identity.propulsion, identity.drivetrain, identity.power_hp)
    cached = _TOLERANCES.get(key)
    if cached is not None:
        return cached
    own = power_bucket(identity.power_hp)
    same = [e for e in catalog_family_entries(identity, index)
            if identity.body and identity.propulsion and identity.drivetrain
            and (e["parts"]["body"], e["parts"]["propulsion"], e["parts"]["drivetrain"])
            == (identity.body, identity.propulsion, identity.drivetrain)]
    others = sorted({e["parts"]["power"] for e in same if e["parts"]["power"] and e["parts"]["power"] != own})
    out = {"tolerance": DEFAULT_POWER_TOLERANCE, "neighbour_hp": None}
    if others and identity.power_hp:
        nearest = min(others, key=lambda p: (abs(p - identity.power_hp), p))
        gap = abs(nearest - identity.power_hp) / abs(identity.power_hp)
        out = {"tolerance": round(min(DEFAULT_POWER_TOLERANCE, 0.5 * gap), 5), "neighbour_hp": nearest}
    if len(_TOLERANCES) > 4096:
        _TOLERANCES.clear()
    _TOLERANCES[key] = out
    return out


# --- binding --------------------------------------------------------------------------------------------

HYBRID_PROPULSIONS = ("plug_in", "hybrid")
# H1: the fact layers whose stated power (other than the target's engine power) leaves a hybrid's variant unresolved
POWER_LAYERS = ("value_clause", "column_header", "column_identity", "quote", "section_heading")
STALE_PUBLICATION_YEARS = 2


def relative_reference(text: str, vocab: dict | None = None) -> str | None:
    """H2: the comparative reference to another version a text makes ("הבכיר יותר", "the more powerful"), else None."""
    vocab = vocabulary() if vocab is None else vocab
    pattern = _terms(("relative_variant_terms",), (vocab.get("relative_variant_terms") or {}).get("terms") or [])
    m = pattern.search(normalize_text(text or "")) if pattern else None
    return m.group(0).strip() if m else None


def names_version(region: dict | None) -> bool:
    """Does a Document Variant Map region's identity name a version: a designation, the model code, the government
    model code or a catalog trim? (H1: only such a region proves a hybrid's variant in a multi-version document.)"""
    vec = (region or {}).get("identity") or {}
    return bool(vec.get("designation") or vec.get("model_code") or vec.get("gov_model_code") or vec.get("trim")
                or region.get("rule") == "gov_model_code")


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


def fact_layer_statuses(identity: TargetIdentity, layers: list[tuple[str, str]] | None,
                        trim_named_in_document: bool = False) -> list[tuple[str, dict[str, str]]]:
    """(layer name, {dimension: status}) of every non-empty fact layer, most specific first: what bind() reads."""
    return [(name, _layer_statuses(name, text, identity, trim_named_in_document)) for name, text in layers or []
            if text]


TECHNICAL_DIMS = ("displacement", "power", "drivetrain")
# R4 (PR #45): the bases that name the version for ONE fact (its own context layers, or a target DVM region)
FACT_LAYER_NAMES = ("value_clause", "column_header", "column_identity", "quote", "source_line", "section_heading")
TARGET_REGION_BASES = ("dvm_region", "gov_model_code", "designation_region")


def _ladder(s: dict[str, str], identity: TargetIdentity, veto_dims: tuple[str, ...],
            market: str | None, version_open: bool = False) -> tuple[str, str | None]:
    """(binding level, trim basis) of effective dimension statuses (vetoes are applied by the caller). `version_open`
    (H1, hybrids): the fact or its document names another powertrain version, so displacement alone never reaches the
    technical variant (the model code, or a version-naming region the caller proved, still does)."""
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
                if version_open:
                    technical = s["model_code"] == "match"
                unresolved = (s["displacement"] == "mixed" or s["drivetrain"] == "mixed"
                              or ("power" in veto_dims and s["power"] == "mixed"))
                if technical and not unresolved:
                    level = "exact_technical_variant"
                    if s["trim"] == "match" and market and market == identity.target_market:
                        level = "exact_market_trim"
                        # a generic government trim (MAX) can only match as its qualified phrase ("g6 max")
                        basis = "qualified_trim_phrase" if identity.qualified_trim_phrases else None
    return level, basis


def _names_other_propulsion(identity: TargetIdentity, layers: list[tuple[str, str]] | None,
                            veto_layers: list[tuple[str, str]] | None, document_propulsions: Iterable[str]) -> bool:
    """Does a fact layer, a veto layer or the document (its full-text mentions) name a propulsion other than the
    target's, strong or weak? (R1's third safeguard.)"""
    target = identity.propulsion
    named = {str(k).removeprefix("weak:") for k in document_propulsions or []}
    for _, text in [*(layers or []), *(veto_layers or [])]:
        if text:
            named |= {str(k).removeprefix("weak:") for k in mentions(text, identity)["propulsion"]}
    return bool(named - {target})


def bind(identity: TargetIdentity, doc_statuses: dict[str, str], layers: list[tuple[str, str]] | None = None,
         veto_layers: list[tuple[str, str]] | None = None, *, market: str | None = None,
         requirement: str | None = None, model_declared_different: bool = False,
         trim_named_in_document: bool = False, source_authority: str | None = None,
         document_names_family: bool = False, other_trims_named: list[str] | None = None,
         document_propulsions: list[str] | None = None, brand_policy: dict | None = None,
         region: dict | None = None, safeguard_context: Iterable[str] = (), market_trim: dict | None = None,
         powertrain_versions: dict | None = None, stale: dict | None = None,
         version_page: dict | None = None, engine_invariant: bool = False, multi_version: dict | None = None) -> dict:
    """The effective binding of a fact (or, with no layers, of the whole document). `source_authority`,
    `document_names_family` and `other_trims_named` (the document profile's) feed the single_trim_catalog rule only;
    `other_trims_named=None` (unknown) never lets it apply.

    binding-v4 inputs (all optional, None = the rule does not apply): `document_propulsions` (the document profile's
    propulsion mentions; R1 reads them), `brand_policy` (R4: the caller's eligibility verdict for a brand-wide warranty
    statement), `region` (R2: the fact's Document Variant Map decision, src/variant_map.fact_region),
    `safeguard_context` (lines that can only STOP R1 / R1b: the row label right above the value) and `market_trim`
    (F5: the document's model-code table verdict, src/variant_map.market_trim_offer; `market_trim_not_offered` keeps
    every fact of the document below exact_market_trim).

    PR #43 inputs: `powertrain_versions` (the document profile's distinct powers and version designations, H1) and
    `stale` (the caller's stale-publication verdict, H3: {publication_date, basis}); the flags they raise are recorded as
    `binding_flags` (system_power_unmapped, several_powertrain_versions, relative_variant_reference, stale_publication).

    PR #44 inputs: `version_page` (src/il_version_pages.version_page_verdict of the fact's document; its page statuses
    already replaced the document statuses of a single-version page) and `engine_invariant` (the field's
    variant_invariance is "engine"), see binding-v6 in the module doc.

    PR #45 input: `multi_version` (evidence_admission.multi_version_context: how many technical versions the document's
    inventory names, whether the value is repeated identically for every version, whether the fact's DVM region is
    `unresolved / inventory_without_target`), see binding-v7 in the module doc."""
    effective: dict[str, dict] = {}
    layer_statuses = fact_layer_statuses(identity, layers, trim_named_in_document)
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
    flags: list[str] = []          # PR #43 binding flags (telemetry; the caps they cause are applied below)
    version_open = False
    rules: list[str] = []          # binding-v4 rules applied to this fact, in order
    page = version_page or {}
    page_accepted = page.get("status") == "accepted"
    if identity.propulsion in HYBRID_PROPULSIONS:
        # H1: the government power of a hybrid is the engine's; a document states system power. A fact whose own
        # clause / quote / column / section states another power, or a document naming several powertrain versions,
        # is never identified by its displacement alone
        power_unmapped = any(name in POWER_LAYERS and st["power"] in ("mismatch", "mixed") for name, st in layer_statuses)
        # PR #44 (P4.2 tightening): a single-version page that states another power (its system power) is just as
        # unmapped, unless the catalog has exactly ONE entry for the target's manufacturer / family / year /
        # propulsion / drivetrain (then the page's one version can only be the target's)
        if page.get("single_version") and (page.get("page_statuses") or {}).get("power") in ("mismatch", "mixed") \
                and not page.get("catalog_single_entry"):
            power_unmapped = True
        several = False
        versions = powertrain_versions or {}
        if len(versions.get("powers") or []) >= 2 or len(versions.get("designations") or []) >= 2:
            several = not (page.get("single_version") and page_accepted)
        if power_unmapped:
            flags.append("system_power_unmapped")
            version_open = True
        if several:
            flags.append("several_powertrain_versions")
            version_open = True
        if page_accepted and not several:
            # P4.2: an accepted single-version Israeli page (power match or the catalog's single entry) is the target
            version_open = False
        elif engine_invariant and version_open and not several and s["displacement"] == "match":
            # P4.1: a gearbox or a body dimension is the same for every powertrain version of one engine: when every
            # version the document names shares the target's displacement, the unmapped system power does not hold it
            version_open = False
            rules.append("engine_invariant")
    level, basis = _ladder(s, identity, veto_dims, market, version_open)
    raised_by: str | None = None   # the rule that raised the level last
    if "engine_invariant" in rules and level_index(level) >= level_index("exact_technical_variant"):
        raised_by = "engine_invariant"

    def apply(rule: str) -> None:
        nonlocal level, basis, raised_by
        rules.append(rule)
        new_level, new_basis = _ladder(s, identity, veto_dims, market, version_open)
        if level_index(new_level) > level_index(level):
            raised_by = rule
        level, basis = new_level, new_basis

    # R1b / R1: the government catalog of the target's model year knows only one body / one propulsion
    # (only for a fact about the target family: body_powertrain needs the model to match, and its own words name no
    # other catalog family of the manufacturer)
    catalog_rules = s["model"] == "match" and not _names_sibling_family(identity, layers, safeguard_context)
    if s["body"] == "mixed" and catalog_rules:
        catalog_body = single_body_catalog(identity)
        if catalog_body is not None:
            s["body"] = "match"
            effective["body"] = {"status": "match", "basis": effective["body"]["basis"], "was": "mixed",
                                 "rule": "single_body_catalog", "catalog_entries": catalog_body["entries"]}
            apply("single_body_catalog")
    if s["propulsion"] == "absent" and identity.propulsion not in (None, "conventional") \
            and document_propulsions is not None and catalog_rules:
        catalog_propulsion = single_propulsion_catalog(identity)
        if catalog_propulsion is not None and not _names_other_propulsion(identity, layers, veto_layers,
                                                                          document_propulsions):
            s["propulsion"] = "match"
            effective["propulsion"] = {"status": "match", "basis": "single_propulsion_catalog", "was": "absent",
                                       "catalog_entries": catalog_propulsion["entries"]}
            apply("single_propulsion_catalog")
    # R4: a brand-wide warranty statement of the target manufacturer's official target-market source (eligibility:
    # evidence_admission.fact_binding) that names no model binds at body_powertrain
    policy = None
    if brand_policy and not vetoes and not model_declared_different and effective["model"]["basis"] == "document" \
            and s["model"] != "mismatch" and s["year"] != "mismatch" \
            and level_index(level) < level_index("body_powertrain"):
        policy = dict(brand_policy)
        level, raised_by = "body_powertrain", "brand_policy"
        rules.append("brand_policy")
    # R2: the fact's Document Variant Map region (positive proof completes absent / mixed technical dimensions that the
    # value's own clause did not decide; a contradicting region vetoes a dimension no fact layer decided)
    region_trim = False
    if region and region.get("allowed") and not vetoes and not model_declared_different and policy is None \
            and s["model"] == "match":
        status = region.get("status")
        if status in ("target", "shared"):
            # a fact already bound at the technical variant only gains a region's trim (never other dimensions)
            exact = level_index(level) >= level_index("exact_technical_variant")
            open_dims = [] if exact else [d for d in TECHNICAL_DIMS if s[d] in ("absent", "mixed")]
            blocked = any(effective[d]["basis"] == "value_clause" and s[d] == "mixed" for d in open_dims)
            # H1: in a multi-version hybrid document only a target region that names the version (designation, model
            # code, catalog trim) proves the technical variant; a shared row or an unnamed region never does
            proves_version = version_open and status == "target" and names_version(region)
            if version_open and not proves_version:
                open_dims = []
            if not blocked and level_index(level) >= level_index("body_powertrain"):
                rule = "dvm_region" if status == "target" else "dvm_shared"
                if status == "target" and region.get("rule") == "gov_model_code":
                    rule = "gov_model_code"          # F5: the region names the target's government model code
                if region.get("reason") == "designation_region":
                    rule = "designation_region"      # PR #43: the region's designation is the target region's
                if proves_version:
                    version_open = False
                    effective["version"] = {"status": "match", "basis": rule, "region": region.get("region_id"),
                                            "designation": (region.get("identity") or {}).get("designation")}
                for dim in open_dims:
                    effective[dim] = {"status": "match", "basis": rule, "was": s[dim],
                                      "region": region.get("region_id")}
                    s[dim] = "match"
                if status == "target" and region.get("trim") == "match" and s["trim"] == "absent":
                    effective["trim"] = {"status": "match", "basis": rule, "region": region.get("region_id")}
                    s["trim"] = "match"
                    region_trim = True
                if open_dims or region_trim or proves_version:
                    apply(rule)
        elif status == "other_variant":
            dim = region.get("contradicts")
            if dim in veto_dims and s.get(dim) in ("absent", "mixed") \
                    and effective.get(dim, {}).get("basis") != "value_clause":
                vetoes.append(f"{dim}_mismatch@dvm_region")
                rules.append("dvm_other_variant")
    if region_trim and level == "exact_market_trim":
        basis = "gov_model_code" if region.get("rule") == "gov_model_code" else "dvm_region"
    fact_mixed = any(s[d] == "mixed" and effective[d]["basis"] != "document"
                     for d in ("body", "propulsion", *TECHNICAL_DIMS))
    if page_accepted and not vetoes and not model_declared_different and s["model"] == "match" and not fact_mixed \
            and level_index(level) >= level_index("generation") and s["year"] not in ("mismatch", "adjacent"):
        # P2: every value of an accepted Israeli version page binds the technical variant; its market trim only when
        # the page names the target trim (title / H1 / URL: the qualified phrase or a catalog trim form)
        rules.append("il_version_page")
        if level_index(level) < level_index("exact_technical_variant"):
            level, basis = "exact_technical_variant", None
        if page.get("trim_named") and s["trim"] != "mismatch" and market and market == identity.target_market:
            effective["trim"] = {"status": "match", "basis": "il_version_page"}
            s["trim"] = "match"
            level = "exact_market_trim"
        raised_by = "il_version_page"
    not_offered = bool(market_trim and market_trim.get("status") == "market_trim_not_offered")
    if not_offered and level == "exact_market_trim":
        # F5: the document's complete model-code table sells the target's technical variant under other codes only
        level, basis = "exact_technical_variant", None
        rules.append("market_trim_not_offered")
    catalog = None
    if (required == "exact_market_trim" and level == "exact_technical_variant" and not vetoes and not not_offered
            and not page_accepted
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
    # H2: a value whose own clause refers to another version ("הבכיר יותר מוסיף ... חישוקי 22") is that version's:
    # heading-level evidence never binds it to a trim or technical variant
    own = next((text for name, text in layers or [] if name == "value_clause" and text), None)
    own = own if own is not None else next((text for name, text in layers or [] if name == "quote" and text), None)
    reference = relative_reference(own) if own else None
    if reference:
        flags.append("relative_variant_reference")
        if level_index(level) > level_index("body_powertrain"):
            level, basis, catalog, raised_by = "body_powertrain", None, None, None
    # R4 (PR #45): a value stated once in a document whose inventory names >= 2 technical versions is not identified by
    # the document's own statuses or a shared-row reading: only a fact layer or a target DVM region naming the version
    # proves it (or the value repeated identically for every version). A fact whose DVM region says the inventory does
    # not contain the target at all never binds the technical variant of a multi-version document
    mv = multi_version or {}
    if int(mv.get("versions") or 0) >= 2 and not page_accepted \
            and level_index(level) >= level_index("exact_technical_variant"):
        technical = [d for d in ("displacement", "model_code", "power") if s.get(d) == "match"]
        proven = any(effective[d].get("basis") in FACT_LAYER_NAMES + TARGET_REGION_BASES for d in technical) \
            or bool(region and region.get("allowed") and region.get("status") == "target")
        if mv.get("inventory_without_target") or not (proven or mv.get("repeated")):
            flags.append("single_value_multi_version")
            level, basis, catalog, raised_by = "body_powertrain", None, None, None
    # R5 (PR #45): a single-version page whose stated power contradicts its own displacement against the catalog
    if page.get("page_inconsistent"):
        flags.append("page_inconsistent")
        if level_index(level) > level_index("body_powertrain"):
            level, basis, catalog, raised_by = "body_powertrain", None, None, None
    # H3: a document published >= 2 years before the target model year that never states that year speaks of an
    # earlier car: at most the generation, unless a DVM region assigned to the target by power + designation holds it
    if stale:
        proven = bool(region and region.get("allowed") and region.get("status") == "target"
                      and region.get("reason") in ("catalog_assignment", "designation_region")
                      and (region.get("identity") or {}).get("designation"))
        if not proven:
            flags.append("stale_publication")
            if level_index(level) > level_index("generation"):
                level, basis, catalog, raised_by = "generation", None, None, None
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
    if "version" in effective:
        dimensions["version"] = effective["version"]
    if basis == "qualified_trim_phrase" and "trim" in dimensions:
        dimensions["trim"] = {**dimensions["trim"], "rule": "qualified_trim_phrase"}
    if catalog is not None:
        dimensions["trim"] = {"status": "match", "basis": "single_trim_catalog", "catalog_key": catalog["key"],
                              "catalog_trim": catalog["trim"], "catalog_records": catalog["records"]}
    if "power" in dimensions and identity.power_hp is not None:
        dimensions["power"] = {**dimensions["power"], "tolerance": power_tolerance(identity)["tolerance"]}
    out = {"binding_level": level, "variant_match": variant_match, "binding_requirement": required,
           "binding_veto": vetoes, "binding_dimensions": dimensions, "binding_version": BINDING_VERSION}
    if raised_by in ("il_version_page", "engine_invariant") and level_index(level) >= level_index(
            "exact_technical_variant") and not vetoes and not model_declared_different:
        out["binding_basis"] = raised_by
    elif catalog is not None or (basis and level == "exact_market_trim" and not raised_by):
        out["binding_basis"] = basis
    elif raised_by and level_index(level) > level_index("unknown") and not vetoes and not model_declared_different:
        out["binding_basis"] = raised_by
    if rules:
        out["binding_rules"] = rules
    if flags:
        out["binding_flags"] = flags
        if reference:
            out["relative_reference"] = reference
        if stale and "stale_publication" in flags:
            out["stale_publication"] = dict(stale)
    if market_trim:
        out["market_trim"] = dict(market_trim)
    if policy is not None:
        out["binding_policy"] = policy
    return out


# --- binding gap diagnosis (observational) ----------------------------------------------------------------

def binding_gaps(item: dict, requirement: str | None = None, propulsion: str | None = None) -> list[str]:
    """Why an evidence item's binding stopped below its requirement, from its own binding_veto / binding_dimensions:
    `veto:<dim>`, or the dimension(s) that kept it from the next level (`trim_absent`, `power_absent`, `power_mixed`,
    `model_mixed`, ...), or `market` (bound at the trim but outside the target market). Never changes anything.
    `propulsion` (the target's, when the caller knows it): a battery_electric target has no displacement, so its
    technical gap is `power_*` / `model_code_*` only (never `displacement_*`)."""
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
    # PR #43: the flag that capped / held the fact (system_power_unmapped, several_powertrain_versions,
    # relative_variant_reference, stale_publication) is its gap
    flagged = [f for f in item.get("binding_flags") or [] if f in BINDING_FLAG_GAPS]
    if flagged:
        return sorted(set(flagged))
    gaps: list[str] = []
    if level == "unknown":
        gaps.append(f"model_{dims['model']}")
    elif level == "model_family":
        gaps.append(f"year_{dims['year']}")
    elif level == "generation":
        gaps += [f"{d}_{dims[d]}" for d, ok in (("model", ("match",)), ("body", ("match", "absent")),
                                                ("propulsion", ("match",))) if dims[d] not in ok]
    elif level == "body_powertrain":
        bev = propulsion == "battery_electric"
        mixed = [f"{d}_mixed" for d in ("displacement", "drivetrain", "power") if dims[d] == "mixed"
                 and not (bev and d == "displacement")]
        if mixed:
            gaps += mixed
        elif bev:
            gaps += [f"{d}_{dims[d]}" for d in ("power", "model_code") if dims[d] != "match"]
        elif dims["power"] == "match":
            gaps.append(f"displacement_{dims['displacement']}")
        else:
            gaps += [f"{d}_{dims[d]}" for d in ("displacement", "power") if dims[d] != "match"]
    elif level == "exact_technical_variant":
        gaps.append("trim_absent" if dims["trim"] == "absent" else "market" if dims["trim"] == "match"
                    else f"trim_{dims['trim']}")
    return sorted(set(gaps)) or ["unresolved"]


BINDING_FLAG_GAPS = ("system_power_unmapped", "several_powertrain_versions", "relative_variant_reference",
                     "stale_publication", "single_value_multi_version", "page_inconsistent")


def is_trim_gap(gap: str) -> bool:
    return gap.startswith("trim_") or gap == "veto:trim"

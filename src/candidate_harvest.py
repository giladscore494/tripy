"""Deterministic Level-2 candidate harvesting: mechanical reading of every fetched document.

    document in DocumentCache
            ↓
    segments (parsed ONCE per document): text lines (reversed-Hebrew PDF lines repaired),
    table rows (HTML tables, <dl>, PDF tables incl. borderless ones) and structured-data leaves (JSON-LD, page state)
    and structural DOM pairs of HTML pages (src/structure_harvest.py, kind "pair", method dom_pair)
    + ADDITION: unit-anchored values (number + unit with exactly one field's alias nearby, method unit_anchor); an
      addition never replaces or displaces a candidate of the segments above
            ↓
    generic matchers configured by the field dictionary (data/enrichment_fields.json):
    numeric · boolean · enum · gearbox · tire_size · charging_time · charging_window · warranty ·
    price · text
            ↓
    CANDIDATES for every field that has dictionary metadata, cached next to the document as
    derived_field_candidates_<schema_hash>.json (shared by every vehicle and worker)

A candidate is NOT evidence. It records that the parser found a label/value pair; it never sets a
field state, never resolves a conflict and never reaches the finalizer as a fact. Only the model
can promote it, through store_evidence, after judging market and variant. `parser_confidence` is the
confidence that the label/value pairing was parsed correctly, never that the value belongs to the
requested vehicle variant.

Nothing here knows a field name: every field-specific word, unit, range and rule comes from the
dictionary, so an alias change is a JSON edit. The Python provides generic primitives only.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlparse

from .fields import DICTIONARY_KEYS, harvest_vocabulary, normalize_field_name, sanity_specs

# v2: booleans need a stated value (label-only is never true); v3: structural DOM pairs (src/structure_harvest.py),
# PDF tables without ruling lines (tools/extract), unit-anchored candidates
# v4: invalidate candidates cached before navigation exclusions and structural extraction limits.
# v7 (PR #40): imperial volume / Wh units are known other units, x-joined dimension groups share their qualifiers,
# Hebrew construct forms of exclusion terms, model years and unit-less values of unit-required fields are rejected
# v8 (PR #42): visually ordered right-to-left PDF lines / cells are detected by dictionary hit rate and reordered
# v10 (PR #44): rpm_guard (an engine speed is never a torque), a unit named in the row label converts a bare value
# (kgf·m -> Nm, rounded to whole Nm)
HARVESTER_VERSION = "harvest-v10"
MAX_CANDIDATES_PER_FIELD_PER_DOC = 12
MAX_CANDIDATES_PER_DOC = 400
MAX_STRUCTURED_LEAVES = 3000
QUOTE_CHARS = 240
LINE_BACK, LINE_FWD = 40, 60          # chars around an alias where its value may sit
EXCLUSION_PAD, POSITIVE_PAD = 15, 45  # context windows for exclusion/negative vs positive terms
MIN_CONFIDENCE = 0.3

# --- normalization ---------------------------------------------------------------------------------
# Character-for-character (length preserving), so a span in the normalized text is the same span in
# the original text and quotes can be cut from the untouched document.

_CHAR_MAP = {
    "״": '"', "”": '"', "“": '"', "„": '"', "″": '"', "‟": '"',
    "׳": "'", "’": "'", "‘": "'", "`": "'", "′": "'", "‚": "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-", "־": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", "‏": " ", "‎": " ",
    "​": " ", "\t": " ", "×": "x",
}
_TRANS = str.maketrans(_CHAR_MAP)
HEBREW = re.compile(r"[א-ת]")
FINAL_LETTERS = "ךםןףץ"


def normalize_text(text: str) -> str:
    """Conservative, length-preserving normalization: quotes/geresh/gershayim, dashes, special spaces,
    case. Raw document text is never modified; this is only the string matchers read."""
    out = (text or "").translate(_TRANS)
    lowered = out.lower()
    return lowered if len(lowered) == len(out) else out


def normalize_term(term: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(term)).strip()


def _reverse_token(token: str) -> str:
    flipped = token[::-1].translate(str.maketrans("()[]{}<>", ")(][}{><"))
    return re.sub(r"\d+(?:[.,:/]\d+)*", lambda m: m.group()[::-1], flipped)


def reverse_hebrew_line(line: str) -> str:
    """Logical order of a visually-ordered (reversed) Hebrew PDF line: token order reversed, Hebrew
    tokens reversed character-wise, digit runs kept left-to-right."""
    tokens = line.split()
    return " ".join(_reverse_token(t) if HEBREW.search(t) else t for t in reversed(tokens))


# --- visually ordered (reversed) right-to-left text (PR #42 F4) ------------------------------------------------------
# PDF text arrives in VISUAL order: pdfplumber reads glyphs left to right, so a Hebrew run "הספק מרבי (כ"ס)" comes out
# as ')ס"כ( יברמ קפסה'. Detection reads the line's Hebrew words against a dictionary (field aliases, the identity
# vocabulary, the harvest vocabulary): a line is visual when its words reversed hit the dictionary more often than as
# read (a word starting with a final letter form counts against its reading). Normalization is the bidi reordering of
# a right-to-left line: its runs in reverse order; a Hebrew run (Hebrew tokens and the numbers between them) word order
# reversed and each word read backwards (digit groups kept); a Latin run ("Long Range", "kWh/100 km") kept as is; a
# free-standing number its own run ("476 286 258 <label>" -> "<label> 258 286 476").

HEBREW_WORD = re.compile(r"[א-ת]+(?:[\"'][א-ת]+)*")
LATIN = re.compile(r"[a-zA-Z]")
WORD_PREFIXES = "ובהלמשכ"
_RTL_WORDS: dict[tuple, frozenset] = {}


def rtl_dictionary(hebrew_terms: Iterable[str] = ()) -> frozenset:
    """The Hebrew words reversal detection reads: of `hebrew_terms` (field aliases), the identity vocabulary
    (data/identity_vocabulary.json) and the harvest vocabulary. Cached per term set."""
    terms = tuple(sorted(set(hebrew_terms)))
    cached = _RTL_WORDS.get(terms)
    if cached is not None:
        return cached
    from .document_binding import vocabulary

    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(w for w in HEBREW_WORD.findall(normalize_text(value)) if len(w) >= 2)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not str(key).startswith("_"):
                    walk(key)
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
    walk(list(terms))
    walk(vocabulary())
    walk(harvest_vocabulary())
    if len(_RTL_WORDS) > 64:
        _RTL_WORDS.clear()
    _RTL_WORDS[terms] = frozenset(found)
    return _RTL_WORDS[terms]


def _known(word: str, words: frozenset) -> bool:
    return word in words or (len(word) > 2 and word[0] in WORD_PREFIXES and word[1:] in words)


def rtl_reversal_score(line: str, words: frozenset) -> dict:
    """{as_read, reversed}: dictionary hits of the line's Hebrew words as read and read backwards, each minus the
    words that would start with a final letter form (impossible in Hebrew)."""
    hebrew = HEBREW_WORD.findall(normalize_text(line))
    backwards = [w[::-1] for w in hebrew]

    def score(items: list[str]) -> int:
        return sum(1 for w in items if _known(w, words)) - sum(1 for w in items if w[0] in FINAL_LETTERS)
    return {"as_read": score(hebrew), "reversed": score(backwards), "words": len(hebrew)}


def _backwards(token: str) -> str:
    """A token read backwards, digit groups kept left to right ("100-0" stays a range, "(כ"ס)" is not mirrored: the
    PDF stores the code points in visual positions)."""
    return re.sub(r"\d+(?:[.,:/]\d+)*", lambda m: m.group()[::-1], token[::-1])


def visual_to_logical(line: str) -> str:
    """The logical order of a visually ordered right-to-left line (see above). Pure; no detection."""
    tokens = line.split()
    kinds = ["H" if HEBREW.search(t) else "L" if LATIN.search(t) else "N" for t in tokens]
    for i, kind in enumerate(kinds):
        if kind == "N":
            left = next((k for k in reversed(kinds[:i]) if k != "N"), None)
            right = next((k for k in kinds[i + 1:] if k != "N"), None)
            if left == right and left is not None:
                kinds[i] = left.lower()                  # a number inside a Hebrew / Latin run belongs to it
    runs: list[tuple[str, list[str]]] = []
    for token, kind in zip(tokens, kinds):
        base = kind.upper()
        if runs and base in ("H", "L") and runs[-1][0] == base:
            runs[-1][1].append(token)
        else:
            runs.append((base, [token]))
    out = []
    for base, items in reversed(runs):
        out.append(" ".join(_backwards(t) for t in reversed(items)) if base == "H" else " ".join(items))
    return " ".join(out)


def logical_rtl_line(line: str, words: frozenset) -> str | None:
    """The logical form of a line detected as visually ordered right-to-left text, else None."""
    if not HEBREW.search(line or ""):
        return None
    score = rtl_reversal_score(line, words)
    if score["reversed"] <= score["as_read"]:
        return None
    return visual_to_logical(line)


def looks_reversed(line: str, hebrew_aliases: Iterable[str] = ()) -> bool:
    """Deterministic detection: a Hebrew word may not START with a final letter form; or a known Hebrew
    label appears only after reversal."""
    words = [w.strip("\"'.,:;()[]-") for w in line.split()]
    hebrew = [w for w in words if w and HEBREW.match(w)]
    if hebrew and any(w[0] in FINAL_LETTERS for w in hebrew) and not any(w[-1] in FINAL_LETTERS for w in hebrew):
        return True
    norm, rev = normalize_text(line), normalize_text(reverse_hebrew_line(line))
    return any(a in rev and a not in norm for a in hebrew_aliases)


# --- numbers and units --------------------------------------------------------------------------------

NUMBER = re.compile(r"(?<![\w.,/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)(?!\d)")
RANGE_BEFORE = re.compile(r"(\d+(?:[.,]\d+)?)\s*-\s*$")


def parse_number(raw: str) -> float | None:
    text = raw.strip()
    try:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            return float(text.replace(",", ""))
        if re.fullmatch(r"\d+,\d{1,2}", text):   # decimal comma (a thousands group always has 3 digits)
            return float(text.replace(",", "."))
        if re.fullmatch(r"\d+,\d{3}", text):
            return float(text.replace(",", ""))
        return float(text)
    except ValueError:
        return None


def _num_value(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 4)


OPERATIONS: dict[str, Callable[[float], float]] = {
    "identity": lambda v: v,
    # kgf·m (קג"מ) -> Nm, to whole Nm as torque is published (32.6 -> 320, 33 -> 324)
    "multiply_by_9_80665": lambda v: float(round(v * 9.80665)),
    "divide_by_10": lambda v: v / 10,
    "multiply_by_10": lambda v: v * 10,
    "divide_by_1000": lambda v: v / 1000,
    "multiply_by_1000": lambda v: v * 1000,
    # D3: km/l -> l/100km = 100 / x, one decimal
    "reciprocal_times_100": lambda v: round(100 / v, 1) if v else v,
    # D4: exact imperial factors (never across test cycles: no mpg -> l/100km, no 0-60 mph -> 0-100 km/h)
    "multiply_by_1_35582": lambda v: float(round(v * 1.35582)),          # lb-ft -> Nm
    "multiply_by_25_4": lambda v: float(round(v * 25.4)),                # in -> mm
    "multiply_by_3_785411784": lambda v: round(v * 3.785411784, 1),     # US gal -> l
    "multiply_by_28_316846592": lambda v: float(round(v * 28.316846592)),  # ft3 -> l
    "multiply_by_0_45359237": lambda v: float(round(v * 0.45359237)),    # lb -> kg
}
# D1: the exact factor of each conversion (the rounding tolerance of a converted value is the source unit's last stated
# digit times this factor; src/conflict_normalizer.py)
OPERATION_FACTORS: dict[str, float] = {
    "identity": 1.0, "multiply_by_9_80665": 9.80665, "divide_by_10": 0.1, "multiply_by_10": 10.0,
    "divide_by_1000": 0.001, "multiply_by_1000": 1000.0, "multiply_by_1_35582": 1.35582, "multiply_by_25_4": 25.4,
    "multiply_by_3_785411784": 3.785411784, "multiply_by_28_316846592": 28.316846592,
    "multiply_by_0_45359237": 0.45359237,
}
GENERIC_UNITS = ["hp", "ps", "bhp", 'כ"ס', "כוח סוס", "rpm", "סל\"ד", "mph", "lb-ft", "cc", 'סמ"ק', "volt", "v",
                 "mpg", "mpge", "miles", "mi", "seats", "מושבים", "doors", "דלתות", "cylinders", "צילינדרים",
                 # other quantities a number may carry (never a litre / km / kWh value): imperial volume, energy per km
                 "cu ft", "cu-ft", "cu. ft", "cu.ft", "cu.ft.", "cuft", "ft3", "ft³", "cubic feet", "cubic ft", "wh/km",
                 "wh/mi", "wh"]


def _construct(word: str) -> str:
    """A Hebrew noun with its construct / plural-construct form (אגרה/אגרת, דרגה/דרגת, מחיר/מחירי)."""
    if len(word) < 3 or not HEBREW.fullmatch(word[-1]):
        return re.escape(word)
    if word.endswith("ה"):
        return re.escape(word[:-1]) + "[הת]"
    if word[-1] in FINAL_LETTERS or word.endswith(("ת", "י")):
        return re.escape(word)
    return re.escape(word) + "י?"


def _term_regex(term: str, construct: bool = False) -> str:
    """Regex for one alias/term: spaces and hyphens interchangeable; word boundaries that also work for
    Hebrew (with up to two attached prefix letters: ו ה ב ל מ ש כ). `construct`: a Hebrew last word also matches its
    construct form (exclusion terms: "אגרה" covers "אגרת רישוי")."""
    norm = normalize_term(term)
    words = [p for p in re.split(r"[\s\-]+", norm) if p]
    parts = [re.escape(p) for p in words]
    if construct and words and HEBREW.match(words[-1]):
        parts[-1] = _construct(words[-1])
    if not parts:
        return r"(?!x)x"
    body = r"[\s\-]*".join(parts)
    lead = r"(?<![\w])"
    if HEBREW.match(norm):
        lead = r"(?<![\w])(?:[ובהלמשכ]{1,2}-?)?"
    elif norm[0].isdigit():
        lead = r"(?<![\w.])"
    trail = r"(?![\w])" if re.search(r"\w$", norm) else ""
    return lead + body + trail


def compile_terms(terms: Iterable[str], construct: bool = False) -> re.Pattern | None:
    terms = sorted({t for t in terms if t and t.strip()}, key=lambda t: -len(t))
    if not terms:
        return None
    return re.compile("|".join(f"(?:{_term_regex(t, construct)})" for t in terms))


def _contains(pattern: re.Pattern | None, text: str) -> bool:
    return bool(pattern and pattern.search(text))


# --- compiled field rules --------------------------------------------------------------------------

@dataclass
class FieldRule:
    name: str
    matcher: str
    component: str | None
    aliases: list[tuple[str, re.Pattern, bool]]       # (original alias, regex, is_abbreviation)
    alias_any: re.Pattern | None
    units: dict[str, str]                              # normalized variant -> normalized unit
    conversions: list[tuple[set, str, str]]            # (variants, operation, to_unit)
    normalized_unit: str | None
    plausible: tuple[float | None, float | None]
    positive: re.Pattern | None
    negative: re.Pattern | None
    require_context: bool
    exclusions: list[tuple[re.Pattern, re.Pattern | None, str]]
    ambiguities: list[tuple[re.Pattern, str, str, str]]
    patterns: list[tuple[re.Pattern, Any, float, str]]
    enum: list[tuple[str, re.Pattern]]
    warranty_kind: str | None
    value_type: str
    spec: dict = field(repr=False, default_factory=dict)


def _compile_rule(spec: dict) -> FieldRule | None:
    aliases_raw = [(a, False) for a in (spec.get("aliases_he") or []) + (spec.get("aliases_en") or [])]
    aliases_raw += [(a, True) for a in spec.get("abbreviations") or []]
    matcher = spec.get("matcher") or ("numeric" if spec.get("expected_units") else None)
    if not matcher or not (aliases_raw or spec.get("patterns") or spec.get("enum_values")):
        return None
    units = {normalize_term(u): spec.get("normalized_unit") or u
             for u in (spec.get("accepted_unit_variants") or []) + (spec.get("expected_units") or [])}
    conversions = []
    for rule in spec.get("conversion_rules") or []:
        variants = {normalize_term(v) for v in [rule.get("from_unit", "")] + list(rule.get("from_unit_variants") or [])
                    if v}
        if rule.get("operation") in OPERATIONS:
            conversions.append((variants, rule["operation"], rule.get("to_unit") or spec.get("normalized_unit")))
    exclusions = []
    for rule in spec.get("exclusion_rules") or []:
        pattern = compile_terms(rule.get("terms") or [])
        if pattern:
            exclusions.append((pattern, compile_terms(rule.get("unless") or []), rule.get("reason") or "excluded"))
    ambiguities = []
    for rule in spec.get("ambiguity_rules") or []:
        pattern = compile_terms(rule.get("terms") or [])
        if pattern:
            ambiguities.append((pattern, rule.get("hint_key") or "ambiguity", str(rule.get("hint_value") or True),
                                rule.get("note") or ""))
    patterns = []
    for item in spec.get("patterns") or []:
        try:
            patterns.append((re.compile(item["regex"]), item.get("value"), float(item.get("confidence") or 0.8),
                             item.get("note") or ""))
        except (re.error, KeyError, TypeError, ValueError):
            continue
    enum = [(str(e.get("value")), compile_terms([e.get("value", "")] + list(e.get("variants") or [])))
            for e in spec.get("enum_values") or [] if isinstance(e, dict) and e.get("value")]
    return FieldRule(
        name=spec["name"], matcher=matcher, component=spec.get("component"),
        aliases=[(a, re.compile(_term_regex(a)), abbr) for a, abbr in aliases_raw],
        alias_any=compile_terms(a for a, _ in aliases_raw),
        units=units, conversions=conversions, normalized_unit=spec.get("normalized_unit"),
        plausible=(spec.get("plausible_min"), spec.get("plausible_max")),
        positive=compile_terms((spec.get("positive_context_terms_he") or []) + (spec.get("positive_context_terms_en") or [])),
        negative=compile_terms((spec.get("negative_context_terms_he") or []) + (spec.get("negative_context_terms_en") or [])),
        require_context=bool(spec.get("require_context")),
        exclusions=exclusions, ambiguities=ambiguities, patterns=patterns,
        enum=[(v, p) for v, p in enum if p], warranty_kind=spec.get("warranty_kind"),
        value_type=spec.get("value_type") or "text", spec=spec)


def dictionary_part(spec: dict) -> dict:
    return {k: spec.get(k) for k in DICTIONARY_KEYS if spec.get(k) not in (None, [], {})}


def schema_hash(specs: Iterable[dict], vocabulary: dict | None = None) -> str:
    """Identity of a harvest: harvester version + shared vocabulary + every field's dictionary. A cached
    harvest is reused only for exactly the same dictionary (applicability is not part of it)."""
    parts = sorted((normalize_field_name(s.get("name")), dictionary_part(s)) for s in specs if s.get("name"))
    blob = json.dumps([HARVESTER_VERSION, vocabulary if vocabulary is not None else harvest_vocabulary(), parts],
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


class Dictionary:
    """All compiled rules + shared vocabularies for one schema hash."""

    def __init__(self, specs: Iterable[dict], vocabulary: dict | None = None):
        specs = [s for s in specs if s.get("name")]
        self.vocabulary = vocabulary if vocabulary is not None else harvest_vocabulary()
        self.hash = schema_hash(specs, self.vocabulary)
        self.rules = [r for r in (_compile_rule({**s, "name": normalize_field_name(s["name"])}) for s in specs) if r]
        v = self.vocabulary
        self.affirmative = {normalize_term(x) for x in v.get("affirmative_values") or []}
        self.negative_values = {normalize_term(x) for x in v.get("negative_values") or []}
        self.optional_values = {normalize_term(x) for x in v.get("optional_values") or []}
        self.optional_terms = compile_terms(x for x in v.get("optional_values") or [] if len(x) > 2)
        self.negative_terms = compile_terms(x for x in v.get("negative_values") or [] if len(x) > 2)
        self.feature_labels = compile_terms(a for r in self.rules if r.matcher == "boolean" for a, _, _ in r.aliases)
        negations = compile_terms(v.get("negation_prefixes") or [])
        self.negation_prefix = re.compile(rf"(?:{negations.pattern})\s*$") if negations else None
        self.equipment = compile_terms(v.get("equipment_context_terms") or [])
        self.front = compile_terms(v.get("front_terms") or [])
        self.rear = compile_terms(v.get("rear_terms") or [])
        self.both = compile_terms(v.get("both_axles_terms") or [])
        self.alternative = compile_terms(v.get("alternative_terms") or [])
        self.tire_terms = compile_terms(v.get("tire_terms") or [])
        self.warranty_terms = compile_terms(v.get("warranty_terms") or [])
        self.warranty_battery = compile_terms(v.get("warranty_battery_terms") or [])
        self.warranty_vehicle = compile_terms(v.get("warranty_vehicle_terms") or [])
        self.warranty_excluded = compile_terms(v.get("warranty_excluded_terms") or [])
        self.unlimited_km = compile_terms(v.get("warranty_unlimited_km_terms") or [])
        self.year_units = compile_terms(v.get("year_units") or [])
        self.km_units = [normalize_term(u) for u in v.get("km_units") or []]
        self.minute_units = [normalize_term(u) for u in v.get("time_minute_units") or []]
        self.hour_units = [normalize_term(u) for u in v.get("time_hour_units") or []]
        self.charging = compile_terms(v.get("charging_terms") or [])
        self.wheel_context = compile_terms(v.get("wheel_context_terms") or [])
        self.trim_header = compile_terms(v.get("trim_header_terms") or [])
        # identity rows of a multi-variant table (power, drivetrain, battery, ...): their cells name a column's variant
        self.column_identity_rows = compile_terms(v.get("column_identity_row_terms") or [])
        # any field's alias (an isolated structural pair needs one in its label: src/structure_harvest.py)
        self.any_alias = compile_terms(a for r in self.rules for a, _, _ in r.aliases)
        self.currencies = {normalize_term(var): code for code, variants in (v.get("currencies") or {}).items()
                           for var in variants}
        self.hebrew_aliases = sorted({normalize_term(a) for r in self.rules for a, _, _ in r.aliases
                                      if HEBREW.match(normalize_term(a)) and len(a) >= 4})
        gearbox = next((r for r in self.rules if r.matcher == "gearbox" and r.enum), None)
        self.gearbox_types = gearbox.enum if gearbox else []
        # PR #45 (R6): which gearbox types a wording of another type overrides ("רובוטית כפולת מצמדים" is dct)
        self.gearbox_outranks = {str(e.get("value")): set(e.get("outranks") or [])
                                 for e in (gearbox.spec.get("enum_values") or [] if gearbox else [])
                                 if isinstance(e, dict) and e.get("value")}
        units: set[str] = set(self.currencies) | {"%"} | set(self.km_units) | set(self.minute_units) \
            | set(self.hour_units) | {normalize_term(u) for u in v.get("year_units") or []} \
            | {normalize_term(u) for u in GENERIC_UNITS}
        for rule in self.rules:
            units |= set(rule.units)
            for variants, _, _ in rule.conversions:
                units |= variants
        units.discard("")
        ordered = sorted(units, key=lambda u: -len(u))
        self.unit_after = re.compile(r"\s*-?\s*(" + "|".join(re.escape(u) for u in ordered)
                                     + r")(?![a-zא-ת])")
        self.unit_before = re.compile(r"(" + "|".join(re.escape(u) for u in sorted(self.currencies, key=lambda u: -len(u)))
                                      + r")\s?$") if self.currencies else None


# --- segments --------------------------------------------------------------------------------------

UNIT_LINE = re.compile(r"\(\s*[^()\d]{1,25}\s*\)")
BLOCK_LABEL_MAX, BLOCK_VALUE_MAX, BLOCK_DESCRIPTION_MIN, BLOCK_LOOKAHEAD = 40, 40, 40, 5


def spec_block_segments(norms: list[tuple[str, str, bool]], descriptions: set[int] | None = None) -> list["Segment"]:
    """Spec rows written as "label / description / value" blocks (PR #44; Israeli publisher pages put a tooltip between
    a row's label and its value): a short label line, then within BLOCK_LOOKAHEAD lines an optional unit line ("(ס"מ)"),
    an optional repetition of the label (the tooltip's title) and at least one description line (>= 40 characters),
    then a short value line. Read as a structured "label (unit) | value" segment (method spec_block); a label whose value
    is on the next line is the line harvest's (label_next_line) and a DOM pair's, never a block."""
    out: list[Segment] = []
    for i, (norm, quote, _) in enumerate(norms):
        if not (2 <= len(norm) <= BLOCK_LABEL_MAX) or not re.search(r"[a-zא-ת]", norm) or UNIT_LINE.fullmatch(norm):
            continue
        if any(norms[k][0] == norm for k in range(max(0, i - 2), i)):
            continue                                  # the tooltip's repetition of the label above, not a label
        unit, described, j = None, False, i + 1
        prose: list[int] = []
        while j < len(norms) and j <= i + BLOCK_LOOKAHEAD:
            nxt = norms[j][0]
            if UNIT_LINE.fullmatch(nxt) and unit is None and not described:
                unit = norms[j][1]
            elif nxt == norm and not described:
                pass
            elif len(nxt) >= BLOCK_DESCRIPTION_MIN:
                described = True
                prose.append(j)
            else:
                break
            j += 1
        if not described or j >= len(norms):
            continue
        value_norm, value_quote = norms[j][0], norms[j][1]
        if len(value_norm) > BLOCK_VALUE_MAX or UNIT_LINE.fullmatch(value_norm) or value_norm == norm:
            continue
        if descriptions is not None:
            descriptions.update(prose)
        label = f"{quote} {unit}" if unit else quote
        out.append(Segment("block", normalize_text(f"{label} : {value_quote}"), f"{label} | {value_quote}",
                           label=normalize_text(label), value=value_norm, raw_value=value_quote))
    return out


@dataclass
class Segment:
    kind: str                 # line | row | leaf | pair (a structural DOM label/value pair) | block (PR #44)
    text: str                 # text matched (normalized)
    quote: str                # original text for the quote
    label: str = ""           # rows/leaves: normalized label
    value: str = ""           # rows/leaves: normalized value cell
    raw_value: str = ""
    table_index: int | None = None
    row_index: int | None = None
    page: int | None = None
    header: str | None = None
    column_identity: str | None = None   # rows / pairs: the header + identity cells of the value's own column
    multi_column: bool = False           # rows: one of several value cells of a multi-variant table row
    reversed: bool = False
    next_text: str = ""       # lines: the following line (label-on-one-line documents)
    next_quote: str = ""


def _leaves(value: Any, path: str = "", out: list | None = None) -> list[tuple[str, str]]:
    out = out if out is not None else []
    if len(out) >= MAX_STRUCTURED_LEAVES:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            _leaves(item, str(key), out)
    elif isinstance(value, list):
        for item in value[:200]:
            _leaves(item, path, out)
    elif value not in (None, "") and not isinstance(value, bool) and path and not path.startswith("@"):
        text = str(value)
        if len(text) <= 200:
            out.append((path, text))
    return out


def _label(key: str) -> str:
    return normalize_term(re.sub(r"([a-z])([A-Z])", r"\1 \2", key).replace("_", " "))


COLUMN_IDENTITY_CHARS = 300


def table_header(rows: list[list], trim_header) -> list | None:
    """A table's header row: at most one numeric cell, or a first cell that names the column dimension (version /
    trim), so variant columns such as "1.8 Hybrid 140 | 2.0 Hybrid 196" keep their names as variant hints."""
    return rows[0] if rows and len(rows[0]) > 2 and (
        sum(1 for c in rows[0] if NUMBER.search(str(c))) <= 1
        or _contains(trim_header, normalize_text(str(rows[0][0] or "")))) else None


def column_identities(rows: list[list], row_terms, *, header: bool) -> list[str | None]:
    """Per column of a multi-variant table (>= 2 value columns), the text that says WHICH variant the column is: its
    header cell plus its cells in identity rows (power, drivetrain, battery, motor, body, model year, trim), e.g.
    "MAX | הספק 486 כ"ס | הנעה כפולה". Only full-width rows count (a short row's cells may sit in other columns).
    Index 0 (the label column) and columns with nothing to say are None."""
    width = max((len(r) for r in rows), default=0)
    if width < 3 or row_terms is None:
        return []
    texts: list[list[str]] = [[] for _ in range(width)]
    if header and rows and len(rows[0]) == width:
        for j in range(1, width):
            if str(rows[0][j] or "").strip():
                texts[j].append(str(rows[0][j]).strip())
    for row in rows[1 if header else 0:]:
        label = str(row[0] or "").strip() if row else ""
        if len(row) != width or not label or len(label) > 60 or not row_terms.search(normalize_text(label)):
            continue
        for j in range(1, width):
            cell = str(row[j] or "").strip()
            if cell:
                texts[j].append(f"{label} {cell}")
    return [None] + [" | ".join(t)[:COLUMN_IDENTITY_CHARS] or None for t in texts[1:]]


def document_segments(text: str, tables: list[dict] | None, structured: dict | None, *, is_pdf: bool,
                      dictionary: Dictionary) -> list[Segment]:
    """Every searchable unit of one document, built ONCE and then read by all field matchers."""
    segments: list[Segment] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    norms: list[tuple[str, str, bool]] = []
    words = rtl_dictionary(dictionary.hebrew_aliases) if is_pdf else frozenset()
    for line in lines:
        logical = logical_rtl_line(line, words) if is_pdf else None
        if logical is None and is_pdf and HEBREW.search(line) and looks_reversed(line, dictionary.hebrew_aliases):
            logical = reverse_hebrew_line(line)
        reversed_ = logical is not None
        norms.append((normalize_text(logical if reversed_ else line), logical if reversed_ else line, reversed_))
    descriptions: set[int] = set()
    blocks = [] if is_pdf else spec_block_segments(norms, descriptions)
    for i, (norm, quote, reversed_) in enumerate(norms):
        nxt = norms[i + 1] if i + 1 < len(norms) and i not in descriptions else ("", "", False)
        # a block's description is prose: the value below it is the block's, never the prose's (PR #44)
        segments.append(Segment("line", norm, quote, reversed=reversed_, next_text=nxt[0], next_quote=nxt[1]))
    segments += blocks
    for t_index, table in enumerate(tables or []):
        rows = table.get("rows") or []
        header = table_header(rows, dictionary.trim_header)
        # computed once per table (src/tools/extract stores it with newly extracted tables)
        idents = table.get("column_identity") or column_identities(rows, dictionary.column_identity_rows,
                                                                   header=header is not None)
        width = max((len(r) for r in rows), default=0)
        for r_index, row in enumerate(rows):
            cells = [str(c or "").strip() for c in row]
            if len(cells) < 2 or not cells[0]:
                continue
            values = [(j, c) for j, c in enumerate(cells[1:], start=1) if c]
            for j, cell in values:
                head = header[j] if header and j < len(header) and header is not row else None
                ident = idents[j] if idents and len(values) > 1 and len(cells) == width and j < len(idents) \
                    and header is not row else None
                segments.append(Segment("row", normalize_text(f"{cells[0]} : {cell}"),
                                        f"{cells[0]} | {cell}" + (f" ({head})" if head and len(values) > 1 else ""),
                                        label=normalize_text(cells[0]), value=normalize_text(cell), raw_value=cell,
                                        table_index=t_index, row_index=r_index, page=table.get("page"),
                                        header=head if head and len(values) > 1 else None, column_identity=ident,
                                        multi_column=bool(idents) and len(values) > 1))
    for key, value in _leaves(_structured_roots(structured)):
        label = _label(key)
        segments.append(Segment("leaf", normalize_text(f"{label} : {value}"), f"{key}: {value}", label=label,
                                value=normalize_text(value), raw_value=value))
    return segments


def _structured_roots(structured: dict | None) -> list:
    if not structured:
        return []
    keep = ("json_ld", "next_data", "application_json", "window_state", "microdata")
    roots: list = [structured.get(k) for k in keep if structured.get(k)]
    roots += [{item.get("itemprop"): item.get("value")} for item in structured.get("microdata") or []
              if isinstance(item, dict)]
    return roots


_REJECTIONS: ContextVar[list | None] = ContextVar("candidate_rejections", default=None)


@contextmanager
def collect_candidate_rejections():
    rows = []
    token = _REJECTIONS.set(rows)
    try:
        yield rows
    finally:
        _REJECTIONS.reset(token)


def reject_candidate(rule, text, value, method, reason, **extra):
    sink = _REJECTIONS.get()
    if sink is not None:
        row = {"field": rule.name, "value": value, "quote": text[:QUOTE_CHARS],
               "extraction_method": method, "rejection": reason, **extra}
        if row not in sink:
            sink.append(row)


def semantic_reason(spec: dict, text: str, value: Any = None) -> str | None:
    """One field's exclusions, attached to its own numeric statement, shared by all candidate origins and admission.

    With several values on a line, an exclusion belongs to the nearest value. A following label before another
    number names that other number; a qualifier after the last number still qualifies that last number.
    """
    text = normalize_text(text)
    nums = list(NUMBER.finditer(text))
    wanted = parse_number(str(value)) if isinstance(value, (int, float, str)) else None
    own = [n for n in nums if wanted is not None and parse_number(n.group()) == wanted]
    # numbers joined by "x" / "/" ("1100 x 1200 x 800") are ONE statement: a qualifier of one is a qualifier of all
    group, gid = {}, 0
    for i, n in enumerate(nums):
        if i and not re.fullmatch(r"\s*(?:[a-zא-ת\"']{0,4}\s*)?[x/]\s*", text[nums[i - 1].end():n.start()]):
            gid += 1
        group[n.start()] = gid
    own_groups = {group[n.start()] for n in own}
    for exclusion in spec.get("semantic_exclusions") or []:
        applies = exclusion.get("applies_to")
        if applies and spec.get("_sanity_propulsion") not in applies:
            continue
        pattern = compile_terms(exclusion.get("terms") or [], construct=True)
        for hit in pattern.finditer(text) if pattern else []:
            if own and len(nums) > 1 and not re.search(r"\d", hit.group()):
                nearest = min(nums, key=lambda n: max(0, hit.start()-n.end(), n.start()-hit.end()))
                if nearest not in own and group[nearest.start()] not in own_groups:
                    continue
            return exclusion.get("reason") or "semantic exclusion"
    if own and wanted is not None and float(wanted).is_integer() and 1990 <= wanted <= 2039 \
            and spec.get("expected_units") and spec.get("matcher") in ("numeric", None):
        # a bare 4-digit year ("XPeng G6 2026") in a statement that states no unit of the field anywhere
        units = [normalize_term(u) for u in (spec.get("accepted_unit_variants") or [])
                 + (spec.get("expected_units") or []) if u]
        if all(re.fullmatch(r"\d{4}", n.group()) for n in own) and not compile_terms(units).search(text):
            return "a model year, not a value of this field"
    if own and spec.get("unit_required") == "always":
        # the field's unit must be stated: after the value, or in its label / column header within this text
        units = sorted({normalize_term(u) for u in (spec.get("accepted_unit_variants") or [])
                        + (spec.get("expected_units") or []) if u}, key=len, reverse=True)
        if units and not compile_terms(units).search(text):
            return "no unit of this field stated with the value"
    for exclusion in spec.get("value_exclusion_patterns") or []:
        if re.search(exclusion["regex"], normalize_text(str(value if value is not None else text))):
            return exclusion.get("reason") or "invalid text value"
    if own and spec.get("rpm_guard") and all(rpm_number(spec, text, n.start(), n.end()) for n in own):
        return RPM_REASON
    return None


# --- engine speeds (PR #44) ------------------------------------------------------------------------------------------

RPM_REASON = "an engine speed (rpm), not this field's value"
_RPM: dict[str, Any] = {}


def rpm_terms() -> re.Pattern | None:
    """The harvest vocabulary's rpm_terms (rpm, סל"ד, min⁻¹, ...), compiled once per vocabulary version."""
    terms = harvest_vocabulary().get("rpm_terms") or []
    key = json.dumps(terms, ensure_ascii=False)
    if key not in _RPM:
        _RPM.clear()
        _RPM[key] = compile_terms(terms)
    return _RPM[key]


def _field_units(spec: dict) -> re.Pattern | None:
    """Every unit variant of a field, its conversions' too ("Nm", "נ"מ", "kgf·m", "קג"מ")."""
    units = list(spec.get("accepted_unit_variants") or []) + list(spec.get("expected_units") or [])
    for rule in spec.get("conversion_rules") or []:
        units += [rule.get("from_unit") or ""] + list(rule.get("from_unit_variants") or [])
    return compile_terms(u for u in units if u and len(u) > 1)


RANGE_TAIL = re.compile(r"\s*-\s*\d[\d,.]*")


def rpm_number(spec: dict, text: str, start: int, end: int) -> bool:
    """Is the number text[start:end] (normalized text) an ENGINE SPEED for a field with `rpm_guard`? Yes when an rpm
    term follows it (also through a range: "1,500-4,100 rpm", "ב-1,500 סל"ד"), or when its row label (the line up to
    its first number; a "label | value" cell pair) names an rpm term and either names no unit of the field or the
    number is part of a range there ("מומנט מירבי (סל"ד/ קג"מ) 1,500-3,500 / 25.5": the range is the rpm, 25.5 the
    torque). A number with the field's own unit right after it is never an engine speed."""
    rpm = rpm_terms()
    if rpm is None:
        return False
    units = _field_units(spec)
    tail = text[end:end + 40]
    ranged = RANGE_TAIL.match(tail)
    after = tail[ranged.end():] if ranged else tail
    stripped = after.lstrip(" -")
    if units is not None and units.match(stripped) and not ranged:
        return False
    if (m := rpm.search(stripped)) and m.start() == 0:
        return True
    line_start = text.rfind("\n", 0, start) + 1
    first = NUMBER.search(text, line_start)
    label = text[line_start:first.start() if first else start]
    if not rpm.search(label):
        return False
    in_range = bool(ranged) or bool(RANGE_BEFORE.search(text[max(line_start, start - 12):start]))
    return in_range or units is None or not units.search(label)


def dimension_assignment(rule: FieldRule, d: Dictionary, text: str) -> dict | None:
    """Ordered composite label/value triples, driven by sibling_group and axis_token. Each number has one owner."""
    group = rule.spec.get("sibling_group")
    axes = [r for r in d.rules if group and r.spec.get("sibling_group") == group and r.spec.get("axis_token")]
    if len(axes) < 3:
        return None
    labels = []
    for r in axes:
        terms = [pat.pattern for _, pat, _ in r.aliases] + [_term_regex(r.spec["axis_token"])]
        for hit in re.finditer("|".join(f"(?:{t})" for t in terms), text):
            labels.append((hit.start(), hit.end(), r.name))
    # "L×W×H" is normalized to the single word "lxwxh": its axis letters are labels too (word boundaries above never
    # split it)
    tokens = {normalize_term(r.spec["axis_token"]): r.name for r in axes}
    for hit in re.finditer(r"(?<![\w])([a-z])x([a-z])x([a-z])(?![\w])", text):
        if all(hit.group(j) in tokens for j in (1, 2, 3)):
            labels += [(hit.start(j), hit.end(j), tokens[hit.group(j)]) for j in (1, 2, 3)]
    labels.sort(key=lambda h: (h[0], -(h[1]-h[0])))
    kept = []
    for label in labels:
        if not kept or label[0] >= kept[-1][1]:
            kept.append(label)
    for i in range(len(kept)-2):
        triple = kept[i:i+3]
        if len({x[2] for x in triple}) != 3:
            continue
        if not all(re.fullmatch(r"\s*(?:x|/)\s*", text[a[1]:b[0]]) for a,b in zip(triple,triple[1:])):
            continue
        tail = text[triple[-1][1]:]
        # the values follow the labels: after a unit in brackets, a colon, or the cell separator of a label | value
        # pair ("אורך x רוחב x גובה | 4,758 / 1,920 / 1,650 מ"מ")
        m = re.match(r"\s*\)?\s*(?:\([^)]{1,12}\))?\s*[:=|]?\s*(?:\([^)]{1,12}\))?\s*(\d[\d.,]*)\s*(?:x|/)\s*(\d[\d.,]*)\s*(?:x|/)\s*(\d[\d.,]*)", tail)
        if m:
            return {name: (parse_number(m.group(j+1)), (triple[-1][1]+m.start(j+1), triple[-1][1]+m.end(j+1)))
                    for j,(_,_,name) in enumerate(triple)}
    return None


def owns_dimension_number(rule: FieldRule, d: Dictionary, text: str, start: int, end: int) -> bool:
    group = rule.spec.get("sibling_group")
    if not group:
        return True
    composite = dimension_assignment(rule, d, text)
    if composite:
        return rule.name in composite and composite[rule.name][1] == (start, end)
    labels = [(r.name, a,b) for r in d.rules if r.spec.get("sibling_group") == group
              for a,b,_,_ in _alias_hits(r,text)]
    labels = [h for h in labels if not any(o != h and o[1] <= h[1] and h[2] <= o[2]
                                           and o[2]-o[1] > h[2]-h[1] for o in labels)]
    if not labels:
        return True
    prior = [h for h in labels if h[2] <= start]
    # A closer following label supports value-before-label prose. Ties prefer preceding labels.
    best = min(labels, key=lambda h: (max(0,start-h[2],h[1]-end), h[1] >= end))
    if prior and not re.search(r"\d", text[max(prior,key=lambda h:h[2])[2]:start]):
        best = max(prior,key=lambda h:h[2])
    return best[0] == rule.name


# --- matching helpers ----------------------------------------------------------------------------------

@dataclass
class Hit:
    value: Any
    raw_value: str
    unit: str | None = None
    raw_unit: str | None = None
    confidence: float = 0.8
    span: tuple[int, int] = (0, 0)
    alias: str | None = None
    hints: dict = field(default_factory=dict)


def _clause_bounds(text: str, pos: int) -> tuple[int, int]:
    """Sentence/clause containing pos: never crosses '. ', ';' or a bullet."""
    start = 0
    for m in re.finditer(r"(?:\.\s(?!\d)|;|•|\s\|\s)", text[:pos]):
        start = m.end()
    end_match = re.search(r"(?:\.\s(?!\d)|;|•|\s\|\s)", text[pos:])
    return start, pos + end_match.start() if end_match else len(text)


def _window(text: str, a: int, b: int, pad: int, bounds: tuple[int, int]) -> str:
    return text[max(bounds[0], a - pad):min(bounds[1], b + pad)]


def _context_ok(rule: FieldRule, near: str, wide: str, *, full_text: str | None = None, value: Any = None) -> tuple[bool, float, dict]:
    """Exclusion / negative / positive / ambiguity rules. Returns (keep, confidence delta, hints)."""
    hints: dict = {}
    delta = 0.0
    for pattern, unless, reason in rule.exclusions:
        if pattern.search(near) and not (unless and unless.search(wide)):
            if full_text is not None:
                scoped = {"semantic_exclusions": [{"terms": r.get("terms") or [], "reason": r.get("reason")}
                                                  for r in rule.spec.get("exclusion_rules") or []]}
                if semantic_reason(scoped, full_text, value) is None:
                    continue
            return False, 0.0, {"rejected": reason}
    positive = _contains(rule.positive, wide)
    if rule.require_context and not positive:
        return False, 0.0, {"rejected": "required context missing"}
    if _contains(rule.negative, near):
        if not positive:
            return False, 0.0, {"rejected": "negative context"}
        delta -= 0.2
        hints["mixed_context"] = True
    elif positive:
        delta += 0.03
    for pattern, key, value, note in rule.ambiguities:
        if pattern.search(wide):
            hints[key] = value
            if note:
                hints["ambiguity_note"] = note
            delta -= 0.15
    return True, delta, hints


def _plausible(rule: FieldRule, value: float) -> bool:
    low, high = rule.plausible
    step = rule.spec.get("allowed_step")
    return ((low is None or value >= float(low)) and (high is None or value <= float(high))
            and (not step or abs(value/step-round(value/step)) < 1e-7))


def _unit_at(d: Dictionary, text: str, end: int) -> tuple[str | None, int]:
    m = d.unit_after.match(text, end)
    return (m.group(1), m.end()) if m else (None, end)


def _classify_unit(rule: FieldRule, unit: str | None) -> tuple[str, str | None, str | None]:
    """('ok' | 'convert' | 'none' | 'other', normalized unit, operation)."""
    if unit is None:
        return "none", None, None
    if unit in rule.units:
        return "ok", rule.units[unit], None
    for variants, operation, to_unit in rule.conversions:
        if unit in variants:
            return "convert", to_unit, operation
    return "other", None, None


def _label_unit(rule: FieldRule, label: str) -> str | None:
    for variant, unit in rule.units.items():
        if len(variant) > 1 and re.search(rf"(?<![\w]){re.escape(variant)}(?![\w])", label):
            return unit
    return None


def _label_conversion(rule: FieldRule, label: str) -> tuple[str, str] | None:
    """(to unit, operation) when the label names a unit the field converts from ("מומנט מרבי (קג"מ)")."""
    for variants, operation, to_unit in rule.conversions:
        for variant in sorted(variants, key=len, reverse=True):
            if len(variant) > 1 and re.search(rf"(?<![\wא-ת]){re.escape(variant)}(?![\wא-ת])", label):
                return to_unit or rule.normalized_unit, operation
    return None


def _alias_hits(rule: FieldRule, text: str) -> list[tuple[int, int, str, bool]]:
    hits = []
    for alias, pattern, abbr in rule.aliases:
        for m in pattern.finditer(text):
            hits.append((m.start(), m.end(), alias, abbr))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, str, bool]] = []
    for h in hits:  # longest alias per position, no overlaps
        if not kept or h[0] >= kept[-1][1]:
            kept.append(h)
        elif h[1] - h[0] > kept[-1][1] - kept[-1][0] and h[0] == kept[-1][0]:
            kept[-1] = h
    return kept


# --- wheel sizes (PR #43) ----------------------------------------------------------------------------

WHEEL_CONTEXT_CHARS = 30
WHEEL_MARK_BEFORE = re.compile(r'(?:"|(?<![\w])r)\s?$')
WHEEL_MARK_AFTER = re.compile(r"\s?(?:\"|''|-?\s?(?:אינץ'?|אינטש|inch(?:es)?|in\.?)(?![a-zא-ת]))")


def takes_inches(rule: "FieldRule | None") -> bool:
    """Does the field accept an inch unit (rim diameter, screen size)? Those fields keep reading wheel-size forms."""
    return bool(rule and any(u in ('"', "''", "in", "inch", "inches") for u in rule.units))


def wheel_size_number(d: "Dictionary", text: str, start: int, end: int) -> bool:
    """Is the number text[start:end] a wheel size: immediately preceded or followed by an inch mark (" / ״ / אינץ' /
    inch / in.) or preceded by R ("R20"), with a wheel / rim term (harvest vocabulary wheel_context_terms) within
    WHEEL_CONTEXT_CHARS on the same line ("עם חישוקי ״20", "20\" alloy wheels")? Such a number is never a range, a
    consumption or any other value of a field that does not take inches."""
    if d.wheel_context is None:
        return False
    if not (WHEEL_MARK_BEFORE.search(text[max(0, start - 2):start]) or WHEEL_MARK_AFTER.match(text, end)):
        return False
    lo = max(text.rfind("\n", 0, start) + 1, start - WHEEL_CONTEXT_CHARS)
    line_end = text.find("\n", end)
    hi = min(len(text) if line_end < 0 else line_end, end + WHEEL_CONTEXT_CHARS)
    return bool(d.wheel_context.search(text[lo:hi]))


# --- matchers ------------------------------------------------------------------------------------------

def _numeric(rule: FieldRule, d: Dictionary, text: str, anchor: tuple[int, int], *, structured: bool,
             label_unit: str | None = None, value_from: int | None = None,
             label_conversion: tuple[str, str] | None = None) -> Hit | None:
    a, b = anchor
    bounds = _clause_bounds(text, a)
    lo = value_from if value_from is not None else max(bounds[0], a - LINE_BACK)
    hi = len(text) if value_from is not None else min(bounds[1], b + LINE_FWD)
    best = None
    for m in NUMBER.finditer(text, lo, hi):
        s, e = m.span()
        if s < b and e > a:
            continue  # part of the alias itself (e.g. "0-100")
        if not owns_dimension_number(rule, d, text, s, e):
            continue
        if not takes_inches(rule) and wheel_size_number(d, text, s, e):
            continue  # "עם חישוקי ״20": a wheel size, never this field's value
        if rule.spec.get("rpm_guard") and rpm_number(rule.spec, text, s, e):
            continue  # "סל"ד מומנט מרבי | 1,500", "320 Nm at 1,500-4,100 rpm": an engine speed (PR #44)
        number = parse_number(m.group(1))
        if number is None:
            continue
        unit, unit_end = _unit_at(d, text, e)
        kind, norm_unit, operation = _classify_unit(rule, unit)
        if kind == "other":
            continue
        if kind == "none" and label_unit:
            kind, norm_unit = "ok", label_unit
        elif kind == "none" and label_conversion:
            kind, (norm_unit, operation) = "convert", label_conversion
        distance = (s - b) if s >= b else (a - e) * 1.5
        rank = 0 if kind in ("ok", "convert") else 1
        if kind == "none" and (rule.units or rule.conversions) and not structured and distance > 25:
            continue
        key = (rank, distance)
        if best is None or key < best[0]:
            best = (key, m, number, unit, norm_unit, operation, kind, unit_end)
    if best is None:
        return None
    _, m, number, unit, norm_unit, operation, kind, unit_end = best
    value = OPERATIONS[operation](number) if operation else number
    if not _plausible(rule, value):
        reject_candidate(rule, text, _num_value(value), "table" if structured else "line", "implausible_value")
        return None
    raw = m.group(1)
    hints: dict = {}
    confidence = 0.95 if structured else 0.85
    if kind == "none":
        confidence = 0.8 if structured else 0.55
    if operation:
        confidence -= 0.05
        hints["converted_from"] = {"value": raw, "unit": unit, "operation": operation}
    before = RANGE_BEFORE.search(text[max(0, m.start() - 12):m.start()])
    if before and parse_number(before.group(1)) is not None:
        raw = f"{before.group(1)}-{raw}"
        hints["range"] = True
        confidence -= 0.2
        value_out: Any = raw if not operation else value
    else:
        value_out = _num_value(value)
    return Hit(value_out, raw, norm_unit or rule.normalized_unit, unit, confidence,
               (min(a, m.start()), max(b, unit_end)), hints=hints)


def _bool_value(d: Dictionary, cell: str) -> tuple[Any, str] | None:
    cell = normalize_term(cell)
    if not cell:
        return None
    if cell in d.negative_values:
        return False, "absent"
    if cell in d.affirmative or cell.startswith(("standard", "סטנדרט", "כן", "yes", "✓", "✔")):
        return True, "standard"                    # the value written first wins ("standard - X not available")
    if _contains(d.negative_terms, cell):          # "not available" is not the optional "available"
        return False, "absent"
    if cell in d.optional_values or _contains(d.optional_terms, cell):
        return True, "optional"
    return None


def _stated_bool(d: Dictionary, rest: str) -> tuple[Any, str] | None:
    """The availability value written right after a feature label ("label: אין", "label | ✓", "label - standard").
    Only a whole vocabulary value counts; a single-letter marker ("s", "v", "o") only as the entire cell."""
    rest = re.split(r"[;•\n]|\.\s", rest, maxsplit=1)[0]
    rest = rest.lstrip(" :|=\t")
    if rest.startswith("- ") and len(rest.strip()) > 1:
        rest = rest[2:]
    cell = re.split(r"\s\|\s|[,()\[\]]", rest, maxsplit=1)[0].strip()
    if not cell:
        return None
    parsed = _bool_value(d, cell)
    if parsed is not None and (len(cell) > 1 or not cell.isalpha() or cell == rest.strip()):
        return parsed
    words = cell.split()
    for size in (3, 2, 1):
        head = " ".join(words[:size])
        if len(words) > size and len(head) > 1:
            term = normalize_term(head)
            if term in d.negative_values:
                return False, "absent"
            if term in d.affirmative and len(term) > 1:
                return True, "standard"
            if term in d.optional_values and len(term) > 1:
                return True, "optional"
    return None


NEGATION_BEFORE = 14


def _boolean_line(rule: FieldRule, d: Dictionary, seg: Segment, anchor: tuple[int, int]) -> Hit | None:
    """A feature in running text / a label line. Order: an explicitly stated value after the label (same line,
    or the next line for a label-only line) > a negation right before the label > the field's negative context >
    an optional term > equipment wording OUTSIDE the label itself. A label alone never implies `true`."""
    text = seg.text
    a, b = anchor
    bounds = _clause_bounds(text, a)
    clause = text[bounds[0]:bounds[1]]
    for pattern, unless, reason in rule.exclusions:
        if pattern.search(_window(text, *anchor, EXCLUSION_PAD, bounds)) and not (unless and unless.search(clause)):
            return None
    stated = _stated_bool(d, text[b:b + 80])
    if stated is None and not text[b:].strip(" :|=-\t") and len(text) <= 80 and seg.next_text:
        parsed = _bool_value(d, seg.next_text)          # "מושבים חשמליים" / "אין" on the next line
        stated = parsed
    if stated is not None:
        value, availability = stated
        return Hit(value, availability, confidence=0.8 if availability != "optional" else 0.65, span=anchor,
                   hints={"availability": availability, "stated_value": True})
    before = text[max(bounds[0], a - NEGATION_BEFORE):a]
    if d.negation_prefix and d.negation_prefix.search(before):
        return Hit(False, "absent", confidence=0.75, span=anchor, hints={"availability": "absent"})
    if _contains(rule.negative, clause):
        return Hit(False, "absent", confidence=0.75, span=anchor, hints={"availability": "absent"})
    after = text[b:bounds[1]]
    other = d.feature_labels.search(after) if d.feature_labels else None
    if _contains(d.negative_terms, after[:other.start()] if other else after):   # "... are not available on X"
        return Hit(False, "absent", confidence=0.7, span=anchor, hints={"availability": "absent"})
    if _contains(d.optional_terms, _window(text, *anchor, 25, bounds)):
        return Hit(True, "optional", confidence=0.6, span=anchor, hints={"availability": "optional"})
    outside = text[bounds[0]:a] + " " + text[b:bounds[1]]      # the label's own words are not equipment context
    if _contains(d.equipment, outside):
        return Hit(True, "present", confidence=0.7, span=anchor, hints={"availability": "standard_or_unspecified"})
    return None   # a bare mention or a label without a value (menu, navigation, spec label) is not a candidate


def _enum_hits(rule: FieldRule, text: str, context: str, confidence: float) -> list[Hit]:
    if rule.require_context and not _contains(rule.positive, context):
        return []
    hits = []
    for value, pattern in rule.enum:
        m = pattern.search(text)
        if m:
            hints = {key: hint for pat, key, hint, _ in rule.ambiguities if pat.search(context)}
            hits.append(Hit(value, m.group(0), confidence=confidence - (0.15 if hints else 0), span=m.span(),
                            hints=hints))
    return hits


GEAR_SPEED = re.compile(r"(?<![\w.])(\d{1,2})\s*-?\s*(?:speed|spd|הילוכים|הילוכי)(?![a-z])")
GEAR_CODE = re.compile(r"(?<![\w.])(\d{1,2})\s?(at|dct|mt|dsg|edct|tct|g-dct|amt)(?![\w])")
GEAR_CODE_TYPE = {"at": "automatic", "mt": "manual", "dct": "dct", "dsg": "dct", "edct": "dct", "tct": "dct",
                  "g-dct": "dct", "amt": "amt"}
STRONG_GEARBOX = {"cvt", "e-cvt", "dct", "single-speed", "amt"}


def _outranked(d: Dictionary, values: Iterable[str]) -> list[str]:
    """The gearbox types of one wording left once every type another named type outranks is dropped (enum_values
    `outranks`, PR #45): {amt, dct} -> [dct] ("רובוטית כפולת מצמדים"), {automatic, amt, dct} -> [dct]."""
    named = list(dict.fromkeys(values))
    beaten = {v for other in named for v in getattr(d, "gearbox_outranks", {}).get(other, ())}
    return [v for v in named if v not in beaten]


def _gearbox(d: Dictionary, text: str, has_alias: bool) -> list[dict]:
    """Gearbox facts in one text: [{type, count, raw, span}]. CVT never yields a gear count."""
    found = []
    for m in GEAR_SPEED.finditer(text):
        near = text[max(0, m.start() - 30):m.end() + 30]
        kinds = _outranked(d, [value for value, pat in d.gearbox_types if pat.search(near) and value != "single-speed"])
        kind = kinds[0] if kinds else None
        found.append({"count": int(m.group(1)), "type": kind, "raw": text[m.start():m.end() + 20].strip(),
                      "span": m.span()})
    for m in GEAR_CODE.finditer(text):
        found.append({"count": int(m.group(1)), "type": GEAR_CODE_TYPE.get(m.group(2)), "raw": m.group(0),
                      "span": m.span()})
    if not found:
        matches = [(m, value) for value, pat in d.gearbox_types for m in [pat.search(text)]
                   if m and (has_alias or value in STRONG_GEARBOX)]
        separate = {v for mv, v in matches if not any(o is not mv and o.start() <= mv.start() and mv.end() <= o.end()
                                                      for o, _ in matches)}
        separate = set(_outranked(d, sorted(separate)))
        if len(separate) > 1:
            return []   # "תיבה ידנית ותיבה אוטומטית": prose naming several gearbox types states none of them (PR #44)
        if separate:    # "רובוטית כפולת מצמדים": the outranking type's wording (PR #45)
            matches = [(m, v) for m, v in matches if v in separate] or matches
        if matches:   # the most specific wording wins ("e-CVT" over "CVT")
            m, value = max(matches, key=lambda mv: mv[0].end() - mv[0].start())
            found.append({"count": 1 if value == "single-speed" else None, "type": value, "raw": m.group(0),
                          "span": m.span()})
    return found


BARE_COUNT = re.compile(r"\s*[:|\-–]?\s*(\d{1,2})\s*$")


def _labelled_count(seg: "Segment", alias_end: int, structured: bool) -> list[dict]:
    """A gear count written as a bare integer in the gear-count label's own cell (PR #44): the value cell of a
    structural pair or the rest of a "label | 8" / "label: 8" line."""
    if structured:
        m = re.fullmatch(r"\s*(\d{1,2})\s*", seg.value)
        span = (len(seg.label), len(seg.text))
    else:
        m = BARE_COUNT.fullmatch(seg.text, alias_end)
        span = (0, m.end(1)) if m else (0, 0)
    if m is None:
        return []
    return [{"count": int(m.group(1)), "type": None, "raw": m.group(1), "span": span}]


TIRE = re.compile(r"(?<!\d)(\d{3})\s?/\s?(\d{2})\s?(z?r|-)\s?(\d{2})(?!\d)")


def _tires(d: Dictionary, text: str) -> list[tuple[str, str, tuple[int, int], str | None]]:
    """(normalized size, raw, span, position: front | rear | both | alternative | None)."""
    out = []
    prev_end = 0
    matches = list(TIRE.finditer(text))
    for i, m in enumerate(matches):
        before = text[max(prev_end, m.start() - 40):m.start()]
        after_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        after = text[m.end():min(after_end, m.end() + 15)]
        found = [(hit.end(), name == "both", name)
                 for name, pattern in (("both", d.both), ("front", d.front), ("rear", d.rear),
                                       ("alternative", d.alternative)) if pattern
                 for hit in pattern.finditer(before)]
        position = max(found)[2] if found else None   # the closest preceding label; "front and rear" wins ties
        if position is None:
            for name, pattern in (("both", d.both), ("front", d.front), ("rear", d.rear),
                                  ("alternative", d.alternative)):
                if _contains(pattern, after):
                    position = name
                    break
        size = f"{m.group(1)}/{m.group(2)} R{m.group(4)}"
        out.append((size, m.group(0), m.span(), position))
        prev_end = m.end()
    return out


TIME = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(h|hours|hour|hrs|שעות|שעה)\s*(?:and\s*)?(\d{1,2})\s*(min|mins|minutes|m|דקות|דק')"
                  r"|(?<![\w.])(\d+(?:[.,]\d+)?)\s*(minutes|minute|mins|min|דקות|דק'|דקה|hours|hour|hrs|h|שעות|שעה)(?![a-zא-ת])"
                  r"|(?<![\w.])(\d{1,2}):(\d{2})\s*(h|hours|שעות)")
WINDOW = re.compile(r"(?:מ-?\s*)?(?<![\d.])(\d{1,2})\s*%?\s*(?:-|to|עד|ל-?)\s*(\d{2,3})\s*%")


def _owner(rule: FieldRule, text: str, pos: int, end: int) -> bool:
    """Context ownership: the nearest preceding component term (within 80 chars) must be one of this
    field's positive terms, not a negative (other component) term; else a positive term right after."""
    before = text[max(0, pos - 80):pos]
    last_pos = max((m.start() for m in rule.positive.finditer(before)), default=-1) if rule.positive else -1
    last_neg = max((m.start() for m in rule.negative.finditer(before)), default=-1) if rule.negative else -1
    if last_pos > last_neg:
        return True
    if last_pos == -1 and last_neg == -1 and rule.positive:
        return bool(rule.positive.search(text[end:end + 30]))
    return False


def _minutes(m: re.Match) -> tuple[float, str] | None:
    if m.group(1):
        hours, minutes = parse_number(m.group(1)) or 0, parse_number(m.group(3)) or 0
        return hours * 60 + minutes, f"{_num_value(hours)} h {_num_value(minutes)} min"
    if m.group(5):
        n = parse_number(m.group(5))
        if n is None:
            return None
        unit = m.group(6)
        if unit in ("minutes", "minute", "mins", "min", "דקות", "דק'", "דקה"):
            return n, f"{_num_value(n)} min"
        return n * 60, f"{_num_value(n)} h"
    if m.group(7):
        hours, minutes = int(m.group(7)), int(m.group(8))
        return hours * 60 + minutes, f"{hours}:{m.group(8)} h"
    return None


def _warranty_chunks(d: Dictionary, text: str) -> list[tuple[int, int, int]]:
    anchors = [m.start() for m in d.warranty_terms.finditer(text)] if d.warranty_terms else []
    chunks = []
    for i, start in enumerate(anchors):
        end = anchors[i + 1] if i + 1 < len(anchors) else min(len(text), start + 140)
        lead = max(anchors[i - 1] + 1 if i else 0, start - 30)
        chunks.append((lead, start, max(end, start + 1)))
    return chunks


YEARS = re.compile(r"(?<![\w.])(\d{1,2})\s*(?:-\s*)?(years|year|yrs|שנים|שנה|שנות)(?![a-z])")
KMS = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{4,7})\s*(?:km|kms|ק\"מ|קילומטר|קילומטרים|kilometres|kilometers)"
                 r"(?![a-z])")


def _warranty(rule: FieldRule, d: Dictionary, text: str) -> list[Hit]:
    hits = []
    for lead, anchor, end in _warranty_chunks(d, text):
        years = YEARS.search(text, anchor, end)
        kms = KMS.search(text, anchor, end)
        unlimited = d.unlimited_km.search(text, anchor, end) if d.unlimited_km else None
        if not (years or kms or unlimited):
            continue
        # the warranty kind is read from the words around THIS statement only (up to its last number),
        # never from the next sentence ("Basic warranty: 4 years / 80,000 km. Battery warranty: ...")
        chunk = text[lead:max(m.end() for m in (years, kms, unlimited) if m) + 3]
        if _contains(d.warranty_battery, chunk):
            kind = "battery"
        elif _contains(d.warranty_excluded, chunk):
            continue
        else:
            kind = "vehicle"
        if kind != (rule.warranty_kind or "vehicle"):
            continue
        generic = kind == "vehicle" and not _contains(d.warranty_vehicle, chunk)
        confidence = 0.8 - (0.15 if generic else 0)
        y = int(years.group(1)) if years else None
        k: Any = parse_number(kms.group(1)) if kms else None
        k = int(k) if isinstance(k, float) else k
        if unlimited and k is None:
            k = "unlimited"
        hints = {"warranty_type": kind}
        if generic:
            hints["warranty_type_note"] = "generic warranty wording (type not stated)"
        span = (anchor, max(m.end() for m in (years, kms, unlimited) if m))
        if rule.component == "years" and y is not None and _plausible(rule, y):
            hits.append(Hit(y, years.group(0), "years", years.group(2), confidence, span, hints=hints))
        elif rule.component == "km" and k is not None and (k == "unlimited" or _plausible(rule, k)):
            hits.append(Hit(k, kms.group(0) if kms else unlimited.group(0), "km", None, confidence, span,
                            hints=hints))
        elif rule.component in (None, "text"):
            parts = ([f"{y} years"] if y is not None else []) + ([f"{k:,} km" if isinstance(k, int)
                                                                   else f"{k} km"] if k is not None else [])
            hits.append(Hit(" / ".join(parts), text[anchor:span[1]], None, None, confidence, span, hints=hints))
    return hits


def _price(rule: FieldRule, d: Dictionary, text: str, anchor: tuple[int, int], structured: bool) -> Hit | None:
    a, b = anchor
    bounds = _clause_bounds(text, a)
    lo, hi = (b, len(text)) if structured else (max(bounds[0], a - LINE_BACK), min(bounds[1], b + LINE_FWD))
    best = None
    for m in NUMBER.finditer(text, lo, hi):
        number = parse_number(m.group(1))
        if number is None or number < 10:
            continue
        unit, unit_end = _unit_at(d, text, m.end())
        currency = d.currencies.get(unit) if unit else None
        if currency is None and d.unit_before:
            pre = d.unit_before.search(text[max(0, m.start() - 6):m.start()])
            currency = d.currencies.get(pre.group(1)) if pre else None
        if unit and currency is None:
            continue  # another unit (kW, km, ...)
        if currency is None and not structured:
            continue
        distance = abs(m.start() - b)
        key = (0 if currency else 1, distance)
        if best is None or key < best[0]:
            best = (key, m, number, currency, unit_end)
    if best is None:
        return None
    _, m, number, currency, unit_end = best
    if not _plausible(rule, number):
        return None
    return Hit(_num_value(number), m.group(1), currency, currency, 0.9 if structured else 0.8,
               (min(a, m.start()), max(b, unit_end)))


# --- the harvest ---------------------------------------------------------------------------------------

def _candidate(rule: FieldRule, seg: Segment, hit: Hit, method: str, alias: str | None, wide: str) -> dict:
    quote = seg.quote if len(seg.quote) <= QUOTE_CHARS else _cut_quote(seg, hit.span)
    block = f"table:{seg.table_index}:row:{seg.row_index}" if seg.table_index is not None else seg.quote
    origin = method if method in ("dom_pair", "unit_anchor", "spec_block") else (
        "column_identity" if seg.column_identity else "table" if seg.kind == "row" else "line")
    out = {"origin": origin, "block": block, "field": rule.name, "value": hit.value, "raw_value": hit.raw_value, "unit": hit.unit,
           "raw_unit": hit.raw_unit, "quote": quote, "matched_alias": alias, "extraction_method": method,
           "parser_confidence": round(max(0.0, min(1.0, hit.confidence)), 2)}
    if seg.table_index is not None:
        out["table_index"], out["row_index"] = seg.table_index, seg.row_index
    if seg.page:
        out["page"] = seg.page
    if seg.header:
        out["variant_hint"] = seg.header
    if seg.column_identity:
        out["column_identity"] = seg.column_identity
    elif seg.multi_column:
        out["column_identity_unknown"] = True     # a column cell whose column cannot be identified (a short row)
    if seg.reversed:
        out["reversed_hebrew_normalized"] = True
    years = sorted(set(re.findall(r"(?<!\d)(20[1-3]\d)(?!\d)", wide)))
    if years:
        out["year_hint"] = years if len(years) > 1 else years[0]
    out.update({k: v for k, v in hit.hints.items() if v not in (None, "")})
    return {k: v for k, v in out.items() if v is not None}


def _cut_quote(seg: Segment, span: tuple[int, int]) -> str:
    start = max(0, span[0] - 80)
    end = min(len(seg.quote), max(span[1], span[0]) + 120)
    return ("…" if start else "") + seg.quote[start:end].strip() + ("…" if end < len(seg.quote) else "")


def _rule_hits(rule: FieldRule, d: Dictionary, seg: Segment) -> list[tuple[Hit, str, str | None, str]]:
    """(hit, method, alias, wide context) for one rule over one segment."""
    out: list[tuple[Hit, str, str | None, str]] = []
    structured = seg.kind in ("row", "leaf", "pair", "block")
    base = {"row": "table_row", "leaf": "structured_data", "pair": "dom_pair",
            "block": "spec_block"}.get(seg.kind, "alias_proximity")
    text = seg.text

    if structured:
        aliases = _alias_hits(rule, seg.label) if len(seg.label) <= 90 else []
        anchors = [(s, e, alias, abbr) for s, e, alias, abbr in aliases]
    else:
        anchors = _alias_hits(rule, text)

    def context(span: tuple[int, int]) -> tuple[str, str]:
        if structured:
            return text, text
        bounds = _clause_bounds(text, span[0])
        # after the value, look only to the end of its own phrase: "95 kWh usable" is excluded, while the
        # bracketed "(usable 95 kWh)" after "102 kWh" belongs to another value and is not
        tail = re.match(r"[^(\[,;\d]{0,20}", text[span[1]:min(bounds[1], span[1] + 20)])
        near = text[max(bounds[0], span[0] - EXCLUSION_PAD):span[1] + (tail.end() if tail else 0)]
        return near, _window(text, *span, POSITIVE_PAD, bounds)

    def accept(hit: Hit | None, method: str, alias: str | None, abbr: bool = False) -> None:
        if hit is None:
            return
        near, wide = context(hit.span)
        own_text = text[hit.span[0]:hit.span[1]] if rule.matcher == "warranty" else text
        reason = semantic_reason(rule.spec, own_text, hit.value)
        if reason:
            reject_candidate(rule, seg.quote, hit.value, method, reason)
            return
        keep, delta, hints = _context_ok(rule, near, wide, full_text=text, value=hit.value)
        if not keep:
            return
        hit.confidence += delta - (0.1 if abbr else 0)
        hit.hints.update(hints)
        if hit.confidence >= MIN_CONFIDENCE:
            out.append((hit, method, alias, wide))

    m = rule.matcher
    if m == "numeric":
        composite = dimension_assignment(rule, d, text)
        if composite:
            item = composite.get(rule.name)
            if item and item[0] is not None and _plausible(rule, item[0]):
                value, span = item
                accept(Hit(_num_value(value), text[span[0]:span[1]], rule.normalized_unit,
                           confidence=0.95, span=span), base, rule.spec.get("axis_token"))
            return out
        for s, e, alias, abbr in anchors:
            if structured:
                label_unit = _label_unit(rule, seg.label)
                accept(_numeric(rule, d, text, (s, e), structured=True, label_unit=label_unit,
                                value_from=len(seg.label),
                                label_conversion=None if label_unit else _label_conversion(rule, seg.label)),
                       base, alias, abbr)
            else:
                # a unit the field converts from, named between the label and its first value ("מומנט מירבי (סל"ד/
                # קג"מ) 1,500-3,500 / 25.5", "בסיס גלגלים (ס"מ) | 300"): the bare value is read in that unit (PR #44)
                first = NUMBER.search(text, e)
                zone = text[e:first.start()] if first else ""
                zone_conversion = _label_conversion(rule, zone) if zone else None
                hit = _numeric(rule, d, text, (s, e), structured=bool(zone_conversion),
                               label_conversion=zone_conversion,
                               value_from=first.start() if zone_conversion else None)
                if hit is None and len(text) <= 60 and seg.next_text and len(seg.next_text) <= 80:
                    joined = f"{text} : {seg.next_text}"
                    hit = _numeric(rule, d, joined, (s, e), structured=True, value_from=len(text))
                    if hit and hit.raw_unit is None and rule.spec.get("unit_required"):
                        # a bare number on the line below a label is not this field's value without its unit
                        reject_candidate(rule, f"{seg.quote} {seg.next_quote}", hit.value, "label_next_line",
                                         "unit_required")
                        hit = None
                    if hit:
                        hit.confidence -= 0.1
                        accept(hit, "label_next_line", alias, abbr)
                        continue
                accept(hit, base, alias, abbr)
        for pattern, value, conf, note in rule.patterns:
            for pm in pattern.finditer(text):
                raw = pm.group(0)
                val = pm.group(1) if value == "$1" and pm.groups() else value
                number = parse_number(str(val)) if isinstance(val, str) else val
                if number is None or not _plausible(rule, float(number)):
                    continue
                hit = Hit(_num_value(float(number)), raw, rule.normalized_unit, None, conf, pm.span(),
                          hints={"pattern_note": note} if note else {})
                accept(hit, "pattern", None)
    elif m == "boolean":
        for s, e, alias, abbr in anchors:
            if structured:
                parsed = _bool_value(d, seg.value)
                if parsed is not None:
                    value, availability = parsed
                    accept(Hit(value, seg.raw_value, confidence=0.9 if availability != "optional" else 0.75,
                               span=(s, e), hints={"availability": availability}), base, alias, abbr)
            else:
                hit = _boolean_line(rule, d, seg, (s, e))
                if hit is not None:
                    hit.confidence -= 0.1 if abbr else 0
                    out.append((hit, "feature_mention", alias, text))
    elif m == "enum":
        context_text = text if structured else f"{text} {seg.next_text}"
        if anchors or not structured:
            for hit in _enum_hits(rule, seg.value if structured else text, context_text,
                                  0.85 if structured else 0.75):
                out.append((hit, base if structured else "enum_term", None, context_text))
    elif m == "gearbox":
        has_alias = bool(anchors) or _contains(rule.alias_any, text)
        source = seg.value if structured and anchors else (text if not structured else "")
        facts = _gearbox(d, source, has_alias) if source else []
        if not facts and rule.component == "count" and anchors:
            # "מספר הילוכים | 8", "מספר הילוכים: 8", a label line above "8": a bare count in the label's own cell
            facts = _labelled_count(seg, anchors[0][1], structured)
        for fact in facts:
            value = fact["count"] if rule.component == "count" else fact["type"]
            if value is None or (rule.component == "count" and not _plausible(rule, value)):
                continue
            accept(Hit(value, fact["raw"], confidence=0.9 if structured else 0.8, span=fact["span"]),
                   base if structured else "gearbox_form", anchors[0][2] if anchors else None)
    elif m == "tire_size":
        source = seg.value if structured else text
        has_tire_word = bool(anchors) or _contains(d.tire_terms, text)
        label_position = None
        if structured:
            for name, pattern in (("front", d.front), ("rear", d.rear), ("alternative", d.alternative)):
                if _contains(pattern, seg.label):
                    label_position = name
        for size, raw, span, position in _tires(d, source):
            position = position or label_position
            confidence = 0.9 if structured else 0.8
            hints: dict = {}
            if position in (rule.component, "both") and rule.component in ("front", "rear"):
                pass
            elif position == "alternative" and rule.component == "alternative":
                pass
            elif position is None and rule.component in ("front", "rear") and has_tire_word and len(_tires(d, source)) == 1:
                confidence, hints = 0.6, {"ambiguity": "single_size_all_wheels"}
            else:
                continue
            hints["position"] = position or "unspecified"
            out.append((Hit(size, raw, confidence=confidence, span=span, hints=hints),
                        base if structured else "tire_size_form", None, text))
    elif m == "rim_axle":
        # D2: the rim diameter of ONE axle, read from that axle's tyre size ("ק' 275/35R19 , א' 285/30R20" -> front
        # 19 / rear 20); a size stated for both axles, or the only size of a tyre statement, counts for each axle
        source = seg.value if structured else text
        has_tire_word = bool(anchors) or _contains(d.tire_terms, text)
        label_position = None
        if structured:
            for name, pattern in (("front", d.front), ("rear", d.rear)):
                if _contains(pattern, seg.label):
                    label_position = name
        sizes = _tires(d, source)
        for size, raw, span, position in sizes:
            position = position or label_position
            if position in (rule.component, "both"):
                confidence = 0.9 if structured else 0.8
            elif position is None and has_tire_word and len({s for s, _, _, _ in sizes}) == 1:
                confidence = 0.6
            else:
                continue
            rim = int(size.rsplit("R", 1)[1])
            if not _plausible(rule, float(rim)):
                continue
            out.append((Hit(rim, raw, rule.normalized_unit, None, confidence, span,
                            hints={"position": position or "unspecified", "tire_size": size}),
                        base if structured else "tire_size_form", None, text))
    elif m in ("charging_time", "charging_window"):
        if not _contains(d.charging, text):
            return out
        pattern = TIME if m == "charging_time" else WINDOW
        for tm in pattern.finditer(text):
            if not _owner(rule, text, tm.start(), tm.end()):
                continue
            if m == "charging_time":
                parsed = _minutes(tm)
                if parsed is None or not _plausible(rule, parsed[0]):
                    continue
                minutes, label = parsed
                hints = {"minutes": _num_value(minutes)}
                window = WINDOW.search(text[max(0, tm.start() - 30):tm.end() + 30])
                if window:
                    hints["window_pct"] = f"{window.group(1)}-{window.group(2)}"
                hit = Hit(label, tm.group(0), "min", None, 0.8 if not structured else 0.9, tm.span(), hints=hints)
            else:
                low, high = int(tm.group(1)), int(tm.group(2))
                if not (0 <= low < high <= 100):
                    continue
                hit = Hit(f"{low}-{high}", tm.group(0), "%", "%", 0.8, tm.span())
            near, wide = (text, text) if structured else (
                _window(text, *tm.span(), EXCLUSION_PAD, _clause_bounds(text, tm.start())), text)
            keep = not any(p.search(near) and not (u and u.search(wide)) for p, u, _ in rule.exclusions)
            if keep:
                out.append((hit, "charging_form", None, wide))
    elif m == "warranty":
        source = text
        for hit in _warranty(rule, d, source):
            out.append((hit, base if structured else "warranty_form", None, source))
    elif m == "price":
        for s, e, alias, abbr in anchors:
            hit = _price(rule, d, text, (s, e) if not structured else (s, e), structured)
            if hit is None:
                continue
            near, wide = (text, text) if structured else context(hit.span)
            keep, delta, hints = _context_ok(rule, near, wide, full_text=text, value=hit.value)
            if keep:
                hit.confidence += delta
                hit.hints.update(hints)
                out.append((hit, base, alias, wide))
    elif m == "text":
        if structured:
            for s, e, alias, abbr in anchors:
                if s > 3:
                    continue
                label_rest = seg.label[e:].strip(" :-()")
                value = seg.raw_value.strip()
                if value and not label_rest and 1 < len(value) <= 120 and not NUMBER.fullmatch(value):
                    out.append((Hit(value, value, confidence=0.5, span=(s, e)), base, alias, text))
        else:
            for s, e, alias, abbr in anchors:
                tail = re.match(r"\s*:\s*(.{2,80}?)\s*$", text[e:])
                if tail and s <= 3:
                    out.append((Hit(seg.quote[e:].split(":", 1)[-1].strip()[:120], tail.group(1), confidence=0.45,
                                    span=(s, len(text))), "label_colon", alias, text))
    return out


def harvest_segments(segments: list[Segment], d: Dictionary) -> list[dict]:
    """Run every field rule over every segment of ONE document. Deduplicates per (field, value, unit)
    keeping the highest parser confidence and counting occurrences; materially different values stay."""
    best: dict[tuple, dict] = {}
    for seg in segments:
        for rule in d.rules:
            for hit, method, alias, wide in _rule_hits(rule, d, seg):
                own_text = seg.text[hit.span[0]:hit.span[1]] if rule.matcher == "warranty" else seg.text
                reason = semantic_reason(rule.spec, own_text, hit.value)
                if reason:
                    reject_candidate(rule, seg.quote, hit.value, method, reason)
                    continue
                cand = _candidate(rule, seg, hit, method, alias, wide)
                key = (rule.name, json.dumps(cand["value"], ensure_ascii=False, sort_keys=True, default=str).lower(),
                       cand.get("unit"), cand.get("position"), cand.get("warranty_type"))
                prior = best.get(key)
                if prior is None:
                    cand["occurrences"] = 1
                    best[key] = cand
                else:
                    prior["occurrences"] += 1
                    hints = [h for h in (prior.get("variant_hints") or [prior.get("variant_hint")]) +
                             [cand.get("variant_hint")] if h]
                    # a value in two columns names no single column (admission then leaves the column layer out)
                    idents = [i for i in (prior.get("column_identities") or [prior.get("column_identity")]) +
                              [cand.get("column_identity")] if i]
                    unknown_column = bool(prior.get("column_identity_unknown") or cand.get("column_identity_unknown"))
                    if cand["parser_confidence"] > prior["parser_confidence"]:
                        cand["occurrences"] = prior["occurrences"]
                        best[key] = prior = cand
                    if len(set(hints)) > 1:
                        prior["variant_hints"] = list(dict.fromkeys(hints))
                    if len(set(idents)) > 1:
                        prior["column_identities"] = list(dict.fromkeys(idents))
                    if unknown_column:
                        prior["column_identity_unknown"] = True
    by_field: dict[str, list[dict]] = {}
    for cand in best.values():
        by_field.setdefault(cand["field"], []).append(cand)
    out: list[dict] = []
    for name, items in by_field.items():
        items.sort(key=lambda c: (-c["parser_confidence"], -c["occurrences"]))
        out += items[:MAX_CANDIDATES_PER_FIELD_PER_DOC]
    return out[:MAX_CANDIDATES_PER_DOC]


# --- structural pair segments, and the unit-anchor addition (never replaces a candidate of the segments) ----------

UNIT_ANCHOR_WINDOW = 60          # chars between an alias and a number + unit (same line or the previous line)
UNIT_ANCHOR_CONFIDENCE = 0.6     # below every label-first match with a unit (0.85)


def pair_segments(pairs: list[dict]) -> list[Segment]:
    """Segments of kind "pair" from src/structure_harvest.html_pairs (the quote already passed admission's check)."""
    out = []
    for p in pairs:
        label, value = str(p.get("label") or ""), str(p.get("value") or "")
        out.append(Segment("pair", normalize_text(f"{label} : {value}"), str(p["quote"]), label=normalize_text(label),
                           value=normalize_text(value), raw_value=value, header=p.get("header") or None,
                           column_identity=p.get("column_identity") or None))
    return out


def _word_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def unit_anchor_candidates(segments: list[Segment], d: Dictionary) -> list[dict]:
    """Unit-first extraction for numeric fields with expected units: every number + unit occurrence on a text line
    whose range (the same clause, or UNIT_ANCHOR_WINDOW chars before / after, on the same line or the previous line)
    holds the alias of EXACTLY ONE numeric field, and that field accepts the unit. Ambiguous -> nothing. Lower
    parser_confidence than label-first matches; the quote holds the alias and the number + unit."""
    numeric = [r for r in d.rules if r.matcher == "numeric"]
    accepting = [r for r in numeric if r.spec.get("expected_units")]
    if not accepting:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    previous: Segment | None = None
    for seg in segments:
        if seg.kind != "line":
            previous = None
            continue
        prev_text, prev_quote = (previous.text, previous.quote) if previous is not None else ("", "")
        previous = seg
        offset = len(prev_text) + 1 if prev_text else 0
        joined = f"{prev_text}\n{seg.text}" if prev_text else seg.text
        joined_quote = f"{prev_quote}\n{seg.quote}" if prev_text else seg.quote
        if len(joined_quote) != len(joined):
            continue
        hits_cache: list[tuple[FieldRule, int, int, str, bool]] | None = None
        for m in NUMBER.finditer(seg.text):
            unit, unit_end = _unit_at(d, seg.text, m.end())
            if unit is None:
                continue
            if RANGE_BEFORE.search(seg.text[max(0, m.start() - 12):m.start()]):
                continue                                   # a range: label-first matching handles those
            ns, ne = m.start() + offset, unit_end + offset
            if hits_cache is None:
                hits_cache = [(r, s, e, alias, abbr) for r in numeric for s, e, alias, abbr in _alias_hits(r, joined)]
            cs, ce = _clause_bounds(seg.text, m.start())
            cs, ce = cs + offset, ce + offset
            near = [h for h in hits_cache if not (h[1] < ne and h[2] > ns) and (
                (h[2] <= ns and ns - h[2] <= UNIT_ANCHOR_WINDOW) or (h[1] >= ne and h[1] - ne <= UNIT_ANCHOR_WINDOW)
                or (h[1] >= cs and h[2] <= ce))]
            # a shorter alias inside a longer alias of another field is that longer field's word ("גובה" in "גובה גחון")
            near = [h for h in near if not any(o is not h and o[0].name != h[0].name and o[1] <= h[1] and h[2] <= o[2]
                                               and o[2] - o[1] > h[2] - h[1] for o in near)]
            if len({h[0].name for h in near}) != 1:
                continue
            rule = near[0][0]
            if rule not in accepting:
                continue
            kind, norm_unit, operation = _classify_unit(rule, unit)
            if kind not in ("ok", "convert"):
                continue
            number = parse_number(m.group(1))
            if number is None:
                continue
            value = OPERATIONS[operation](number) if operation else number
            if not owns_dimension_number(rule, d, joined, ns, m.end()+offset):
                continue
            if not takes_inches(rule) and wheel_size_number(d, joined, ns, m.end() + offset):
                continue
            reason = semantic_reason(rule.spec, joined, value)
            if reason or not _plausible(rule, value):
                reject_candidate(rule, joined_quote, _num_value(value), "unit_anchor", reason or "implausible_value")
                continue
            hit_alias = min(near, key=lambda h: min(abs(h[1] - ne), abs(ns - h[2])))
            _, s, e, alias, abbr = hit_alias
            lo, hi = min(s, ns), max(e, ne)
            bounds = (0, len(joined))
            near_text = joined[max(0, lo - EXCLUSION_PAD):hi + EXCLUSION_PAD]
            keep, delta, hints = _context_ok(rule, near_text, _window(joined, lo, hi, POSITIVE_PAD, bounds))
            if not keep:
                continue
            key = (rule.name, json.dumps(_num_value(value)))
            if key in seen:
                continue
            seen.add(key)
            qs, qe = _word_bounds(joined_quote, lo, hi)
            quote = re.sub(r"\s+", " ", joined_quote[qs:qe]).strip()
            if not quote or len(quote) > QUOTE_CHARS:
                continue
            confidence = UNIT_ANCHOR_CONFIDENCE + min(0.0, delta) - (0.05 if operation else 0) - (0.1 if abbr else 0)
            if operation:
                hints["converted_from"] = {"value": m.group(1), "unit": unit, "operation": operation}
            hit = Hit(_num_value(value), m.group(1), norm_unit or rule.normalized_unit, unit, confidence, (lo, hi),
                      hints=hints)
            wide = _window(joined, lo, hi, POSITIVE_PAD, bounds)
            out.append(_candidate(rule, Segment("line", joined, quote, reversed=seg.reversed), hit, "unit_anchor",
                                  alias, wide))
    return out


def merge_additions(existing: list[dict], additions: list[dict]) -> list[dict]:
    """`existing` unchanged and in order, plus every addition whose (field, material value) no existing candidate
    has, within the per-field and per-document caps (existing candidates win every tie and use the room first)."""
    from .field_recovery import material_key

    keys = {(c.get("field"), material_key(c.get("value"))) for c in existing}
    per_field: dict[str, int] = {}
    for c in existing:
        per_field[c.get("field")] = per_field.get(c.get("field"), 0) + 1
    out = list(existing)
    for cand in additions:
        key = (cand.get("field"), material_key(cand.get("value")))
        if key in keys or per_field.get(cand.get("field"), 0) >= MAX_CANDIDATES_PER_FIELD_PER_DOC \
                or len(out) >= MAX_CANDIDATES_PER_DOC:
            continue
        keys.add(key)
        per_field[cand.get("field")] = per_field.get(cand.get("field"), 0) + 1
        cand.setdefault("occurrences", 1)
        out.append(cand)
    return out


def market_hint(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    return "IL" if host.endswith(".il") else None


def harvest_text(text: str, specs: Iterable[dict], *, tables: list[dict] | None = None,
                 structured: dict | None = None, is_pdf: bool = False, url: str | None = None,
                 document_id: str | None = None, dictionary: Dictionary | None = None,
                 html: str | None = None) -> list[dict]:
    """Candidates from raw material (used by tests and by harvest_document). `html` (an HTML document's source) adds
    its structural DOM pairs; unit anchors are read from the text lines. Additions never displace a candidate of
    the line / table / structured segments (merge_additions)."""
    from .structure_harvest import html_pairs

    from .structure_harvest import clean_soup
    specs = list(specs)
    specs = sanity_specs(specs)
    d = dictionary or Dictionary(specs)
    if html:
        from .tools.extract import _clean_lines
        text = _clean_lines(clean_soup(html).get_text("\n"))
    segments = document_segments(text, tables, structured, is_pdf=is_pdf, dictionary=d)
    if html:
        # (html_pairs never raises) structural pairs join the same per-value dedupe AFTER every other segment: an identical value keeps its
        # earlier candidate unless the pair reads it with a higher parser confidence (a "label | value" quote instead
        # of a label-only line); a value only a pair reads is new
        segments += pair_segments(html_pairs(html, text, alias_pattern=d.any_alias, trim_header=d.trim_header,
                                             identity_rows=d.column_identity_rows))
    cands = harvest_segments(segments, d)
    try:      # an addition never costs the document's other candidates
        cands = merge_additions(cands, unit_anchor_candidates(segments, d))
    except Exception:  # noqa: BLE001
        pass
    hint = market_hint(url)
    for cand in cands:
        if document_id:
            cand["document_id"] = document_id
        if url:
            cand["source_url"] = url
        if hint:
            cand["market_hint"] = hint
    return cands


_DICTIONARIES: dict[str, Dictionary] = {}


def dictionary_for(specs: list[dict]) -> Dictionary:
    specs = sanity_specs(specs)
    key = schema_hash(specs)
    d = _DICTIONARIES.get(key)
    if d is None:
        d = _DICTIONARIES[key] = Dictionary(specs)
    return d


def harvest_document(cache, document_id: str, specs: list[dict], *, rejections: list | None = None) -> tuple[list[dict], bool]:
    """(candidates, cache_hit) for one cached document. Parsed once per document and dictionary: the
    result is stored as derived_field_candidates_<schema_hash>.json and reused by every vehicle; two
    workers asking at once compute it once (single flight)."""
    from .tools.extract import document_structured, document_tables

    d = dictionary_for(specs)
    meta = cache.get(document_id) or {}
    url = meta.get("final_url") or meta.get("url")

    def compute() -> dict:
        is_html = meta.get("doc_type") == "html" or meta.get("kind") == "rendered"
        html = cache.read_body(document_id).decode("utf-8", errors="replace") if is_html else None
        try:
            tables = document_tables(cache, document_id, meta, html)
        except Exception:  # a broken table extraction never prevents the text harvest
            tables = []
        structured = document_structured(cache, document_id, html) if html is not None else None
        with collect_candidate_rejections() as rejected:
            cands = harvest_text(cache.read_text(document_id), specs, tables=tables, structured=structured,
                                 is_pdf=meta.get("doc_type") == "pdf", url=url, document_id=document_id, dictionary=d,
                                 html=html)
        if meta.get("doc_type") == "pdf":
            for cand in cands:
                if cand.get("origin") == "table":
                    cand["origin"] = "pdf_table_text"
        return {"rejections": rejected,"harvester_version": HARVESTER_VERSION, "schema_hash": d.hash, "document_id": document_id,
                "candidates": cands}

    record, hit = cache.derived(document_id, f"field_candidates_{d.hash}", compute)
    if rejections is not None:
        rejections.extend({**r, "document_id": document_id, "source_url": url,
                           "origin": r.get("origin") or r.get("extraction_method") or "line",
                           "block": r.get("block") or r.get("quote")} for r in record.get("rejections") or [])
    return list(record.get("candidates") or []), hit


# --- per-vehicle aggregation -----------------------------------------------------------------------------

def _official_domains(vehicle: dict | None) -> list[str]:
    from .tools.search import default_domains

    return default_domains(vehicle or {})


def catalog_trim_hints(cand: dict, payload: dict | None, spec: dict) -> dict:
    if not spec.get("catalog_trim_hint"):
        return cand
    from .document_binding import target_identity, trim_index, normalize_catalog_trim
    ident = target_identity(payload or {})
    tokens = set()
    for key, entry in (trim_index().get("entries") or {}).items():
        parts = key.split("|")
        if len(parts) > 1 and parts[0] == ident.manufacturer and parts[1] == ident.family:
            tokens.update(normalize_catalog_trim(t) for t in entry.get("trims") or [] if t)
    value = normalize_catalog_trim(cand.get("value"))
    out = dict(cand)
    if tokens and value in tokens:
        out["trim_in_catalog"] = True
        out["parser_confidence"] = max(out.get("parser_confidence", 0), 0.8)
    else:
        out["trim_not_in_catalog"] = True
        out["parser_confidence"] = min(out.get("parser_confidence", 0.5), 0.35)
    return out


def vehicle_hints(cand: dict, vehicle: dict | None) -> dict:
    """Vehicle-specific hints, added per vehicle (the cached harvest is vehicle-independent). Hints only."""
    out = dict(cand)
    vehicle = vehicle or {}
    tokens = [normalize_term(str(vehicle.get(k))) for k in ("trim", "model_code") if vehicle.get(k)]
    text = normalize_text(f"{cand.get('quote') or ''} {cand.get('variant_hint') or ''}")
    if any(t and len(t) >= 3 and t in text for t in tokens):
        out["trim_mentioned"] = True
    year = str(vehicle.get("year") or "")
    hint = cand.get("year_hint")
    if year and hint and year not in (hint if isinstance(hint, list) else [hint]):
        out["year_hint_differs"] = True
    host = urlparse(cand.get("source_url") or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host and any(host == d or host.endswith("." + d) for d in _official_domains(vehicle)):
        out["official_domain"] = True
    return out


def deterministic_candidates_from_events(events: Iterable[dict]) -> list[dict]:
    """The deterministic harvest's candidates only (no model-located source such as grounded_llm): what the parser
    itself found, for "missed by the deterministic harvest" accounting."""
    return candidates_from_events(e for e in events if not e.get("source"))


# Harvest caps (structure_harvest size / time, the PDF text-strategy pass's pages / time) are noted here by the pure
# extractors and logged by RunHarvester as `harvest_capped` for the document being harvested.
_HARVEST_CAPS: ContextVar[list | None] = ContextVar("harvest_caps", default=None)


def note_harvest_cap(**info: Any) -> None:
    sink = _HARVEST_CAPS.get()
    if sink is not None:
        sink.append(info)


@contextmanager
def collect_harvest_caps() -> Iterator[list]:
    sink: list = []
    token = _HARVEST_CAPS.set(sink)
    try:
        yield sink
    finally:
        _HARVEST_CAPS.reset(token)


def candidates_from_events(events: Iterable[dict]) -> list[dict]:
    """Every candidate of the run: the deterministic harvest's (one event per document) and, as separate
    harvest-equivalent events of the same document, other candidate sources (`source`, e.g. grounded_llm; each
    event carries only its own new candidates)."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for event in events:
        if event.get("kind") != "candidates_harvested":
            continue
        source = event.get("source")
        key = (event.get("document_id"), None) if not source else (event.get("document_id"), source, event.get("seq"))
        if key not in seen:
            seen.add(key)
            out += list(event.get("candidates") or [])
    return out


def candidate_matrix(events: list[dict], specs: list[dict], vehicle: dict | None = None,
                     per_field: int | None = None) -> dict:
    """Candidates of the run grouped by applicable requested field. Different values are never merged;
    conflicts stay visible. Never consulted by the field evaluator."""
    names = [s["name"] for s in specs if s.get("applicable", True)]
    fields: dict[str, list[dict]] = {n: [] for n in names}
    documents: set = set()
    for cand in candidates_from_events(events):
        name = normalize_field_name(cand.get("field"))
        if name in fields:
            fields[name].append(vehicle_hints(cand, vehicle))
            documents.add(cand.get("document_id"))
    for name in fields:
        fields[name].sort(key=lambda c: (-(c.get("parser_confidence") or 0), not c.get("market_hint")))
        if per_field:
            fields[name] = fields[name][:per_field]
    # coverage and counts are the deterministic harvest's; model-located (grounded) candidates stay in `fields` (they
    # keep extraction_method grounded_llm) but are counted apart
    grounded = {n: sum(1 for c in fields[n] if is_model_located(c)) for n in names}
    with_cands = [n for n in names if len(fields[n]) > grounded[n]]
    return {"fields": fields, "fields_with_candidates": with_cands,
            "fields_without_candidates": [n for n in names if n not in with_cands],
            "candidate_count": sum(len(v) for v in fields.values()) - sum(grounded.values()),
            "grounded_candidate_count": sum(grounded.values()),
            "fields_with_grounded_candidates": [n for n in names if grounded[n]],
            "documents": len(documents), "applicable_fields": len(names),
            "candidate_field_coverage_pct": round(100 * len(with_cands) / len(names), 1) if names else 0.0}


def is_model_located(cand: dict) -> bool:
    """A candidate a model located (grounded candidates, extraction_method grounded_llm), not the parser."""
    return cand.get("extraction_method") == "grounded_llm"


class RunHarvester:
    """Harvests every document a vehicle run touches, in any phase, once per run. Logs one
    `candidates_harvested` event per document (with its candidates) so the matrix can be rebuilt from
    events.jsonl. A harvesting problem is logged and never interrupts research."""

    def __init__(self, cache, specs: list[dict], run_log, enabled: bool = True):
        try:
            payload = json.loads((run_log.dir / "input.json").read_text("utf-8"))
        except (OSError, ValueError):
            payload = {}
        specs = sanity_specs(specs, payload=payload)
        self.payload = payload
        self.cache, self.specs, self.run_log, self.enabled = cache, specs, run_log, enabled
        self.schema_hash = schema_hash(specs)
        self.done: set[str] = set()
        self.stats = {"documents_harvested": 0, "candidate_cache_hits": 0, "candidate_cache_misses": 0,
                      "candidates_total": 0, "harvest_errors": 0}

    def observe(self, document_ids: Iterable[str], phase: str | None = None) -> None:
        if not self.enabled:
            return
        for document_id in list(document_ids):
            if document_id in self.done:
                continue
            self.done.add(document_id)
            try:
                with collect_harvest_caps() as caps:
                    rejected = []
                    cands, hit = harvest_document(self.cache, document_id, self.specs, rejections=rejected)
                by_name = {s["name"]: s for s in self.specs}
                cands = [catalog_trim_hints(c, self.payload, by_name.get(c.get("field"), {})) for c in cands]
                for row in rejected:
                    self.run_log.event("candidate_rejected", phase=phase, **row)
                for cap in caps:
                    self.run_log.event("harvest_capped", document_id=document_id, phase=phase, **cap)
            except Exception as exc:  # never break research because of the parser
                self.stats["harvest_errors"] += 1
                self.run_log.event("candidate_harvest_failed", document_id=document_id, phase=phase,
                                   error=f"{type(exc).__name__}: {str(exc)[:300]}")
                continue
            self.stats["documents_harvested"] += 1
            self.stats["candidate_cache_hits" if hit else "candidate_cache_misses"] += 1
            self.stats["candidates_total"] += len(cands)
            meta = self.cache.get(document_id) or {}
            self.run_log.event("candidates_harvested", document_id=document_id, phase=phase,
                               url=meta.get("final_url") or meta.get("url"), cache_hit=hit,
                               candidate_count=len(cands),
                               fields=sorted({c.get("field") for c in cands if c.get("field")}),
                               harvester_version=HARVESTER_VERSION, schema_hash=self.schema_hash,
                               candidates=cands)

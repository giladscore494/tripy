"""Deterministic Level-2 candidate harvesting: mechanical reading of every fetched document.

    document in DocumentCache
            ↓
    segments (parsed ONCE per document): text lines (reversed-Hebrew PDF lines repaired),
    table rows (HTML tables, <dl>, PDF tables) and structured-data leaves (JSON-LD, page state)
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
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .fields import DICTIONARY_KEYS, harvest_vocabulary, normalize_field_name

HARVESTER_VERSION = "harvest-v2"   # v2: booleans need a stated value (label-only is never true)
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
    "divide_by_10": lambda v: v / 10,
    "multiply_by_10": lambda v: v * 10,
    "divide_by_1000": lambda v: v / 1000,
    "multiply_by_1000": lambda v: v * 1000,
    "reciprocal_times_100": lambda v: round(100 / v, 2) if v else v,
}
GENERIC_UNITS = ["hp", "ps", "bhp", 'כ"ס', "כוח סוס", "rpm", "סל\"ד", "mph", "lb-ft", "cc", 'סמ"ק', "volt", "v",
                 "mpg", "mpge", "miles", "mi", "seats", "מושבים", "doors", "דלתות", "cylinders", "צילינדרים"]


def _term_regex(term: str) -> str:
    """Regex for one alias/term: spaces and hyphens interchangeable; word boundaries that also work for
    Hebrew (with up to two attached prefix letters: ו ה ב ל מ ש כ)."""
    norm = normalize_term(term)
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", norm) if p]
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


def compile_terms(terms: Iterable[str]) -> re.Pattern | None:
    terms = sorted({t for t in terms if t and t.strip()}, key=lambda t: -len(t))
    if not terms:
        return None
    return re.compile("|".join(f"(?:{_term_regex(t)})" for t in terms))


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
        self.trim_header = compile_terms(v.get("trim_header_terms") or [])
        self.currencies = {normalize_term(var): code for code, variants in (v.get("currencies") or {}).items()
                           for var in variants}
        self.hebrew_aliases = sorted({normalize_term(a) for r in self.rules for a, _, _ in r.aliases
                                      if HEBREW.match(normalize_term(a)) and len(a) >= 4})
        gearbox = next((r for r in self.rules if r.matcher == "gearbox" and r.enum), None)
        self.gearbox_types = gearbox.enum if gearbox else []
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

@dataclass
class Segment:
    kind: str                 # line | row | leaf
    text: str                 # text matched (normalized)
    quote: str                # original text for the quote
    label: str = ""           # rows/leaves: normalized label
    value: str = ""           # rows/leaves: normalized value cell
    raw_value: str = ""
    table_index: int | None = None
    row_index: int | None = None
    page: int | None = None
    header: str | None = None
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


def document_segments(text: str, tables: list[dict] | None, structured: dict | None, *, is_pdf: bool,
                      dictionary: Dictionary) -> list[Segment]:
    """Every searchable unit of one document, built ONCE and then read by all field matchers."""
    segments: list[Segment] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    norms: list[tuple[str, str, bool]] = []
    for line in lines:
        reversed_ = bool(is_pdf and HEBREW.search(line) and looks_reversed(line, dictionary.hebrew_aliases))
        logical = reverse_hebrew_line(line) if reversed_ else line
        norms.append((normalize_text(logical), logical if reversed_ else line, reversed_))
    for i, (norm, quote, reversed_) in enumerate(norms):
        nxt = norms[i + 1] if i + 1 < len(norms) else ("", "", False)
        segments.append(Segment("line", norm, quote, reversed=reversed_, next_text=nxt[0], next_quote=nxt[1]))
    for t_index, table in enumerate(tables or []):
        rows = table.get("rows") or []
        # a header row: at most one numeric cell, or a first cell that names the column dimension (version / trim),
        # so variant columns such as "1.8 Hybrid 140 | 2.0 Hybrid 196" keep their names as variant hints
        header = rows[0] if rows and len(rows[0]) > 2 and (
            sum(1 for c in rows[0] if NUMBER.search(str(c))) <= 1
            or _contains(dictionary.trim_header, normalize_text(str(rows[0][0] or "")))) else None
        for r_index, row in enumerate(rows):
            cells = [str(c or "").strip() for c in row]
            if len(cells) < 2 or not cells[0]:
                continue
            values = [(j, c) for j, c in enumerate(cells[1:], start=1) if c]
            for j, cell in values:
                head = header[j] if header and j < len(header) and header is not row else None
                segments.append(Segment("row", normalize_text(f"{cells[0]} : {cell}"),
                                        f"{cells[0]} | {cell}" + (f" ({head})" if head and len(values) > 1 else ""),
                                        label=normalize_text(cells[0]), value=normalize_text(cell), raw_value=cell,
                                        table_index=t_index, row_index=r_index, page=table.get("page"),
                                        header=head if head and len(values) > 1 else None))
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


def _context_ok(rule: FieldRule, near: str, wide: str) -> tuple[bool, float, dict]:
    """Exclusion / negative / positive / ambiguity rules. Returns (keep, confidence delta, hints)."""
    hints: dict = {}
    delta = 0.0
    for pattern, unless, reason in rule.exclusions:
        if pattern.search(near) and not (unless and unless.search(wide)):
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
    return (low is None or value >= float(low)) and (high is None or value <= float(high))


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


# --- matchers ------------------------------------------------------------------------------------------

def _numeric(rule: FieldRule, d: Dictionary, text: str, anchor: tuple[int, int], *, structured: bool,
             label_unit: str | None = None, value_from: int | None = None) -> Hit | None:
    a, b = anchor
    bounds = _clause_bounds(text, a)
    lo = value_from if value_from is not None else max(bounds[0], a - LINE_BACK)
    hi = len(text) if value_from is not None else min(bounds[1], b + LINE_FWD)
    best = None
    for m in NUMBER.finditer(text, lo, hi):
        s, e = m.span()
        if s < b and e > a:
            continue  # part of the alias itself (e.g. "0-100")
        number = parse_number(m.group(1))
        if number is None:
            continue
        unit, unit_end = _unit_at(d, text, e)
        kind, norm_unit, operation = _classify_unit(rule, unit)
        if kind == "other":
            continue
        if kind == "none" and label_unit:
            kind, norm_unit = "ok", label_unit
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
    if _contains(d.negative_terms, cell):          # "not available" is not the optional "available"
        return False, "absent"
    if cell in d.optional_values or _contains(d.optional_terms, cell):
        return True, "optional"
    if cell in d.affirmative or cell.startswith(("standard", "סטנדרט", "כן", "yes", "✓", "✔")):
        return True, "standard"
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
    if _contains(d.negative_terms, text[b:bounds[1]]):          # "heated seats are not available on Business"
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


def _gearbox(d: Dictionary, text: str, has_alias: bool) -> list[dict]:
    """Gearbox facts in one text: [{type, count, raw, span}]. CVT never yields a gear count."""
    found = []
    for m in GEAR_SPEED.finditer(text):
        near = text[max(0, m.start() - 30):m.end() + 30]
        kind = next((value for value, pat in d.gearbox_types if pat.search(near) and value != "single-speed"), None)
        found.append({"count": int(m.group(1)), "type": kind, "raw": text[m.start():m.end() + 20].strip(),
                      "span": m.span()})
    for m in GEAR_CODE.finditer(text):
        found.append({"count": int(m.group(1)), "type": GEAR_CODE_TYPE.get(m.group(2)), "raw": m.group(0),
                      "span": m.span()})
    if not found:
        matches = [(m, value) for value, pat in d.gearbox_types for m in [pat.search(text)]
                   if m and (has_alias or value in STRONG_GEARBOX)]
        if matches:   # the most specific wording wins ("e-CVT" over "CVT")
            m, value = max(matches, key=lambda mv: mv[0].end() - mv[0].start())
            found.append({"count": 1 if value == "single-speed" else None, "type": value, "raw": m.group(0),
                          "span": m.span()})
    return found


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
    out = {"field": rule.name, "value": hit.value, "raw_value": hit.raw_value, "unit": hit.unit,
           "raw_unit": hit.raw_unit, "quote": quote, "matched_alias": alias, "extraction_method": method,
           "parser_confidence": round(max(0.0, min(1.0, hit.confidence)), 2)}
    if seg.table_index is not None:
        out["table_index"], out["row_index"] = seg.table_index, seg.row_index
    if seg.page:
        out["page"] = seg.page
    if seg.header:
        out["variant_hint"] = seg.header
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
    structured = seg.kind in ("row", "leaf")
    base = "table_row" if seg.kind == "row" else ("structured_data" if seg.kind == "leaf" else "alias_proximity")
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
        keep, delta, hints = _context_ok(rule, near, wide)
        if not keep:
            return
        hit.confidence += delta - (0.1 if abbr else 0)
        hit.hints.update(hints)
        if hit.confidence >= MIN_CONFIDENCE:
            out.append((hit, method, alias, wide))

    m = rule.matcher
    if m == "numeric":
        for s, e, alias, abbr in anchors:
            if structured:
                label_unit = _label_unit(rule, seg.label)
                accept(_numeric(rule, d, text, (s, e), structured=True, label_unit=label_unit,
                                value_from=len(seg.label)), base, alias, abbr)
            else:
                hit = _numeric(rule, d, text, (s, e), structured=False)
                if hit is None and len(text) <= 60 and seg.next_text and len(seg.next_text) <= 80:
                    joined = f"{text} : {seg.next_text}"
                    hit = _numeric(rule, d, joined, (s, e), structured=True, value_from=len(text))
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
        for fact in _gearbox(d, source, has_alias) if source else []:
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
            elif position is None and rule.component in ("front", "rear") and has_tire_word:
                confidence, hints = 0.45, {"ambiguity": "axle_not_stated"}
            else:
                continue
            hints["position"] = position or "unspecified"
            out.append((Hit(size, raw, confidence=confidence, span=span, hints=hints),
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
            keep, delta, hints = _context_ok(rule, near, wide)
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
                    if cand["parser_confidence"] > prior["parser_confidence"]:
                        cand["occurrences"] = prior["occurrences"]
                        best[key] = prior = cand
                    if len(set(hints)) > 1:
                        prior["variant_hints"] = list(dict.fromkeys(hints))
    by_field: dict[str, list[dict]] = {}
    for cand in best.values():
        by_field.setdefault(cand["field"], []).append(cand)
    out: list[dict] = []
    for name, items in by_field.items():
        items.sort(key=lambda c: (-c["parser_confidence"], -c["occurrences"]))
        out += items[:MAX_CANDIDATES_PER_FIELD_PER_DOC]
    return out[:MAX_CANDIDATES_PER_DOC]


def market_hint(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    return "IL" if host.endswith(".il") else None


def harvest_text(text: str, specs: Iterable[dict], *, tables: list[dict] | None = None,
                 structured: dict | None = None, is_pdf: bool = False, url: str | None = None,
                 document_id: str | None = None, dictionary: Dictionary | None = None) -> list[dict]:
    """Candidates from raw material (used by tests and by harvest_document)."""
    d = dictionary or Dictionary(specs)
    segments = document_segments(text, tables, structured, is_pdf=is_pdf, dictionary=d)
    cands = harvest_segments(segments, d)
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
    key = schema_hash(specs)
    d = _DICTIONARIES.get(key)
    if d is None:
        d = _DICTIONARIES[key] = Dictionary(specs)
    return d


def harvest_document(cache, document_id: str, specs: list[dict]) -> tuple[list[dict], bool]:
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
        cands = harvest_text(cache.read_text(document_id), specs, tables=tables, structured=structured,
                             is_pdf=meta.get("doc_type") == "pdf", url=url, document_id=document_id, dictionary=d)
        return {"harvester_version": HARVESTER_VERSION, "schema_hash": d.hash, "document_id": document_id,
                "candidates": cands}

    record, hit = cache.derived(document_id, f"field_candidates_{d.hash}", compute)
    return list(record.get("candidates") or []), hit


# --- per-vehicle aggregation -----------------------------------------------------------------------------

def _official_domains(vehicle: dict | None) -> list[str]:
    from .tools.search import default_domains

    return default_domains(vehicle or {})


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


def candidates_from_events(events: Iterable[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for event in events:
        if event.get("kind") == "candidates_harvested" and event.get("document_id") not in seen:
            seen.add(event.get("document_id"))
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
    with_cands = [n for n in names if fields[n]]
    return {"fields": fields, "fields_with_candidates": with_cands,
            "fields_without_candidates": [n for n in names if not fields[n]],
            "candidate_count": sum(len(v) for v in fields.values()), "documents": len(documents),
            "applicable_fields": len(names),
            "candidate_field_coverage_pct": round(100 * len(with_cands) / len(names), 1) if names else 0.0}


class RunHarvester:
    """Harvests every document a vehicle run touches, in any phase, once per run. Logs one
    `candidates_harvested` event per document (with its candidates) so the matrix can be rebuilt from
    events.jsonl. A harvesting problem is logged and never interrupts research."""

    def __init__(self, cache, specs: list[dict], run_log, enabled: bool = True):
        self.cache, self.specs, self.run_log, self.enabled = cache, specs, run_log, enabled
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
                cands, hit = harvest_document(self.cache, document_id, self.specs)
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
                               candidates=cands)

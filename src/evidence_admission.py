"""Evidence Admission Gate: a store_evidence request becomes Evidence only if deterministic checks pass.

    store_evidence(field, value, quote, document_id | source_url, market?, variant?, variant_match?, note?, ...)
            ↓
    1. provenance      the cited document must be RETRIEVED (in the document cache). A URL never fetched, a
                       search snippet or the model's memory is not a source.
    2. applicability   the field must apply to this vehicle (schema applies_to by propulsion).
    3. quote           the quote must occur in that document (normalized: case, punctuation, whitespace,
                       reversed-Hebrew PDF lines; digit groups stay whole, so "150 mm" is not in "4,150 mm";
                       "…" joins verbatim fragments IN ORDER within one short passage). With no quote, a
                       deterministic candidate of the same document, field and value supplies it.
    4. entailment      ONE fragment of the quote must state the value FOR THIS FIELD: a deterministic parse with
                       the field's own dictionary rule (booleans: the field's label with a stated availability
                       right after it, or a negation right before it), or the number literally (written numbers
                       only with a unit) / by a schema-approved conversion, next to the field's label or backed by
                       the parser's own pairing of this field and value in this document, and not a number the
                       parser assigns to another field. Nothing else: "e-CVT" does not state a gear count of 1;
                       a note never states anything.
    5. semantics       unit, plausibility range, and the field's semantic exclusions for this propulsion, read in
                       the value's own clause of the quote AND of the source line it comes from (a hybrid's torque
                       field means engine torque, not system torque).
    6. identity        server-side binding (src/document_binding.py) -> binding_level, variant_match; market
                       from the source itself (src/source_authority.py); the model's own variant_match and
                       market are kept only as model_variant_claim / model_market_claim.
    7. authority       source_authority from deterministic source rules.
    8. typing & time   typed_value (scalar / range / boolean / enum / text / compound), a condition only when
                       the quote states it, valid_as_of only from a date stated in the quote or its source lines,
                       else the document's own publication metadata (never invented).
            ↓
    accepted -> EvidenceStore          rejected -> `evidence_rejected` event + reasons back to the model

No model call. `note` is commentary: it is stored, but no check reads it, so it can never supply a value,
a market, a variant or a binding. The gate does not decide which of several admitted values is true; the
field evaluator (field_recovery.current_evaluation) stays the only field-state authority.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .candidate_harvest import (NUMBER, OPERATIONS, TIRE, _bool_value, _classify_unit, _contains, _owner, _stated_bool,
                                compile_terms, dictionary_for, harvest_document, harvest_text, normalize_term,
                                normalize_text, parse_number, reverse_hebrew_line)
from .document_binding import TargetIdentity, about_target, bind, document_profile, target_identity
from .fields import harvest_vocabulary, load_schema, normalize_field_name, resolve_requested_fields
from .source_authority import classify_source, normalize_market, source_market
from .structure_harvest import page_text
from .typed_values import as_boolean, numbers_in, typed_value

ADMISSION_VERSION = "admission-v2"
MIN_QUOTE_TOKENS, MIN_QUOTE_CHARS = 2, 4
NUMERIC_MATCHERS = ("numeric", "price")
COMPOUND_MATCHERS = ("warranty", "charging_time", "charging_window", "tire_size", "gearbox")
FRAGMENT_WINDOW = 400          # squashed characters a "…"-joined quote may span in the source
SHARED_TEXT_CACHE = 256        # documents whose target-independent material is shared by all runs
REASON_TEXT = {
    "source_not_retrieved": "The cited document is not in the document store. Fetch it (fetch_url / fetch_pdf) and "
                            "cite its document_id; search results and memory are not sources.",
    "field_not_applicable_for_vehicle": "This field does not apply to this vehicle's propulsion; report "
                                        "not_applicable instead.",
    "quote_missing": "Give a short verbatim quote from the document that states the value.",
    "quote_too_short": "The quote is too short to identify what it states; quote the label with the value.",
    "quote_not_in_source": "The quote does not occur in the cited document (fragments joined with … must be in "
                           "order and close together). Quote the document verbatim.",
    "value_missing": "No value given.",
    "invalid_value_type": "The value does not have the field's type.",
    "unsupported_inference": "The quote does not state this value; it would be an inference. Store only what the "
                             "source states (or report the field unresolved / not_applicable).",
    "value_not_in_quote": "The quote does not contain this value.",
    "value_not_stated": "The quote does not state this feature's availability next to its label (a label alone, or "
                        "another feature's 'yes', is not a statement).",
    "field_label_not_in_quote": "The quote has the number but not this field's label; quote the label with the value.",
    "value_belongs_to_other_field": "In this quote the value is stated for another field.",
    "unit_mismatch": "The value's unit is not the field's unit.",
    "unit_not_normalized": "Convert the value to the field's unit before storing it.",
    "implausible_value": "The value is outside the field's plausible range (wrong unit or wrong quantity?).",
    "semantic_mismatch": "The quote describes a different quantity than this field means (see semantic_definition).",
}
REJECTION_PRIORITY = ("semantic_mismatch", "unit_mismatch", "unit_not_normalized", "value_belongs_to_other_field",
                      "value_not_stated", "field_label_not_in_quote", "unsupported_inference", "value_not_in_quote",
                      "invalid_value_type")
HEBREW_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר",
                 "דצמבר"]
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]
CLAUSE_BREAK = re.compile(r"[;\n|•]|\.\s(?!\d)|,\s(?!\d)")
FEATURE_BREAK = re.compile(r"[;\n|•,]|\.\s(?!\d)")
FRAGMENT_SPLIT = re.compile(r"…|\.\.\.|\[\.\.\.\]")
TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")
# "4.5 litres per 100 km" / "4.5 ליטר למאה ק"מ" / "22 kWh per 100 km" written the way the unit dictionary spells
# consumption units (l/100km, kwh/100km)
PER_100 = re.compile(r"(?<![\wא-ת])(?P<unit>ליטרים|ליטר|litres|liters|litre|liter|lt|l|kwh|קוט\"ש)\s*"
                     r"(?:/\s*|per\s+|ל-?\s*|לכל\s+)?(?:100|מאה|למאה)\s*(?:km|ק\"מ|kilometres|kilometers)(?![\w])")


def _per_100(match: re.Match) -> str:
    return ("kwh" if match.group("unit") in ("kwh", 'קוט"ש') else "l") + "/100km"
DATE_KEYS = (("json_ld", "dateModified"), ("meta", "article:modified_time"), ("meta", "og:updated_time"),
             ("json_ld", "datePublished"), ("meta", "article:published_time"), ("meta", "date"),
             ("pdf", "ModDate"), ("pdf", "CreationDate"))
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)


HEBREW_CONJUNCTION = re.compile(r"(?<![\w])ו(?=[א-ת]{2,})")


def squash(text: Any) -> str:
    """Comparison form for quotes: normalized characters, lowercase, words and WHOLE numbers ("4,150", "146.0" stay
    one token) separated by single spaces; a Hebrew conjunction prefix (ו) is dropped on both sides, so a quote of
    "חימום מושבים: יש" matches "וחימום מושבים: יש"."""
    return " ".join(TOKEN.findall(HEBREW_CONJUNCTION.sub("", normalize_text(str(text or "")))))


def _tokens(text: str) -> list[str]:
    return squash(text).split()


# --- per-document material --------------------------------------------------------------------------------

@dataclass
class DocumentText:
    """Target-independent material of one cached document, shared by every run (bounded LRU)."""
    document_id: str
    url: str | None
    meta: dict
    text: str
    haystack: str                      # squashed text + tag-stripped source + structured data (+ reversed lines)
    lines: list[tuple[str, str]]       # (original line, squashed line)
    headings: list[str] | None
    subheadings: list[str] | None      # HTML H2s (identity-zone year statements only)
    body_text: str | None              # HTML: the page without its chrome (binding's full-text profile only)
    market: str | None
    market_basis: str | None
    source_date: str | None
    source_date_basis: str | None


@dataclass
class DocumentMaterial:
    """A document as one run sees it: shared text + this target's binding profile, authority and candidates."""
    doc: DocumentText
    profile: dict
    authority: dict
    candidates: list[dict]

    def __getattr__(self, name: str) -> Any:            # document_id, url, meta, haystack, lines, market, ...
        return getattr(self.__dict__["doc"], name)


_TEXTS: "OrderedDict[tuple[str, str], DocumentText]" = OrderedDict()
_TEXTS_LOCK = threading.Lock()


def _iso(value: str) -> str | None:
    text = str(value or "").strip()
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?", text) if text.startswith("D:") else None
    if m:
        return "-".join(p for p in m.groups() if p)
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if m:
        return "-".join(p for p in m.groups() if p)
    return None


def _document_date(meta: dict, structured: dict | None) -> tuple[str | None, str | None]:
    """The document's OWN publication/modification date (metadata), never the fetch time."""
    pdf_meta = meta.get("pdf_metadata") or {}
    for source, key in DATE_KEYS:
        value = None
        if source == "pdf":
            value = pdf_meta.get(key) or pdf_meta.get("/" + key)
        elif source == "meta":
            value = ((structured or {}).get("meta") or {}).get(key)
        elif source == "json_ld":
            for item in (structured or {}).get("json_ld") or []:
                for node in item if isinstance(item, list) else [item]:
                    if isinstance(node, dict) and node.get(key):
                        value = node[key]
                        break
                if value:
                    break
        iso = _iso(value) if value else None
        if iso:
            return iso, f"{source}:{key}"
    return None, None


def document_text(cache, meta: dict) -> DocumentText:
    """The shared, target-independent material of a cached document (built once per document version)."""
    from .tools.extract import document_structured

    doc_id = meta["document_id"]
    key = (doc_id, str(meta.get("sha256") or meta.get("fetched_at") or ""))
    with _TEXTS_LOCK:
        cached = _TEXTS.get(key)
        if cached is not None:
            _TEXTS.move_to_end(key)
            return cached
    url = meta.get("final_url") or meta.get("url")
    text = cache.read_text(doc_id)
    parts, structured, headings = [text], None, None     # headings: H1s of HTML (a list); None = text / PDF
    subheadings = body_text = None
    if meta.get("doc_type") == "html" or meta.get("kind") == "rendered":
        html = cache.read_body(doc_id).decode("utf-8", errors="replace")
        parts.append(re.sub(r"<[^>]+>", " ", html))
        headings = [re.sub(r"<[^>]+>|\s+", " ", h).strip() for h in H1.findall(html)][:3]
        subheadings = [re.sub(r"<[^>]+>|\s+", " ", h).strip() for h in H2.findall(html)][:3]
        try:
            body_text = page_text(html)
        except Exception:  # an unparsable page keeps its stored text for the profile
            body_text = None
        try:
            structured = document_structured(cache, doc_id, html)
            parts.append(json.dumps(structured, ensure_ascii=False))
        except Exception:  # structured data is a convenience; the text is enough
            structured = None
    if meta.get("doc_type") == "pdf":
        parts.append("\n".join(reverse_hebrew_line(line) for line in text.splitlines()))
    market, basis = source_market(url, text)
    source_date, date_basis = _document_date(meta, structured)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    material = DocumentText(document_id=doc_id, url=url, meta=meta, text=text,
                            haystack=" " + " ".join(squash(p) for p in parts) + " ",
                            lines=[(ln, " " + squash(ln) + " ") for ln in lines], headings=headings,
                            subheadings=subheadings, body_text=body_text,
                            market=market, market_basis=basis, source_date=source_date, source_date_basis=date_basis)
    with _TEXTS_LOCK:
        _TEXTS[key] = material
        while len(_TEXTS) > SHARED_TEXT_CACHE:
            _TEXTS.popitem(last=False)
    return material


class AdmissionContext:
    """Everything the gate needs for ONE vehicle run: target identity, field specs, target market, and the
    per-document binding profiles of this target (the target-independent text is shared across runs)."""

    def __init__(self, *, identity: TargetIdentity, specs: list[dict], target_market: str = "IL",
                 manufacturer: str | None = None):
        self.identity = identity
        self.specs = {s["name"]: s for s in specs}
        self.all_specs = list(specs)
        # Fields outside the run's requested subset keep their schema policy (applicability, plausibility, semantics,
        # binding requirement): the full schema resolved for this vehicle's propulsion is the fallback.
        self.fallback = {s["name"]: s for s in resolve_requested_fields(None, load_schema(),
                                                                       propulsion=identity.propulsion)}
        # Entailment parses a quote with the WHOLE dictionary (shared vocabularies such as gearbox types come from
        # other fields' rules), the run's own specs overriding same-name defaults.
        merged = dict(self.fallback)
        merged.update(self.specs)
        self.parse_specs = list(merged.values())
        self.dictionary = dictionary_for(self.parse_specs)
        self.target_market = normalize_market(target_market) or "IL"
        self.manufacturer = manufacturer or identity.manufacturer
        self._docs: dict[str, DocumentMaterial] = {}
        self._lock = threading.Lock()
        vocab = harvest_vocabulary()
        self.number_words = {normalize_term(w): float(n) for n, words in (vocab.get("number_words") or {}).items()
                             for w in words}
        self.feature_labels = self.dictionary.feature_labels
        # every field label that can precede a number: the nearest one before a value names the field it belongs to
        self.value_labels = [(r.name, pattern) for r in self.dictionary.rules
                             if r.matcher in NUMERIC_MATCHERS + COMPOUND_MATCHERS for _, pattern, _ in r.aliases]
        self.matcher_of = {r.name: r.matcher for r in self.dictionary.rules}

    @classmethod
    def for_run(cls, payload: dict | None, vehicle: dict | None, specs: list[dict],
                target_market: str = "IL") -> "AdmissionContext":
        identity = target_identity(payload, vehicle, normalize_market(target_market) or "IL")
        return cls(identity=identity, specs=specs, target_market=target_market,
                   manufacturer=((payload or {}).get("identity") or {}).get("manufacturer")
                   or (vehicle or {}).get("manufacturer"))

    @classmethod
    def default(cls, vehicle: dict | None) -> "AdmissionContext":
        """For tool contexts without a run (standalone tools, tests): identity from the vehicle metadata only."""
        vehicle = vehicle or {}
        specs = resolve_requested_fields(None, load_schema(), propulsion=vehicle.get("propulsion"))
        return cls.for_run(None, vehicle, specs, vehicle.get("target_market") or "IL")

    def spec(self, name: str) -> dict:
        return self.specs.get(name) or self.fallback.get(name) or {"name": name}

    def material(self, cache, document_id: str | None, source_url: str | None,
                 run_documents: list[str] | tuple = ()) -> DocumentMaterial | None:
        """The cited document: by id, else by URL (exact cache key of any fetch kind, else a document this run
        touched whose final URL it is). Never a scan of the whole shared cache."""
        from .storage.cache import document_id_for

        meta = cache.get(str(document_id)) if document_id else None
        if meta is None and source_url:
            url = str(source_url).split("#")[0].strip()
            for kind in ("rendered", "pdf", "fetch"):
                meta = cache.get(document_id_for(kind, url))
                if meta:
                    break
            if meta is None:
                for doc_id in run_documents:
                    candidate = cache.get(doc_id) or {}
                    if url in (candidate.get("final_url"), candidate.get("url")):
                        meta = candidate
                        break
        if not meta:
            return None
        doc_id = meta["document_id"]
        with self._lock:
            cached = self._docs.get(doc_id)
        if cached is not None:
            return cached
        doc = document_text(cache, meta)
        try:
            candidates, _ = harvest_document(cache, doc_id, self.all_specs)
        except Exception:  # the parser never blocks admission; quotes are checked against the text
            candidates = []
        material = DocumentMaterial(
            doc=doc, profile=document_profile(text=doc.text, title=meta.get("title"), url=doc.url,
                                              identity=self.identity, headings=doc.headings,
                                              subheadings=doc.subheadings, body_text=doc.body_text),
            authority=classify_source(doc.url, self.manufacturer), candidates=candidates)
        with self._lock:
            self._docs[doc_id] = material
        return material


# --- quote verification ---------------------------------------------------------------------------------

def quote_fragments(quote: str) -> list[str]:
    """The verbatim fragments of a quote ("…" joins fragments of one passage)."""
    return [p.strip() for p in FRAGMENT_SPLIT.split(quote) if squash(p)]


def _in_order(haystack: str, parts: list[str], window: int) -> bool:
    start = 0
    while True:
        first = haystack.find(f" {parts[0]} ", start)
        if first < 0:
            return False
        end, ok, limit = first + len(parts[0]) + 1, True, first + window + 2
        for part in parts[1:]:
            pos = haystack.find(f" {part} ", end, limit)      # bounded: never a scan of the rest of the document
            if pos < 0:
                ok = False
                break
            end = pos + len(part) + 1
        if ok:
            return True
        start = first + 1


def quote_in_source(material: DocumentMaterial, quote: str) -> bool:
    """The quote's fragments occur in the document in order within one short passage (or inside one quote the
    deterministic parser cut from this same document)."""
    parts = [squash(p) for p in quote_fragments(quote)]
    if not parts:
        return False
    if _in_order(material.haystack, parts, FRAGMENT_WINDOW):
        return True
    return any(_in_order(" " + squash(c.get("quote")) + " ", parts, FRAGMENT_WINDOW)
               for c in material.candidates if c.get("quote"))


# --- entailment -------------------------------------------------------------------------------------------

def _close(a: float, b: float, rel: float = 1e-9) -> bool:
    return abs(a - b) <= max(1e-6, rel * abs(b))


def _same_value(spec: dict, claimed: Any, candidate: Any) -> bool:
    if isinstance(claimed, bool) or isinstance(candidate, bool):
        return as_boolean(claimed) is not None and as_boolean(claimed) == as_boolean(candidate)
    a, b = numbers_in(claimed), numbers_in(candidate)
    if a or b:
        return len(a) == len(b) and all(_close(x, y, 1e-6) for x, y in zip(sorted(a), sorted(b)))
    return squash(claimed) == squash(candidate) or normalize_term(str(claimed)) == normalize_term(str(candidate))


def clause_span(text: str, position: int | None, breaks: re.Pattern = CLAUSE_BREAK) -> tuple[int, int]:
    if position is None:
        return 0, len(text)
    start = 0
    for m in breaks.finditer(text):            # (an endpos would cut the lookaheads: ", 185" is no break)
        if m.start() >= position:
            break
        start = m.end()
    end_match = breaks.search(text, position)
    return start, end_match.start() if end_match else len(text)


def value_clause(text: str, position: int | None, breaks: re.Pattern = CLAUSE_BREAK) -> str:
    start, end = clause_span(text, position, breaks)
    return text[start:end]


NOUN_AFTER = re.compile(r"\s*-?\s*(?!(?:at|from|to|and|or|with|in|on|for|by|per|each|approx|up)\b)[a-z]{3,}")
PAREN_GROUP = re.compile(r"[(\[][^()\[\]]*[)\]]")


def semantic_clause(text: str, position: int | None) -> str:
    """What a value's own words are: its clause, up to the next number after it ("596 litres, 1,526 litres with
    seats folded" -> "596 litres, "), inside its bracket group when it sits in one ("(system 185 Nm)"), and without
    other bracket groups that hold numbers (qualifiers such as "(system)" before it stay)."""
    if position is None:
        return text
    start, end = clause_span(text, position)
    number = NUMBER.match(text, position) or re.match(r"\S+", text[position:])
    after = position + (number.end() - number.start() if number else 0)
    nxt = NUMBER.search(text, after, end)
    end = nxt.start() if nxt else end
    for group in PAREN_GROUP.finditer(text, start, end):
        if group.start() <= position < group.end():
            return text[group.start() + 1:group.end() - 1]
    segment = text[start:end]
    return PAREN_GROUP.sub(lambda g: " " if re.search(r"\d", g.group(0)) else g.group(0), segment)


def _nearest_label(adm: AdmissionContext, lead: str) -> str | None:
    """The field whose label ends closest before a value (the longest label wins at the same end)."""
    best: tuple[int, int, str] | None = None
    for name, pattern in adm.value_labels:
        for m in pattern.finditer(lead):
            key = (m.end(), m.end() - m.start(), name)
            if best is None or key[:2] > best[:2]:
                best = key
    return best[2] if best else None


def _backing_candidates(material: DocumentMaterial, spec: dict, value: Any, fragment: str) -> list[dict]:
    """The parser's own pairing of this field and value, from the SAME place in the document as the quote."""
    frag = " " + squash(fragment) + " "
    lines = [" " + squash(line) + " " for line in _source_lines(material, fragment)]
    out = []
    for cand in _matching_candidates(material, spec["name"], value, spec):
        cq = " " + squash(cand.get("quote")) + " "
        if frag.strip() and (frag in cq or cq in frag or any(cq.strip() and cq in line for line in lines)):
            out.append(cand)
    return out


def _unit_token(d, text: str, end: int) -> tuple[str | None, int]:
    m = d.unit_after.match(text, end)
    return (m.group(1), m.end()) if m else (None, end)


def _number_unit_kind(d, rule, text: str, end: int, matcher: str | None) -> tuple[str, str | None, str | None]:
    """(ok | convert | other | none, the unit written after the number, its conversion operation)."""
    unit, _ = _unit_token(d, text, end)
    if unit is None or rule is None:
        return "none", unit, None
    if matcher == "price" and unit in d.currencies:
        return "ok", unit, None
    kind, _, operation = _classify_unit(rule, unit)
    if kind == "other" and not (rule.units or rule.conversions):
        return "none", unit, None
    return kind, unit, operation


def _quote_numbers(text: str, number_words: dict[str, float], d, rule) -> list[tuple[float, int, int]]:
    """(number, start, end) of every number in the normalized text. A written number ("שלוש", "two") counts only
    when the field's own unit follows it ("שלוש שנים", "two zones"; never "two-tone" or "השני")."""
    out = []
    for m in NUMBER.finditer(text):
        raw = m.group(1)
        value = parse_number(raw)
        if value is not None:
            out.append((value, m.start(1), m.end(1)))
        if re.fullmatch(r"\d+,\d{3}", raw):                # "1,460" may also be a decimal comma elsewhere
            alt = parse_number(raw.replace(",", "."))
            if alt is not None and alt != value:
                out.append((alt, m.start(1), m.end(1)))
        if re.fullmatch(r"\d{1,3}\.\d{3}", raw):            # "2.700 mm": a European thousands separator
            out.append((float(raw.replace(".", "")), m.start(1), m.end(1)))
    warranty_units = set(d.km_units) | {normalize_term(u) for u in d.vocabulary.get("year_units") or []}
    if rule is not None and (rule.units or rule.conversions or rule.matcher == "warranty"):
        for word, value in number_words.items():
            for m in re.finditer(rf"(?<![\wא-ת-])(?:[ובהלמשכ]{{1,2}})?{re.escape(word)}(?![\wא-ת])", text):
                unit, _ = _unit_token(d, text, m.end() + (1 if text[m.end():m.end() + 1] == "-" else 0))
                if unit and (_classify_unit(rule, unit)[0] in ("ok", "convert")
                             or (rule.matcher == "warranty" and not rule.units and unit in warranty_units)):
                    out.append((value, m.start(), m.end()))
    for m in TIRE.finditer(text):                           # 205/55 R16 is a tire size, not three numbers
        out = [n for n in out if not (m.start() <= n[1] < m.end())]
    return out


@dataclass
class Entailment:
    ok: bool
    method: str | None = None
    reason: str | None = None
    position: int | None = None
    fragment: str | None = None        # the (normalized) fragment that states the value
    details: dict = field(default_factory=dict)


def _gear_words(text: str, number_words: dict[str, float]) -> str:
    """"six-speed" -> "6-speed": written gear counts in the gearbox matcher's own form."""
    for word, value in number_words.items():
        text = re.sub(rf"(?<![\wא-ת]){re.escape(word)}(?=\s*-?\s*(?:speed|spd|הילוכים|הילוכי))",
                      str(int(value)), text)
    return text


def _alias_positions(rule, text: str) -> list[tuple[int, int]]:
    return [m.span() for _, pattern, _ in (rule.aliases if rule else []) for m in pattern.finditer(text)]


def _stated_availability(adm: AdmissionContext, d, rule, text: str) -> list[bool]:
    """Availability values this text states FOR this feature: after its label in the same statement (a label-only
    line continues on the next line; another feature's label ends the statement), a negation right before the
    label, or an availability statement before the label in its own clause ("includes heated seats")."""
    vocab = d.vocabulary
    statement = compile_terms(vocab.get("availability_statement_terms") or [])
    values: list[bool] = []
    for start, end in _alias_positions(rule, text):
        rest = text[end:end + 80]
        first = rest.split("\n", 1)[0].strip(" :|=\t")
        if first in d.negative_values:                       # "Ventilated seats -", "Sunroof | ✗": the cell itself
            values.append(False)
            continue
        if not first.strip(" -"):                             # a label alone on its line: the value is below
            rest = rest.split("\n", 1)[1] if "\n" in rest else ""
        rest = rest.lstrip(" :|=-\t")
        other = adm.feature_labels.search(rest) if adm.feature_labels else None
        rest = rest[:other.start()] if other else rest
        rest = rest.split(")")[0] if rest.startswith(("(", "[")) else FEATURE_BREAK.split(rest, maxsplit=1)[0]
        stated = _stated_bool(d, rest)                        # the value written first wins
        if stated is None:
            cell = rest.strip(" :|=-\t()[]")
            parsed = _bool_value(d, cell) if cell else None
            if parsed is not None:
                stated = parsed
            elif d.negative_terms and d.negative_terms.search(rest):
                stated = (False, "absent")
            elif statement and statement.search(rest):
                stated = (True, "standard")
        if stated is not None:
            values.append(bool(stated[0]))
            continue
        before = text[max(0, start - 14):start]
        if d.negation_prefix and d.negation_prefix.search(before):
            values.append(False)
            continue
        clause_start = max([m.end() for m in FEATURE_BREAK.finditer(text) if m.end() <= start] or [0])
        lead = text[clause_start:start]
        if statement and statement.search(lead) and not (adm.feature_labels and adm.feature_labels.search(lead)):
            values.append(True)
    return values


def _entail_fragment(adm: AdmissionContext, spec: dict, rule, value: Any, fragment: str,
                     material: DocumentMaterial | None) -> Entailment:
    d = adm.dictionary
    matcher = spec.get("matcher") or (rule.matcher if rule else None)
    text = PER_100.sub(_per_100, normalize_text(fragment))
    if spec.get("value_type") == "boolean" or matcher == "boolean":
        # booleans only through the statement rules (a parse of a flattened quote can lend a neighbour's "yes")
        stated = _stated_availability(adm, d, rule, text)
        claimed = as_boolean(value)
        if stated and claimed in stated and (not claimed) not in stated:
            return Entailment(True, "stated_availability" if claimed else "stated_absence", fragment=text)
        return Entailment(False, reason="value_not_stated")
    parse_text = _gear_words(text, adm.number_words) if matcher == "gearbox" else text
    parsed = harvest_text(parse_text, adm.parse_specs, dictionary=d)
    own = [c for c in parsed if normalize_field_name(c.get("field")) == spec["name"]]
    for cand in own:
        if _same_value(spec, value, cand.get("value")):
            raw = normalize_text(str(cand.get("raw_value") or ""))
            hit = parse_text.find(raw) if raw else -1
            if hit >= 0 and matcher in NUMERIC_MATCHERS:
                # "length x width x height 4650 x 1790 x 1460 mm": the nearest label before a value names its field
                start, _ = clause_span(parse_text, hit)
                nearest = _nearest_label(adm, parse_text[start:hit])
                if nearest is not None and nearest != spec["name"]:
                    continue
            return Entailment(True, f"deterministic_parse:{cand.get('extraction_method')}",
                              position=hit if hit >= 0 else None, fragment=parse_text,
                              details={"unit": cand.get("unit")})
    if matcher == "tire_size":
        sizes = {f"{m.group(1)}/{m.group(2)} R{m.group(4)}" for m in TIRE.finditer(text)}
        claimed = {f"{m.group(1)}/{m.group(2)} R{m.group(4)}" for m in TIRE.finditer(normalize_text(str(value)))}
        if claimed and claimed <= sizes:
            m = TIRE.search(text)
            return Entailment(True, "tire_size_literal", position=m.start() if m else None, fragment=text)
        return Entailment(False, reason="value_not_in_quote" if sizes else "unsupported_inference")
    if matcher == "enum" and rule is not None:
        entry = next((p for v, p in rule.enum if normalize_term(v) == normalize_term(str(value))), None)
        hit = entry.search(text) if entry else None
        if hit:
            return Entailment(True, "enum_term", position=hit.start(), fragment=text)
        return Entailment(False, reason="value_not_in_quote")
    if matcher == "gearbox" and spec.get("component") != "count":
        return Entailment(False, reason="unsupported_inference")      # a gearbox type comes from its own parse only
    claimed_numbers = numbers_in(value)
    if not claimed_numbers:
        if matcher in NUMERIC_MATCHERS or spec.get("value_type") == "number" or matcher == "gearbox":
            return Entailment(False, reason="invalid_value_type")
        words = [t for t in _tokens(str(value)) if len(t) >= 2]
        quote_sq = " " + squash(text) + " "
        if words and (f" {' '.join(words)} " in quote_sq or all(f" {w} " in quote_sq for w in words)):
            return Entailment(True, "text_literal", fragment=text)
        return Entailment(False, reason="value_not_in_quote")
    quote_numbers = _quote_numbers(text, adm.number_words, d, rule)
    first_position, methods, units = None, [], []
    for number in claimed_numbers:
        found = None
        for q, start, end in quote_numbers:
            kind, unit, operation = _number_unit_kind(d, rule, text, end, matcher)
            if kind == "none" and rule is not None and rule.units and NOUN_AFTER.match(text, end):
                kind = "other"          # "6 speakers": a count of something else, not this field's unit
            if _close(q, number):
                if kind in ("other", "convert"):
                    found = found or ("unit", kind)
                    continue
                found = ("literal", start, unit)
                break
            if kind == "convert" and operation and _close(OPERATIONS[operation](q), number, 0.005):
                found = ("conversion:" + operation, start, None)
                break
        if found is None:
            return Entailment(False, reason="value_not_in_quote" if quote_numbers else "unsupported_inference")
        if found[0] == "unit":
            return Entailment(False, reason="unit_mismatch" if found[1] == "other" else "unit_not_normalized")
        methods.append(found[0])
        units.append(found[2])
        first_position = found[1] if first_position is None else first_position
    # the number must be stated FOR THIS FIELD: its label in the value's clause, or the parser's own pairing of this
    # field and value in this document; and not a number the quote's own parse gives to another field
    start, _ = clause_span(text, first_position)
    clause = value_clause(text, first_position)
    # The number must be stated FOR THIS FIELD. The nearest field label before it names its field
    # ("length / width / height: 4,650 / 1,790 / 1,460" gives 1,460 to height, never to width).
    nearest = _nearest_label(adm, text[start:first_position])
    # components of one compound statement share their label ("אחריות" heads years, km and the warranty text);
    # the kind / AC-DC checks below decide between them
    sibling = nearest is not None and matcher in COMPOUND_MATCHERS and adm.matcher_of.get(nearest) == matcher
    if nearest is not None and nearest != spec["name"] and not sibling:
        return Entailment(False, reason="value_belongs_to_other_field")
    # compound matchers anchor on their shared vocabulary ("אחריות: שלוש שנים", "AC charging 11 kW"), and must
    # still be THIS component: the warranty kind (vehicle vs battery) and the charging side (AC vs DC)
    anchors = {"warranty": d.warranty_terms, "charging_time": d.charging, "charging_window": d.charging,
               "numeric": d.charging if rule is not None and rule.require_context else None}.get(matcher)
    # the parser's own pairing of this field and value at the same place (a table row whose label cell the quote
    # does not repeat) already settled label, kind and AC/DC side
    backed = bool(material and _backing_candidates(material, spec, value, fragment))
    if matcher == "warranty" and not backed:
        kind = "battery" if _contains(d.warranty_battery, clause) else "vehicle"
        if kind != (rule.warranty_kind if rule is not None and rule.warranty_kind else "vehicle"):
            return Entailment(False, reason="value_belongs_to_other_field")
    if not backed and (matcher in ("charging_time", "charging_window")
                       or (rule is not None and rule.require_context and rule.positive and rule.negative)):
        if not _owner(rule, text, first_position, first_position):
            return Entailment(False, reason="value_belongs_to_other_field")
    labelled = bool(rule is None or not rule.aliases or nearest == spec["name"] or sibling
                    or _alias_positions(rule, clause)
                    or (anchors is not None and anchors.search(clause)))
    if not (labelled or backed):
        others = [c for c in parsed if normalize_field_name(c.get("field")) != spec["name"]
                  and _same_value(spec, value, c.get("value"))]
        return Entailment(False, reason="value_belongs_to_other_field" if others else "field_label_not_in_quote")
    method = "literal" if all(m == "literal" for m in methods) else \
        next(f"approved_{m}" for m in methods if m != "literal")
    currency = next((d.currencies.get(u) for u in units if u and matcher == "price" and d.currencies.get(u)), None)
    return Entailment(True, method + ("" if labelled else "+candidate_backed"), position=first_position,
                      fragment=text, details={"unit": currency})


def entail(adm: AdmissionContext, spec: dict, value: Any, quote: str,
           material: DocumentMaterial | None = None) -> Entailment:
    """Does ONE fragment of the quote state this value for this field? Deterministic; never reads a note."""
    rule = next((r for r in adm.dictionary.rules if r.name == spec["name"]), None)
    failures = []
    for fragment in quote_fragments(quote) or [quote]:
        result = _entail_fragment(adm, spec, rule, value, fragment, material)
        if result.ok:
            return result
        failures.append(result)
    return min(failures, key=lambda f: REJECTION_PRIORITY.index(f.reason) if f.reason in REJECTION_PRIORITY else 99)


def _claimed_unit_status(d, rule, spec: dict, unit: str | None, stated_currency: str | None) -> str | None:
    """None when fine, else a rejection reason for the unit the model passed."""
    if not unit or rule is None or not (rule.units or rule.conversions):
        return None
    term = normalize_term(unit)
    if rule.matcher == "price":
        code = d.currencies.get(term) or (term.upper() if term.upper() in (d.vocabulary.get("currencies") or {})
                                          else None)
        if code:
            return "unit_mismatch" if stated_currency and code != stated_currency else None
    kind, _, _ = _classify_unit(rule, term)
    if kind == "ok" or term == normalize_term(spec.get("normalized_unit") or ""):
        return None
    if kind == "convert":
        return "unit_not_normalized"
    # only a unit the dictionary knows as ANOTHER unit is a mismatch; free wording is left as written
    return "unit_mismatch" if d.unit_after.fullmatch(term) else None


def _plausible(spec: dict, value: Any) -> bool:
    low, high = spec.get("plausible_min"), spec.get("plausible_max")
    numbers = numbers_in(value)
    if (low is None and high is None) or not numbers or spec.get("matcher") not in ("numeric", "price", "warranty",
                                                                                     "gearbox"):
        return True
    if spec.get("matcher") == "warranty" and spec.get("component") not in ("years", "km"):
        return True
    return all((low is None or n >= float(low)) and (high is None or n <= float(high)) for n in numbers)


def semantic_violation(adm: AdmissionContext, spec: dict, clauses: list[str]) -> str | None:
    """The field's semantic exclusions (for this propulsion) found in the value's own clause(s)."""
    for rule in spec.get("semantic_exclusions") or []:
        applies = rule.get("applies_to")
        if applies and adm.identity.propulsion not in applies:
            continue
        pattern = compile_terms(rule.get("terms") or [])
        if pattern and any(pattern.search(normalize_text(c)) for c in clauses if c):
            return rule.get("reason") or "semantic exclusion"
    return None


# --- dates ---------------------------------------------------------------------------------------------

def _date_parts(value: str) -> tuple[int, int | None, int | None] | None:
    text = str(value or "").strip()
    for pattern, order in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"), (r"^(\d{4})-(\d{1,2})$", "ym"),
                           (r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", "dmy"), (r"^(\d{1,2})[/.](\d{4})$", "my"),
                           (r"^(\d{4})$", "y")):
        m = re.match(pattern, text)
        if m:
            g = [int(x) for x in m.groups()]
            parts = dict(zip(order, g))
            y, mo, dd = parts.get("y"), parts.get("m"), parts.get("d")
            if y and 1990 <= y <= 2100 and (mo is None or 1 <= mo <= 12) and (dd is None or 1 <= dd <= 31):
                return y, mo, dd
    return None


def date_in_source(value: str, places: list[str]) -> str | None:
    """ISO form of a model-supplied date when the quote or its own source lines state that date; else None.
    (A whole-document search would find the model year in any title or footer.)"""
    parts = _date_parts(value)
    if parts is None:
        return None
    y, mo, dd = parts
    text = normalize_text("\n".join(p for p in places if p))
    patterns = []
    if mo is None:
        patterns.append(rf"(?<!\d){y}(?!\d)")
    else:
        names = [EN_MONTHS[mo - 1], EN_MONTHS[mo - 1][:3], HEBREW_MONTHS[mo - 1]]
        if dd is None:
            patterns += [rf"(?<!\d){y}-0?{mo}(?!\d)", rf"(?<![\d/.])0?{mo}[/.]{y}(?!\d)"]
            patterns += [rf"{re.escape(n)}\.?,?\s*{y}(?!\d)" for n in names]
        else:
            patterns += [rf"(?<!\d){y}-0?{mo}-0?{dd}(?!\d)", rf"(?<![\d/.])0?{dd}[/.]0?{mo}[/.]{y}(?!\d)"]
            patterns += [rf"(?<!\d)0?{dd}\s+(?:ב)?{re.escape(n)}\.?,?\s*{y}(?!\d)" for n in names]
            patterns += [rf"{re.escape(n)}\.?\s+0?{dd},?\s*{y}(?!\d)" for n in names[:2]]
    if any(re.search(p, text) for p in patterns):
        return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(x for x in (y, mo, dd) if x is not None))
    return None


# --- the gate ------------------------------------------------------------------------------------------

def _reject(reasons: list[str], **extra: Any) -> dict:
    return {"accepted": False, "reasons": reasons,
            "message": " ".join(REASON_TEXT.get(r, r) for r in reasons), **extra}


def _matching_candidates(material: DocumentMaterial, name: str, value: Any, spec: dict) -> list[dict]:
    return [c for c in material.candidates
            if normalize_field_name(c.get("field")) == name and _same_value(spec, value, c.get("value"))]


def _line_index(material: DocumentMaterial, fragment: str) -> int | None:
    probe = " " + squash(fragment) + " "
    if probe.strip() == "":
        return None
    for index, (_, squashed) in enumerate(material.lines):
        if probe in squashed or squashed in probe and len(squashed) > 8:
            return index
    return None


def _source_lines(material: DocumentMaterial, fragment: str, around: int = 0) -> list[str]:
    """The document line(s) holding a quote fragment (whole tokens: "596 litres" is not inside "1,596 litres"),
    optionally with `around` neighbouring lines. A fragment found only in page source / structured data (not a
    visible line) gets the surrounding window of that source instead, so it keeps its context."""
    index = _line_index(material, fragment)
    if index is not None:
        lo, hi = max(0, index - around), min(len(material.lines), index + around + 1)
        return [orig for orig, _ in material.lines[lo:hi]]
    probe = squash(fragment)
    pos = material.haystack.find(" " + probe + " ") if probe else -1
    return [material.haystack[max(0, pos - 80):pos + len(probe) + 40]] if pos >= 0 else []   # its own object


def _section_headings(material: DocumentMaterial, fragment: str, adm: AdmissionContext) -> list[str]:
    """The nearest heading-like line above the fragment's line, within a few lines ("2.0 Hybrid", "Electric motor"
    above "Max. power 95 hp" / "Max. torque 185 Nm"): short, no value with a unit, not a menu."""
    index = _line_index(material, fragment)
    if not index:
        return []
    for orig, _ in material.lines[max(0, index - 6):index][::-1]:
        norm = normalize_text(orig)
        if any(adm.dictionary.unit_after.match(norm, m.end()) for m in NUMBER.finditer(norm)):
            continue                                   # another value row of the same section
        menu = bool(re.search(r"[|•·]", norm)) or not about_target(orig, adm.identity).strip()
        return [orig] if len(norm) <= 40 and not menu else []
    return []


def _line_context(line: str, value: Any) -> str:
    """A source line without the bracket groups that hold OTHER numbers ("596 litres (581 for the 2.0)")."""
    norm = normalize_text(line)
    numbers = numbers_in(value)

    def keep(group: re.Match) -> str:
        inside = [parse_number(m.group(1)) for m in NUMBER.finditer(group.group(0))]
        if not inside or any(n is not None and any(_close(n, v) for v in numbers) for n in inside):
            return group.group(0)
        return " "
    return PAREN_GROUP.sub(keep, norm)


def _clause_in_line(line: str, value: Any) -> str:
    """The clause of a source line around the (first) number of the value; the whole line otherwise."""
    norm = PER_100.sub(_per_100, normalize_text(line))
    for number in numbers_in(value)[:1]:
        for m in NUMBER.finditer(norm):
            parsed = parse_number(m.group(1))
            if parsed is not None and _close(parsed, number):
                return semantic_clause(norm, m.start(1))
    return norm


def _text_arg(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " … ".join(str(v) for v in value if v not in (None, ""))
    return "" if value is None else str(value)


# --- the binding of one fact (shared by admit() and src/binding_replay.py) -------------------------------------------

@dataclass
class FactContext:
    """Where in the document a quote states its value: the raw material of the fact's binding layers."""
    fragment: str                      # the (normalized) quote fragment that states the value
    clause: str                        # the value's own words in that fragment
    source_lines: list[str]            # the document line(s) holding the fragment
    headings: list[str]                # the section heading above it


def fact_context(adm: AdmissionContext, material: DocumentMaterial, quote: str, entailment: Entailment) -> FactContext:
    fragment = entailment.fragment or normalize_text(quote)
    return FactContext(fragment=fragment, clause=semantic_clause(fragment, entailment.position),
                       source_lines=_source_lines(material, fragment),
                       headings=_section_headings(material, fragment, adm))


def evidence_market(material: DocumentMaterial, model_market: Any) -> tuple[str, str | None]:
    """(market, market_basis) of a fact: the source's own market. A source that does not establish one gives
    "unknown": the model's market is a claim (kept as model_market_claim), never the evidence market, whichever market
    it names (a claim must not manufacture foreign or IL provenance)."""
    if material.market is not None:
        return material.market, material.market_basis
    return "unknown", "unverified_model_claim" if normalize_market(model_market) else "not_determinable"


def binding_layers(material: DocumentMaterial, name: str, spec: dict, value: Any, quote: str, ctx: FactContext,
                   variant_text: str = "", candidates: list[dict] | None = None) -> dict:
    """The context layers server-side binding reads for one fact, most specific first: the value's own words, the
    table column, the stating fragment, its source line (other numbers' bracket groups removed), the section heading
    above; the model's variant text and the quote's OTHER fragments can only veto. `matching`: the deterministic
    candidates of the same document, field and value (`candidates`, default the document's own harvest) whose table
    column header / identity feed the column layers. Pure."""
    pool = material.candidates if candidates is None else candidates
    matching = [c for c in pool if normalize_field_name(c.get("field")) == name and _same_value(spec, value,
                                                                                                c.get("value"))]
    hints = [h for c in matching for h in [c.get("variant_hint")] + list(c.get("variant_hints") or []) if h]
    # the value's table column identity (header + that column's power / drivetrain / ... cells); a value found in two
    # or more columns (or in a column that cannot be identified) names no single column, so the layer is left out
    # (fail-closed: the document decides)
    columns = list(dict.fromkeys(i for c in matching
                                 for i in [c.get("column_identity")] + list(c.get("column_identities") or []) if i))
    if any(c.get("column_identity_unknown") for c in matching):
        columns = []
    fragment, clause = ctx.fragment, ctx.clause
    other_fragments = [f for f in quote_fragments(quote) if normalize_text(f) != fragment]
    layers = [("value_clause", clause if clause != fragment else ""),
              ("column_header", " | ".join(dict.fromkeys(hints))),
              ("column_identity", columns[0] if len(columns) == 1 else ""), ("quote", _line_context(fragment, value)),
              ("source_line", " ".join(_line_context(line, value) for line in ctx.source_lines)),
              ("section_heading", " ".join(ctx.headings))]
    veto_layers = [("model_variant", variant_text), *[("other_quote_fragment", f) for f in other_fragments]]
    return {"layers": layers, "veto_layers": veto_layers, "matching": matching, "columns": columns}


def fact_binding(adm: AdmissionContext, material: DocumentMaterial, name: str, spec: dict, value: Any, quote: str,
                 ctx: FactContext, *, variant_text: str = "", claim: str | None = None, market: str | None = None,
                 candidates: list[dict] | None = None) -> tuple[dict, dict]:
    """(binding, layer inputs) of one fact: src/document_binding.bind over binding_layers(). Pure; the one place both
    Evidence Admission and Binding Replay compute a fact's binding, so the two cannot drift."""
    inputs = binding_layers(material, name, spec, value, quote, ctx, variant_text, candidates)
    profile = material.profile
    binding = bind(adm.identity, profile["statuses"], inputs["layers"], inputs["veto_layers"], market=market,
                   requirement=spec.get("binding_requirement"), model_declared_different=claim == "different",
                   trim_named_in_document=profile.get("trim_named_in_document", False),
                   source_authority=material.authority.get("source_authority"),
                   document_names_family=profile.get("zone_statuses", {}).get("model") == "match",
                   other_trims_named=profile.get("other_trims_named"))
    year = profile.get("year_context") or {}
    if year.get("statements") or year.get("ignored"):
        # telemetry only: the model-year statements behind the year dimension and the years the rules ignored
        binding["year_context"] = year
    return binding, inputs


def admit(adm: AdmissionContext, cache, args: dict, run_documents: list[str] | tuple = ()) -> dict:
    """{'accepted': True, 'record': {...}} or {'accepted': False, 'reasons': [...], 'message': ...}."""
    name = normalize_field_name(args.get("field"))
    spec = adm.spec(name)
    value = args.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        return _reject(["value_missing"])
    material = adm.material(cache, args.get("document_id"), args.get("source_url"), run_documents)
    if material is None:
        return _reject(["source_not_retrieved"])
    reject = lambda reasons, **extra: _reject(reasons, document_id=material.document_id, **extra)  # noqa: E731
    if spec.get("applicable") is False:
        return reject(["field_not_applicable_for_vehicle"])
    if isinstance(value, dict) or (isinstance(value, (list, tuple)) and spec.get("value_type") != "text"):
        return reject(["invalid_value_type"])
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    if spec.get("value_type") == "boolean":
        parsed = as_boolean(value)
        if parsed is None:
            return reject(["invalid_value_type"])
        value = parsed
    unit = _text_arg(args.get("unit")).strip() or None
    checks: dict[str, Any] = {"provenance": "document_in_store"}
    quote = _text_arg(args.get("quote")).strip()
    if not quote:
        cands = _matching_candidates(material, name, value, spec)
        if not cands:
            return reject(["quote_missing"])
        quote = str(cands[0].get("quote") or "")
        checks["quote"] = "from_deterministic_candidate"
    tokens = _tokens(quote)
    if (len(tokens) < MIN_QUOTE_TOKENS or len(squash(quote)) < MIN_QUOTE_CHARS
            or not any(re.search(r"[^\W\d_]", t) for t in tokens)):       # a bare number identifies nothing
        return reject(["quote_too_short"])
    if not quote_in_source(material, quote):
        return reject(["quote_not_in_source"])
    checks.setdefault("quote", "verbatim_in_source")
    entailment = entail(adm, spec, value, quote, material)
    if not entailment.ok:
        return reject([entailment.reason or "value_not_in_quote"])
    checks["entailment"] = entailment.method
    rule = next((r for r in adm.dictionary.rules if r.name == name), None)
    stated_unit = (entailment.details or {}).get("unit")
    unit_problem = _claimed_unit_status(adm.dictionary, rule, spec, unit,
                                        stated_unit if spec.get("matcher") == "price" else None)
    if unit_problem:
        return reject([unit_problem])
    if not _plausible(spec, value):
        return reject(["implausible_value"])
    ctx = fact_context(adm, material, quote, entailment)
    fragment, clause, source_lines, headings = ctx.fragment, ctx.clause, ctx.source_lines, ctx.headings
    line_clauses = [_clause_in_line(line, value) for line in source_lines]
    violation = semantic_violation(adm, spec, [clause, *line_clauses,
                                               *[h for h in headings if not re.search(r"\d", h)]])
    if violation:
        return reject(["semantic_mismatch"], semantic_note=violation)
    checks["semantics"] = "ok"

    # identity: server-side binding (the model's variant text can only veto)
    claim = str(args.get("variant_match") or "").strip().lower() or None
    model_market = args.get("market")
    market, market_basis = evidence_market(material, model_market)
    binding, _ = fact_binding(adm, material, name, spec, value, quote, ctx, variant_text=_text_arg(args.get("variant")),
                              claim=claim, market=market)

    # the unit: the model's, else what the source wrote (a USD price stays USD), else the field's unit
    record_unit = unit or stated_unit or spec.get("normalized_unit")
    typed = typed_value(value, unit=record_unit, value_type=spec.get("value_type"), matcher=spec.get("matcher"),
                        condition=_text_arg(args.get("condition")) or None)
    condition = typed.get("condition") if isinstance(typed, dict) else None
    if condition:
        cond_tokens = _tokens(condition)
        quote_sq = " " + squash(quote) + " "
        if not cond_tokens or not all(f" {t} " in quote_sq for t in cond_tokens):
            checks["condition"] = "dropped_not_in_quote"
            typed["condition"] = condition = None
    # time: a date stated in the quote or its own source lines, else the document's publication metadata
    temporal: dict[str, Any] = {}
    date_places = [quote, *_source_lines(material, fragment, around=1)]
    for key in ("valid_as_of", "valid_from", "valid_to"):
        if args.get(key):
            iso = date_in_source(_text_arg(args[key]), date_places)
            if iso:
                temporal[key] = iso
            else:
                checks[key] = "dropped_not_in_source"
    if temporal:
        temporal["temporal_basis"] = "stated_in_source"
    elif spec.get("time_sensitive") and material.source_date:
        temporal = {"valid_as_of": material.source_date, "temporal_basis": material.source_date_basis}
    if spec.get("time_sensitive"):
        temporal["temporal_status"] = "dated" if temporal.get("valid_as_of") or temporal.get("valid_from") \
            else "undated"

    record = {
        "field": name, "value": value, "unit": record_unit,
        "source_url": material.url, "document_id": material.document_id, "quote": quote,
        "market": market, "variant": _text_arg(args.get("variant")) or None,
        "variant_match": binding["variant_match"],
        "note": _text_arg(args.get("note")) or None, "condition": condition, "typed_value": typed,
        "admission_status": "accepted", "admission_version": ADMISSION_VERSION, "admission_checks": checks,
        "entailment": entailment.method,
        "binding_level": binding["binding_level"], "binding_requirement": binding["binding_requirement"],
        "binding_veto": binding["binding_veto"] or None, "binding_dimensions": binding["binding_dimensions"],
        "binding_basis": binding.get("binding_basis"), "year_context": binding.get("year_context"),
        "model_variant_claim": claim,
        "market_basis": market_basis,
        "model_market_claim": model_market if model_market and normalize_market(model_market) != market else None,
        "source_authority": material.authority.get("source_authority"),
        "authority_basis": material.authority.get("authority_basis"),
        "source_domain": material.authority.get("source_domain"),
        "source_date": material.source_date, "source_date_basis": material.source_date_basis,
        "observed_at": material.meta.get("fetched_at"),
        **temporal,
    }
    claimed_url = args.get("source_url")
    if claimed_url and args.get("document_id") and str(claimed_url).split("#")[0] != str(material.url):
        record["model_source_url_claim"] = claimed_url
    return {"accepted": True, "record": {k: v for k, v in record.items() if v not in (None, "", [], {})}}

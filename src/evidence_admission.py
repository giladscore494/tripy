"""Evidence Admission Gate: a store_evidence request becomes Evidence only if deterministic checks pass.

    store_evidence(field, value, quote, document_id | source_url, market?, variant?, variant_match?, note?, ...)
            ↓
    1. provenance      the cited document must be RETRIEVED (in the document cache). A URL never fetched, a
                       search snippet or the model's memory is not a source.
    2. applicability   the field must apply to this vehicle (schema applies_to by propulsion).
    3. quote           the quote must occur in that document (normalized: case, punctuation, whitespace,
                       reversed-Hebrew PDF lines; "…" joins verbatim fragments). With no quote, a deterministic
                       candidate of the same document, field and value supplies it.
    4. entailment      the QUOTE must state the value: a deterministic parse of the quote with the field's own
                       dictionary rule (booleans need a stated value, gearbox/tire/pattern forms), or the number
                       literally (written numbers included), or a schema-approved unit conversion. Nothing else:
                       "e-CVT" does not state a gear count of 1; a note never states anything.
    5. semantics       unit, plausibility range, and the field's semantic exclusions for this propulsion
                       (a hybrid's torque field means engine torque, not system torque).
    6. identity        server-side binding (src/document_binding.py) -> binding_level, variant_match; market
                       from the source itself (src/source_authority.py); the model's own variant_match and
                       market are kept only as model_variant_claim / model_market_claim.
    7. authority       source_authority from deterministic source rules.
    8. typing & time   typed_value (scalar / range / boolean / enum / text / compound), a condition only when
                       the quote states it, valid_as_of only from a date the source states or the document's
                       own publication metadata (never invented).
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
from dataclasses import dataclass, field
from typing import Any

from .candidate_harvest import (NUMBER, TIRE, _classify_unit, compile_terms, dictionary_for, harvest_document,
                                harvest_text, normalize_term, normalize_text, parse_number, reverse_hebrew_line)
from .document_binding import TargetIdentity, bind, document_profile, target_identity
from .fields import harvest_vocabulary, load_schema, normalize_field_name, resolve_requested_fields
from .source_authority import classify_source, normalize_market, source_market
from .typed_values import as_boolean, numbers_in, typed_value

ADMISSION_VERSION = "admission-v1"
MIN_QUOTE_TOKENS, MIN_QUOTE_CHARS = 2, 4
NUMERIC_MATCHERS = ("numeric", "price")
REASON_TEXT = {
    "source_not_retrieved": "The cited document is not in the document store. Fetch it (fetch_url / fetch_pdf) and "
                            "cite its document_id; search results and memory are not sources.",
    "field_not_applicable_for_vehicle": "This field does not apply to this vehicle's propulsion; report "
                                        "not_applicable instead.",
    "quote_missing": "Give a short verbatim quote from the document that states the value.",
    "quote_too_short": "The quote is too short to identify what it states; quote the label with the value.",
    "quote_not_in_source": "The quote does not occur in the cited document. Quote the document verbatim.",
    "value_missing": "No value given.",
    "invalid_value_type": "The value does not have the field's type.",
    "unsupported_inference": "The quote does not state this value; it would be an inference. Store only what the "
                             "source states (or report the field unresolved / not_applicable).",
    "value_not_in_quote": "The quote does not contain this value.",
    "value_not_stated": "The quote names the feature but does not state its availability (a label alone is not "
                        "'true').",
    "unit_mismatch": "The value's unit is not the field's unit.",
    "unit_not_normalized": "Convert the value to the field's unit before storing it.",
    "implausible_value": "The value is outside the field's plausible range (wrong unit or wrong quantity?).",
    "semantic_mismatch": "The quote describes a different quantity than this field means (see semantic_definition).",
}
HEBREW_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר",
                 "דצמבר"]
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]
CLAUSE_BREAK = re.compile(r"[;\n|•]|\.\s(?!\d)|,\s(?!\d)")
DATE_KEYS = (("json_ld", "dateModified"), ("meta", "article:modified_time"), ("meta", "og:updated_time"),
             ("json_ld", "datePublished"), ("meta", "article:published_time"), ("meta", "date"),
             ("pdf", "ModDate"), ("pdf", "CreationDate"))


def squash(text: Any) -> str:
    """Comparison form for quotes: normalized characters, lowercase, every non-word run is one space."""
    return re.sub(r"[^\w]+", " ", normalize_text(str(text or ""))).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in squash(text).split() if t]


# --- per-document material --------------------------------------------------------------------------------

@dataclass
class DocumentMaterial:
    document_id: str
    url: str | None
    meta: dict
    haystack: str                      # squashed text + tag-stripped source + structured data (+ reversed lines)
    lines: list[tuple[str, str]]       # (original line, squashed line)
    profile: dict
    market: str | None
    market_basis: str | None
    authority: dict
    source_date: str | None
    source_date_basis: str | None
    candidates: list[dict]
    date_text: str                     # normalized text for date lookups


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


class AdmissionContext:
    """Everything the gate needs for ONE vehicle run: target identity, requested field specs, target market,
    and a per-document material cache (haystack, binding profile, market, authority) computed once."""

    def __init__(self, *, identity: TargetIdentity, specs: list[dict], target_market: str = "IL",
                 manufacturer: str | None = None):
        self.identity = identity
        self.specs = {s["name"]: s for s in specs}
        self.all_specs = list(specs)
        # Entailment parses a quote with the WHOLE dictionary (shared vocabularies such as gearbox types come from
        # other fields' rules), the run's own specs overriding same-name defaults.
        merged = {s["name"]: s for s in load_schema()}
        merged.update(self.specs)
        self.parse_specs = list(merged.values())
        self.target_market = normalize_market(target_market) or "IL"
        self.manufacturer = manufacturer or identity.manufacturer
        self._docs: dict[str, DocumentMaterial] = {}
        self._lock = threading.Lock()
        vocab = harvest_vocabulary()
        self.number_words = {normalize_term(w): float(n) for n, words in (vocab.get("number_words") or {}).items()
                             for w in words}

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
        return self.specs.get(name) or {"name": name}

    def material(self, cache, document_id: str | None, source_url: str | None,
                 run_documents: list[str] | tuple = ()) -> DocumentMaterial | None:
        """The cited document: by id, else by URL (exact cache key of any fetch kind, else a document this run
        touched whose final URL it is). Never a scan of the whole shared cache."""
        from .storage.cache import document_id_for

        meta = cache.get(document_id) if document_id else None
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
        material = self._build(cache, meta)
        with self._lock:
            self._docs[doc_id] = material
        return material

    def _build(self, cache, meta: dict) -> DocumentMaterial:
        from .tools.extract import document_structured

        doc_id = meta["document_id"]
        url = meta.get("final_url") or meta.get("url")
        text = cache.read_text(doc_id)
        parts = [text]
        structured = None
        if meta.get("doc_type") == "html" or meta.get("kind") == "rendered":
            html = cache.read_body(doc_id).decode("utf-8", errors="replace")
            parts.append(re.sub(r"<[^>]+>", " ", html))
            try:
                structured = document_structured(cache, doc_id, html)
                parts.append(json.dumps(structured, ensure_ascii=False))
            except Exception:  # structured data is a convenience; the text is enough
                structured = None
        if meta.get("doc_type") == "pdf":
            parts.append("\n".join(reverse_hebrew_line(line) for line in text.splitlines()))
        raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        market, basis = source_market(url, text)
        source_date, date_basis = _document_date(meta, structured)
        try:
            candidates, _ = harvest_document(cache, doc_id, self.all_specs)
        except Exception:  # the parser never blocks admission; quotes are checked against the text
            candidates = []
        return DocumentMaterial(
            document_id=doc_id, url=url, meta=meta, haystack=" " + " ".join(squash(p) for p in parts) + " ",
            lines=[(ln, squash(ln)) for ln in raw_lines],
            profile=document_profile(text=text, title=meta.get("title"), url=url, identity=self.identity),
            market=market, market_basis=basis, authority=classify_source(url, self.manufacturer),
            source_date=source_date, source_date_basis=date_basis, candidates=candidates,
            date_text=normalize_text(" ".join(parts[:2])))


# --- checks ---------------------------------------------------------------------------------------------

def _quote_parts(quote: str) -> list[str]:
    return [p for p in (squash(x) for x in re.split(r"…|\.\.\.|\[\.\.\.\]", quote)) if p]


def quote_in_source(material: DocumentMaterial, quote: str) -> bool:
    """Every verbatim fragment of the quote occurs in the document (or in a quote the deterministic parser cut
    from this same document)."""
    parts = _quote_parts(quote)
    if not parts:
        return False
    if all(f" {p} " in material.haystack for p in parts):
        return True
    cand_quotes = [" " + squash(c.get("quote")) + " " for c in material.candidates if c.get("quote")]
    return all(any(f" {p} " in cq for cq in cand_quotes) for p in parts)


def _quote_numbers(quote_norm: str, number_words: dict[str, float]) -> list[tuple[float, int, int]]:
    """(number, start, end) of every number in the normalized quote, written numbers included."""
    out = []
    for m in NUMBER.finditer(quote_norm):
        value = parse_number(m.group(1))
        if value is not None:
            out.append((value, m.start(1), m.end(1)))
        if re.fullmatch(r"\d+,\d{3}", m.group(1)):        # "1,460" may also be a decimal comma elsewhere; keep both
            alt = parse_number(m.group(1).replace(",", "."))
            if alt is not None and alt != value:
                out.append((alt, m.start(1), m.end(1)))
    for word, value in number_words.items():
        for m in re.finditer(rf"(?<![\wא-ת])(?:[ובהלמשכ]{{1,2}})?{re.escape(word)}(?![\wא-ת])", quote_norm):
            out.append((value, m.start(), m.end()))
    for m in TIRE.finditer(quote_norm):                  # 205/55 R16 is a tire size, not three numbers
        out = [n for n in out if not (m.start() <= n[1] < m.end())]
    return out


def _close(a: float, b: float, rel: float = 1e-9) -> bool:
    return abs(a - b) <= max(1e-6, rel * abs(b))


def _same_value(spec: dict, claimed: Any, candidate: Any) -> bool:
    if isinstance(claimed, bool) or isinstance(candidate, bool):
        return as_boolean(claimed) is not None and as_boolean(claimed) == as_boolean(candidate)
    a, b = numbers_in(claimed), numbers_in(candidate)
    if a or b:
        return len(a) == len(b) and all(_close(x, y, 1e-6) for x, y in zip(sorted(a), sorted(b)))
    return squash(claimed) == squash(candidate) or normalize_term(str(claimed)) == normalize_term(str(candidate))


def value_clause(quote_norm: str, position: int | None) -> str:
    if position is None:
        return quote_norm
    start = 0
    for m in CLAUSE_BREAK.finditer(quote_norm, 0, position):
        start = m.end()
    end_match = CLAUSE_BREAK.search(quote_norm, position)
    return quote_norm[start:end_match.start() if end_match else len(quote_norm)]


@dataclass
class Entailment:
    ok: bool
    method: str | None = None
    reason: str | None = None
    position: int | None = None
    candidate: dict | None = None
    details: dict = field(default_factory=dict)


def entail(adm: AdmissionContext, spec: dict, value: Any, quote: str) -> Entailment:
    """Does the quote STATE this value for this field? Deterministic; never reads a note."""
    d = dictionary_for(adm.parse_specs)
    rule = next((r for r in d.rules if r.name == spec["name"]), None)
    matcher = spec.get("matcher") or (rule.matcher if rule else None)
    quote_norm = normalize_text(quote)
    for cand in harvest_text(quote, adm.parse_specs, dictionary=d):
        if normalize_field_name(cand.get("field")) == spec["name"] and _same_value(spec, value, cand.get("value")):
            hit = re.search(re.escape(normalize_text(str(cand.get("raw_value") or ""))), quote_norm) \
                if cand.get("raw_value") else None
            return Entailment(True, f"deterministic_parse:{cand.get('extraction_method')}",
                              position=hit.start() if hit else None, candidate=cand)
    if spec.get("value_type") == "boolean" or matcher == "boolean":
        return _boolean_statement(adm, d, spec, value, quote, quote_norm)
    if matcher == "gearbox":
        return Entailment(False, reason="unsupported_inference")
    if matcher == "tire_size":
        sizes = {f"{m.group(1)}/{m.group(2)} R{m.group(4)}" for m in TIRE.finditer(quote_norm)}
        claimed = {f"{m.group(1)}/{m.group(2)} R{m.group(4)}" for m in TIRE.finditer(normalize_text(str(value)))}
        if claimed and claimed <= sizes:
            m = TIRE.search(quote_norm)
            return Entailment(True, "tire_size_literal", position=m.start() if m else None)
        return Entailment(False, reason="value_not_in_quote" if sizes else "unsupported_inference")
    if matcher == "enum" and rule is not None:
        entry = next((p for v, p in rule.enum if normalize_term(v) == normalize_term(str(value))), None)
        hit = entry.search(quote_norm) if entry else None
        if hit:
            return Entailment(True, "enum_term", position=hit.start())
        return Entailment(False, reason="value_not_in_quote")
    claimed_numbers = numbers_in(value)
    quote_numbers = _quote_numbers(quote_norm, adm.number_words)
    if claimed_numbers:
        first_position = None
        methods = []
        for number in claimed_numbers:
            found = None
            for q, start, end in quote_numbers:
                if _close(q, number):
                    unit_status = _unit_after(d, rule, quote_norm, end, matcher)
                    if unit_status in ("other", "convert"):
                        found = ("unit", unit_status)
                        continue
                    found = ("literal", start)
                    break
                if rule is not None and rule.conversions:
                    unit, _ = _unit_token(d, quote_norm, end)
                    kind, _, operation = _classify_unit(rule, unit)
                    if kind == "convert" and operation:
                        from .candidate_harvest import OPERATIONS
                        if _close(OPERATIONS[operation](q), number, 0.005):
                            found = ("conversion:" + operation, start)
                            break
            if found is None:
                return Entailment(False, reason="value_not_in_quote" if quote_numbers else "unsupported_inference")
            if found[0] == "unit":
                return Entailment(False, reason="unit_mismatch" if found[1] == "other" else "unit_not_normalized")
            methods.append(found[0])
            first_position = found[1] if first_position is None else first_position
        method = "literal" if all(m == "literal" for m in methods) else \
            next(f"approved_{m}" for m in methods if m != "literal")
        return Entailment(True, method, position=first_position)
    if matcher in NUMERIC_MATCHERS or spec.get("value_type") == "number":
        return Entailment(False, reason="invalid_value_type")
    words = [t for t in _tokens(str(value)) if len(t) >= 2]
    quote_sq = " " + squash(quote) + " "
    if words and (f" {' '.join(words)} " in quote_sq or all(f" {w} " in quote_sq for w in words)):
        return Entailment(True, "text_literal")
    return Entailment(False, reason="value_not_in_quote")


def _boolean_statement(adm: AdmissionContext, d, spec: dict, value: Any, quote: str, quote_norm: str) -> Entailment:
    """A boolean the field's own parse did not produce: admitted only when the quote EXPLICITLY states availability
    in the claimed direction (an affirmative / availability term for true, a negative value for false), with no
    term of the other direction and no contradicting parse of this field. The label-to-field mapping is the model's
    judgment (as for a number); a label alone never states `true`."""
    claimed = as_boolean(value)
    parsed = [c for c in harvest_text(quote, adm.parse_specs, dictionary=d)
              if normalize_field_name(c.get("field")) == spec["name"]]
    if parsed:                                   # the field's own parse read the quote differently
        return Entailment(False, reason="value_not_stated")
    words = " " + squash(quote) + " "
    symbols = set(quote_norm)

    def stated(terms) -> bool:
        for term in terms:
            if len(term) == 1 and not term.isalnum():
                if term in symbols:
                    return True
            elif len(term) > 1 and f" {squash(term)} " in words and squash(term):
                return True
        return False

    vocab = d.vocabulary
    positive = stated(set(d.affirmative) | {normalize_term(t) for t in vocab.get("availability_statement_terms") or []})
    negative = stated(set(d.negative_values) | {normalize_term(t) for t in vocab.get("negation_prefixes") or []})
    if claimed is True and positive and not negative:
        return Entailment(True, "stated_availability")
    if claimed is False and negative and not positive:
        return Entailment(True, "stated_absence")
    return Entailment(False, reason="value_not_stated")


def _unit_token(d, quote_norm: str, end: int) -> tuple[str | None, int]:
    m = d.unit_after.match(quote_norm, end)
    return (m.group(1), m.end()) if m else (None, end)


def _unit_after(d, rule, quote_norm: str, end: int, matcher: str | None) -> str:
    """ok | convert | other | none for the unit written right after a number in the quote."""
    if rule is None:
        return "none"
    unit, _ = _unit_token(d, quote_norm, end)
    if unit is None:
        return "none"
    if matcher == "price" and unit in d.currencies:
        return "ok"
    if matcher == "warranty":
        return "ok"
    kind, _, _ = _classify_unit(rule, unit)
    if kind == "other" and not (rule.units or rule.conversions):
        return "none"
    return kind


def _claimed_unit_status(d, rule, spec: dict, unit: str | None) -> str | None:
    """None when fine, else a rejection reason for the unit the model passed."""
    if not unit or rule is None or not (rule.units or rule.conversions):
        return None
    term = normalize_term(unit)
    if rule.matcher == "price" and (term in d.currencies or term.upper() in (d.vocabulary.get("currencies") or {})):
        return None
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


def semantic_violation(adm: AdmissionContext, spec: dict, clause: str) -> str | None:
    """The field's semantic exclusions (for this propulsion) found in the value's own clause."""
    for rule in spec.get("semantic_exclusions") or []:
        applies = rule.get("applies_to")
        if applies and adm.identity.propulsion not in applies:
            continue
        pattern = compile_terms(rule.get("terms") or [])
        if pattern and pattern.search(normalize_text(clause)):
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


def date_in_source(value: str, material: DocumentMaterial) -> str | None:
    """ISO form of a model-supplied date when the document itself states that date; else None."""
    parts = _date_parts(value)
    if parts is None:
        return None
    y, mo, dd = parts
    text = material.date_text
    patterns = []
    if mo is None:
        patterns.append(rf"(?<!\d){y}(?!\d)")
    else:
        months = [rf"0?{mo}"]
        names = [EN_MONTHS[mo - 1], EN_MONTHS[mo - 1][:3], HEBREW_MONTHS[mo - 1]]
        if dd is None:
            patterns += [rf"(?<!\d){y}-0?{mo}(?!\d)", rf"(?<![\d/.]){months[0]}[/.]{y}(?!\d)"]
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


def _source_line(material: DocumentMaterial, quote: str) -> str | None:
    parts = _quote_parts(quote)
    if not parts:
        return None
    probe = max(parts, key=len)
    return next((orig for orig, sq in material.lines if probe in sq), None)


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
    if spec.get("applicable") is False:
        return _reject(["field_not_applicable_for_vehicle"], document_id=material.document_id)
    if spec.get("value_type") == "boolean":
        parsed = as_boolean(value)
        if parsed is None:
            return _reject(["invalid_value_type"], document_id=material.document_id)
        value = parsed
    checks: dict[str, Any] = {"provenance": "document_in_store"}
    quote = str(args.get("quote") or "").strip()
    if not quote:
        cands = _matching_candidates(material, name, value, spec)
        if not cands:
            return _reject(["quote_missing"], document_id=material.document_id)
        quote = str(cands[0].get("quote") or "")
        checks["quote"] = "from_deterministic_candidate"
    tokens = _tokens(quote)
    if (len(tokens) < MIN_QUOTE_TOKENS or len(squash(quote)) < MIN_QUOTE_CHARS
            or not any(re.search(r"[^\W\d_]", t) for t in tokens)):       # a bare number identifies nothing
        return _reject(["quote_too_short"], document_id=material.document_id)
    if not quote_in_source(material, quote):
        return _reject(["quote_not_in_source"], document_id=material.document_id)
    checks.setdefault("quote", "verbatim_in_source")
    entailment = entail(adm, spec, value, quote)
    if not entailment.ok:
        return _reject([entailment.reason or "value_not_in_quote"], document_id=material.document_id)
    checks["entailment"] = entailment.method
    d = dictionary_for(adm.parse_specs)
    rule = next((r for r in d.rules if r.name == name), None)
    unit = args.get("unit")
    unit_problem = _claimed_unit_status(d, rule, spec, unit)
    if unit_problem:
        return _reject([unit_problem], document_id=material.document_id)
    if not _plausible(spec, value):
        return _reject(["implausible_value"], document_id=material.document_id)
    quote_norm = normalize_text(quote)
    clause = value_clause(quote_norm, entailment.position)
    violation = semantic_violation(adm, spec, clause)
    if violation:
        return _reject(["semantic_mismatch"], document_id=material.document_id, semantic_note=violation)
    checks["semantics"] = "ok"

    # identity: server-side binding (the model's variant text can only veto)
    hints = [h for c in _matching_candidates(material, name, value, spec)
             for h in [c.get("variant_hint")] + list(c.get("variant_hints") or []) if h]
    claim = str(args.get("variant_match") or "").strip().lower() or None
    model_market = args.get("market")
    market, market_basis = material.market, material.market_basis
    if market is None:
        claimed = normalize_market(model_market)
        if claimed and claimed != adm.target_market:
            market, market_basis = claimed, "model_claim_other_market"
        else:
            market, market_basis = "unknown", "unverified_model_claim" if claimed else "not_determinable"
    requirement = spec.get("binding_requirement")
    layers = [("value_clause", clause if clause != quote_norm else ""), ("quote", quote),
              ("source_line", _source_line(material, quote) or ""), ("column_header", " | ".join(dict.fromkeys(hints)))]
    binding = bind(adm.identity, material.profile["statuses"], layers,
                   [("model_variant", str(args.get("variant") or ""))], market=market, requirement=requirement,
                   model_declared_different=claim == "different")

    # condition: only when the quote states it
    typed = typed_value(value, unit=unit or spec.get("normalized_unit"), value_type=spec.get("value_type"),
                        matcher=spec.get("matcher"), condition=args.get("condition"))
    condition = typed.get("condition") if isinstance(typed, dict) else None
    if condition:
        cond_tokens = _tokens(condition)
        quote_sq = " " + squash(quote) + " "
        if not cond_tokens or not all(f" {t} " in quote_sq for t in cond_tokens):
            checks["condition"] = "dropped_not_in_quote"
            typed["condition"] = condition = None
    # time: a date the source states, else the document's own publication metadata; never invented
    temporal: dict[str, Any] = {}
    for key in ("valid_as_of", "valid_from", "valid_to"):
        if args.get(key):
            iso = date_in_source(str(args[key]), material)
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
        "field": name, "value": value, "unit": unit or spec.get("normalized_unit"),
        "source_url": material.url, "document_id": material.document_id, "quote": quote,
        "market": market, "variant": args.get("variant"), "variant_match": binding["variant_match"],
        "note": args.get("note"), "condition": condition, "typed_value": typed,
        "admission_status": "accepted", "admission_version": ADMISSION_VERSION, "admission_checks": checks,
        "entailment": entailment.method,
        "binding_level": binding["binding_level"], "binding_requirement": binding["binding_requirement"],
        "binding_veto": binding["binding_veto"] or None, "binding_dimensions": binding["binding_dimensions"],
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

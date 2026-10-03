"""Candidate Adjudication sweep (SWEEP_MODE=adjudication): the model only JUDGES harvested candidates.

The legacy document sweep (src/document_sweep.py) asks the model to re-type every valid candidate as a full
store_evidence call (value, unit, document_id, verbatim quote, market, variant, variant_match) inside a 2-turn tool
loop. That is output-bound: production chunks of 30 fields timed out at the 240 s read timeout, and the re-typing
itself caused admission rejections. Here code does everything mechanical and the model answers a tiny JSON:

    open fields (current_evaluation)  ->  candidates from USABLE documents only (src/tail_planner)
            ↓  B0 grouping: route_candidates (presentation order), group by material_key, <= 2 per distinct value
            ↓  B1 admission DRY RUN: admit() on the request the candidate itself makes (nothing is stored); a
            ↓     mechanical rejection gets ONE widened quote cut from the cached document; still rejected -> never
            ↓     shown, and not fresh for recovery (`adjudication_not_admissible`)
            ↓  B2 classes: U (one admissible value, exact binding, no harvester hints) · A (other admissible) ·
            ↓     M (nothing admissible, but the pre-sweep inspection located label snippets) · else -> recovery
            ↓  B3 packets per recovery cluster, NO tools, JSON decisions only
            ↓  B4 accepted U/A candidates / located M statements -> synthetic store_evidence calls through the normal
                  ToolSession path: Evidence Admission stays the only gate

A model decision is a PROPOSAL. Nothing here stores evidence, votes, ranks truth or reads model confidence.
Pure helpers only; the runner lives in src/agent.py (run_document_sweep dispatches by mode).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from .field_recovery import material_key

ADJUDICATION_VERSION = "adjudication-v1"
SWEEP_MODES = ("adjudication", "legacy")
# admission rejections a fuller verbatim quote of the same place can cure (the label or the stated availability sits
# next to the candidate's own quote); every other reason is a property of the value itself
MECHANICAL_REASONS = ("field_label_not_in_quote", "value_not_stated", "quote_not_in_source")
# hints the harvester already sets that make a single-value field worth a closer look (A instead of U); a field's own
# dictionary ambiguity rules add their hint keys (src/candidate_harvest._context_ok)
AMBIGUITY_HINTS = ("trim_mentioned", "variant_hints", "ambiguity", "ambiguity_note", "year_hint_differs")
REASON_CODES = ("ok", "other_trim", "other_year", "other_market", "other_powertrain", "wrong_quantity",
                "not_this_field", "unclear")
CLASSES = ("U", "A", "M")
PER_VALUE = 2                 # candidates kept per distinct value of a field
WIDEN_CHARS = 160             # +/- characters around a quote when no line / table context applies
CONTEXT_CHARS = 700           # an A candidate's context
SNIPPET_CHARS = 600           # an M snippet
QUOTE_CHARS = 300             # a quote as shown to the model (the stored quote is never cut)
DEFAULT_LIMITS = {"u_items": 40, "a_fields": 6, "a_candidates": 18, "m_fields": 6, "m_snippets": 12}
DEFAULT_MAX_TOKENS = {"U": 1500, "A": 2000, "M": 1500}


def _domain(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower()
    return (host[4:] if host.startswith("www.") else host) or None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# --- B0 grouping ------------------------------------------------------------------------------------------------

def group_by_value(routed: list[dict], per_value: int = PER_VALUE) -> list[dict]:
    """At most `per_value` candidates per distinct value (material_key), in routed (presentation) order: the first
    one of each value is its best-routed candidate. Nothing else is decided here."""
    counts: dict[str, int] = {}
    kept = []
    for cand in routed:
        key = material_key(cand.get("value"))
        if counts.get(key, 0) < per_value:
            counts[key] = counts.get(key, 0) + 1
            kept.append(cand)
    return kept


def assign_ids(items: Iterable[Any], prefix: str = "c") -> dict[str, Any]:
    """Packet-local ids c1, c2, ... in the given (deterministic) order."""
    return {f"{prefix}{i}": item for i, item in enumerate(items, start=1)}


# --- B1 admission dry run -----------------------------------------------------------------------------------------

def candidate_request(field: str, cand: dict, quote: str | None = None) -> dict:
    """The store_evidence request a candidate itself makes: its own value / unit / document / quote."""
    request = {"field": field, "value": cand.get("value"), "unit": cand.get("unit"),
               "document_id": cand.get("document_id"), "quote": quote if quote is not None else cand.get("quote")}
    return {k: v for k, v in request.items() if v not in (None, "")}


def dry_run(adm, cache, request: dict, documents: Iterable[str] = ()) -> dict:
    """admit() without storing anything (admit itself never stores). Never raises."""
    from .evidence_admission import admit

    try:
        return admit(adm, cache, request, list(documents))
    except Exception as exc:  # noqa: BLE001 - a broken document never costs the sweep
        return {"accepted": False, "reasons": ["dry_run_error"], "message": f"{type(exc).__name__}: {exc}"[:300]}


def decision_flags(decision: dict) -> dict:
    record = decision.get("record") or {}
    return {k: record.get(k) for k in ("variant_match", "binding_level", "market", "source_authority")}


class DocumentReader:
    """Cached document text and tables for one sweep (read once per document). Never raises."""

    def __init__(self, cache):
        self.cache = cache
        self._text: dict[str, str] = {}
        self._tables: dict[str, list[dict]] = {}

    def text(self, document_id: str) -> str:
        doc = str(document_id)
        if doc not in self._text:
            try:
                self._text[doc] = self.cache.read_text(doc) or ""
            except Exception:  # noqa: BLE001
                self._text[doc] = ""
        return self._text[doc]

    def tables(self, document_id: str) -> list[dict]:
        """The document's tables from the existing extraction (the same derived cache the harvester reads)."""
        from .tools.extract import document_tables

        doc = str(document_id)
        if doc not in self._tables:
            try:
                meta = self.cache.get(doc) or {}
                is_html = meta.get("doc_type") == "html" or meta.get("kind") == "rendered"
                html = self.cache.read_body(doc).decode("utf-8", errors="replace") if is_html else None
                self._tables[doc] = list(document_tables(self.cache, doc, meta, html) or [])
            except Exception:  # noqa: BLE001
                self._tables[doc] = []
        return self._tables[doc]

    def table_rows(self, cand: dict) -> tuple[list[str], list[str]] | None:
        """(header row, the candidate's row) of a table-row candidate; None when it is not one."""
        t_index, r_index = cand.get("table_index"), cand.get("row_index")
        if t_index is None or r_index is None:
            return None
        tables = self.tables(cand.get("document_id"))
        if not isinstance(t_index, int) or not 0 <= t_index < len(tables):
            return None
        rows = tables[t_index].get("rows") or []
        if not isinstance(r_index, int) or not 0 <= r_index < len(rows):
            return None
        return [str(c or "").strip() for c in rows[0]], [str(c or "").strip() for c in rows[r_index]]


def _sequences(norm: str, cells: list[str], lo: int = 0, hi: int | None = None) -> list[tuple[int, int]]:
    """Every (start, end) where the cells occur in order (normalized, length-preserving text), one per occurrence of
    the first cell, each matched greedily (so the shortest passage from that start)."""
    from .candidate_harvest import normalize_text

    probes = [normalize_text(c) for c in cells if c and c.strip()]
    hi = len(norm) if hi is None else hi
    if not probes:
        return []
    out, first = [], norm.find(probes[0], lo, hi)
    while first >= 0:
        pos, ok = first + len(probes[0]), True
        for probe in probes[1:]:
            hit = norm.find(probe, pos, hi)
            if hit < 0:
                ok = False
                break
            pos = hit + len(probe)
        if not ok:
            break
        out.append((first, pos))
        first = norm.find(probes[0], first + 1, hi)
    return out


def _locate(text: str, norm: str, quote: str) -> tuple[int, int] | None:
    from .candidate_harvest import normalize_text

    parts = [p.strip() for p in re.split(r"…|\.\.\.", quote or "") if p.strip()]
    if not parts:
        return None
    first = text.find(parts[0])
    if first < 0:
        first = norm.find(normalize_text(parts[0]))
    if first < 0:
        return None
    end = first + len(parts[0])
    for part in parts[1:]:            # a "…"-joined quote: the passage up to its last fragment
        hit = norm.find(normalize_text(part), end)
        if hit < 0 or hit - end > 400:
            break
        end = hit + len(part)
    return first, end


def _word_window(text: str, start: int, end: int) -> str:
    """text[start:end] snapped inward to whole words (a cut "4,150" must not become "150")."""
    if start > 0 and not text[start - 1].isspace():
        nxt = re.search(r"\s", text[start:])
        start = start + nxt.end() if nxt else end
    if end < len(text) and not text[end].isspace():
        back = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
        end = back if back > start else start
    return text[start:end]


def widen_quote(reader: DocumentReader, cand: dict) -> str | None:
    """ONE wider verbatim quote of the candidate's own place in the cached document (never invented text):
    a table-row candidate gets its table's header line and its row; otherwise the line holding the quote when that
    line says more than the quote; else +/- WIDEN_CHARS characters around the quote. Line breaks are collapsed to
    spaces (the admission quote check compares whitespace-normalized text). None when nothing wider is found."""
    from .candidate_harvest import normalize_text

    doc = cand.get("document_id")
    text = reader.text(doc) if doc else ""
    if not text:
        return None
    norm = normalize_text(text)
    if len(norm) != len(text):
        norm = text
    quote = str(cand.get("quote") or "")
    rows = reader.table_rows(cand)
    if rows is not None:
        header, row = rows
        spans = _sequences(norm, row)
        row_span = min(spans, key=lambda sp: (sp[1] - sp[0], sp[0])) if spans else None     # the row itself
        if row_span is not None:
            start = row_span[0]
            if cand.get("row_index"):
                # the closest header line before the row, within the context budget
                heads = [sp for sp in _sequences(norm, header, max(0, row_span[0] - CONTEXT_CHARS), row_span[0])
                         if sp[1] <= row_span[0]]
                if heads:
                    start = max(heads)[0]
            widened = _collapse(text[start:row_span[1]])
            if widened and _collapse(quote) != widened:
                return widened
    span = _locate(text, norm, quote)
    if span is None:
        return None
    line_start = text.rfind("\n", 0, span[0]) + 1
    line_end = text.find("\n", span[1])
    line_end = len(text) if line_end < 0 else line_end
    line = _collapse(text[line_start:line_end])
    if len(line) > len(_collapse(quote)) and line_end - line_start <= 2 * WIDEN_CHARS + (span[1] - span[0]):
        return line
    window = _collapse(_word_window(text, max(0, span[0] - WIDEN_CHARS), min(len(text), span[1] + WIDEN_CHARS)))
    return window if window and window != _collapse(quote) else None


def candidate_context(reader: DocumentReader, cand: dict, limit: int = CONTEXT_CHARS) -> str:
    """What an A candidate is read with: the table header + row for a table candidate, else the lines around its
    quote. Display context only (never a quote)."""
    rows = reader.table_rows(cand)
    if rows is not None:
        header, row = rows
        out = " | ".join(header) + ("\n" + " | ".join(row) if row is not header and cand.get("row_index") else "")
        return out[:limit]
    from .candidate_harvest import normalize_text

    text = reader.text(cand.get("document_id")) if cand.get("document_id") else ""
    if not text:
        return ""
    norm = normalize_text(text)
    span = _locate(text, norm if len(norm) == len(text) else text, str(cand.get("quote") or ""))
    if span is None:
        return ""
    start = text.rfind("\n", 0, max(0, text.rfind("\n", 0, span[0]))) + 1     # the line before
    nxt = text.find("\n", span[1])
    end = len(text) if nxt < 0 else (text.find("\n", nxt + 1) if text.find("\n", nxt + 1) >= 0 else len(text))
    if end - start > limit:
        pad = max(0, (limit - (span[1] - span[0])) // 2)
        start, end = max(start, span[0] - pad), min(end, span[1] + pad)
    return text[start:end].strip()[:limit]


# --- B2 classes ----------------------------------------------------------------------------------------------------

def hint_keys(spec: dict | None) -> tuple[str, ...]:
    """The harvester's hint keys that mark a candidate as ambiguous for this field: the fixed ones plus the field's
    own dictionary ambiguity rules' hint keys."""
    own = [str(r.get("hint_key") or "ambiguity") for r in (spec or {}).get("ambiguity_rules") or []
           if isinstance(r, dict)]
    return tuple(dict.fromkeys(AMBIGUITY_HINTS + tuple(own)))


def candidate_hints(cand: dict, keys: Iterable[str]) -> dict:
    return {k: cand.get(k) for k in keys if cand.get(k) not in (None, "", [], {}, False)}


def field_class(admissible: list[dict], candidates: list[dict], has_snippets: bool,
                keys: Iterable[str] = AMBIGUITY_HINTS) -> str | None:
    """U | A | M | None (nothing to adjudicate: recovery). `admissible` items carry {candidate, flags}; `candidates`
    are all of the field's candidates from usable documents (their hints count)."""
    keys = tuple(keys)
    if admissible:
        values = {material_key(a["candidate"].get("value")) for a in admissible}
        hinted = any(candidate_hints(c, keys) for c in candidates)
        exact = all((a.get("flags") or {}).get("variant_match") == "exact" for a in admissible)
        return "U" if len(values) == 1 and exact and not hinted else "A"
    return "M" if has_snippets else None


# --- B3 packets ----------------------------------------------------------------------------------------------------

def plan_packets(*, fields: list[str], classes: dict[str, str], clusters: dict[str, str],
                 sizes: dict[str, int], limits: dict) -> list[dict]:
    """Packets per recovery cluster (clusters in schema order, `fields` being in schema order), class by class
    (U, A, M), fields packed greedily in schema order. A field is never split across packets; one that alone exceeds
    an item limit is cut to that limit by the caller. `sizes` = items (U / A candidates, M snippets) per field."""
    limits = {**DEFAULT_LIMITS, **{k: v for k, v in (limits or {}).items() if v}}
    item_limit = {"U": limits["u_items"], "A": limits["a_candidates"], "M": limits["m_snippets"]}
    field_limit = {"U": 0, "A": limits["a_fields"], "M": limits["m_fields"]}
    order: list[str] = []
    for name in fields:
        if classes.get(name) in CLASSES and clusters.get(name) not in order:
            order.append(clusters.get(name))
    packets: list[dict] = []
    for cluster in order:
        for cls in CLASSES:
            current: dict | None = None
            for name in fields:
                if classes.get(name) != cls or clusters.get(name) != cluster:
                    continue
                size = min(sizes.get(name, 0), item_limit[cls])
                if current is not None and (current["items"] + size > item_limit[cls]
                                            or (field_limit[cls] and len(current["fields"]) >= field_limit[cls])):
                    packets.append(current)
                    current = None
                if current is None:
                    current = {"class": cls, "cluster": cluster, "fields": [], "items": 0}
                current["fields"].append(name)
                current["items"] += size
            if current is not None:
                packets.append(current)
    return packets


def definition(spec: dict) -> str:
    return str(spec.get("semantic_definition") or spec.get("description") or spec.get("name"))


def shown_item(item: dict, *, context: bool = False, hints: Iterable[str] = ()) -> dict:
    """One adjudication candidate as the model sees it. `item` = {id, field, candidate, quote, flags, context?}."""
    cand, flags = item["candidate"], item.get("flags") or {}
    out = {"id": item["id"], "value": cand.get("value"), "unit": cand.get("unit"),
           "quote": str(item.get("quote") or "")[:QUOTE_CHARS], "source": _domain(cand.get("source_url")),
           "market": flags.get("market"), "binding_level": flags.get("binding_level")}
    if context:
        out["context"] = item.get("context") or ""
        out["variant_match"] = flags.get("variant_match")
        shown = candidate_hints(cand, hints)
        if shown:
            out["hints"] = shown
    return {k: v for k, v in out.items() if v not in (None, "")}


def u_packet(*, identity: dict, target_market: str, specs: dict[str, dict], items: list[dict]) -> dict:
    fields = list(dict.fromkeys(i["field"] for i in items))
    return {"task": "adjudicate_unambiguous", "vehicle_identity": identity, "target_market": target_market,
            "field_definitions": {f: definition(specs.get(f) or {"name": f}) for f in fields},
            "items": [{**shown_item(i), "field": i["field"]} for i in items],
            "reply_shape": {"decisions": [{"id": "c1", "accept": True, "reason": "ok"}]},
            "reason_codes": list(REASON_CODES)}


def a_packet(*, identity: dict, target_market: str, specs: dict[str, dict], items: list[dict],
             hints: dict[str, tuple]) -> dict:
    fields = list(dict.fromkeys(i["field"] for i in items))
    return {"task": "adjudicate_ambiguous", "vehicle_identity": identity, "target_market": target_market,
            "fields": [{"field": f, "definition": definition(specs.get(f) or {"name": f}),
                        "unit": (specs.get(f) or {}).get("normalized_unit") or (specs.get(f) or {}).get("unit"),
                        "candidates": [shown_item(i, context=True, hints=hints.get(f, AMBIGUITY_HINTS))
                                       for i in items if i["field"] == f]} for f in fields],
            "reply_shape": {"fields": [{"field": "<name>", "accept": ["c3"],
                                        "reject": [{"id": "c4", "reason": "other_trim"}]}]},
            "reason_codes": list(REASON_CODES)}


def m_packet(*, identity: dict, target_market: str, specs: dict[str, dict], snippets: list[dict]) -> dict:
    """`snippets` = [{id, field, document_id, source, text}]."""
    fields = list(dict.fromkeys(s["field"] for s in snippets))
    return {"task": "locate_missing", "vehicle_identity": identity, "target_market": target_market,
            "fields": [{"field": f, "definition": definition(specs.get(f) or {"name": f}),
                        "unit": (specs.get(f) or {}).get("normalized_unit") or (specs.get(f) or {}).get("unit"),
                        "value_type": (specs.get(f) or {}).get("value_type"),
                        "snippets": [{k: s.get(k) for k in ("id", "document_id", "source", "text") if s.get(k)}
                                     for s in snippets if s["field"] == f]} for f in fields],
            "reply_shape": {"fields": [{"field": "<name>", "value": "<value>", "unit": "<unit>", "snippet": "s2",
                                        "span": [0, 40]}]},
            "span_rule": "span = [start, end) character offsets inside that snippet's text, covering the label and the "
                         "value; omit a field the snippets do not state"}


def packet_chars(packet: dict) -> int:
    return len(json.dumps(packet, ensure_ascii=False, default=str))


# --- B3 replies ------------------------------------------------------------------------------------------------------

def _reason(value: Any) -> str:
    reason = str(value or "").strip().lower()
    return reason if reason in REASON_CODES else "unclear"


def parse_u_reply(reply: Any, ids: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """([{id, accept, reason}], invalid). Unknown ids / ids of another packet are invalid and ignored."""
    decisions, invalid, seen = [], [], set()
    rows = reply.get("decisions") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return [], [{"problem": "no_decisions_list"}]
    for row in rows:
        cid = str(row.get("id")) if isinstance(row, dict) else None
        if cid not in ids or cid in seen:
            invalid.append({"problem": "unknown_id" if cid not in ids else "duplicate_id", "id": cid})
            continue
        seen.add(cid)
        accept = row.get("accept")
        decisions.append({"id": cid, "accept": accept is True or str(accept).lower() == "true",
                          "reason": _reason(row.get("reason") or ("ok" if accept is True else None))})
    return decisions, invalid


def parse_a_reply(reply: Any, ids: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """([{id, accept, reason}], invalid). An id must belong to the field it is listed under; an id both accepted and
    rejected is invalid (ignored); an accepted id carries reason ok."""
    decisions: dict[str, dict] = {}
    invalid: list[dict] = []
    rows = reply.get("fields") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return [], [{"problem": "no_fields_list"}]
    for row in rows:
        if not isinstance(row, dict):
            invalid.append({"problem": "not_an_object"})
            continue
        field = str(row.get("field") or "")
        entries = [(cid, True, "ok") for cid in row.get("accept") or [] if not isinstance(cid, dict)]
        entries += [((r or {}).get("id") if isinstance(r, dict) else r, False,
                     _reason((r or {}).get("reason") if isinstance(r, dict) else None))
                    for r in row.get("reject") or []]
        for cid, accept, reason in entries:
            cid = str(cid)
            if cid not in ids:
                invalid.append({"problem": "unknown_id", "id": cid, "field": field})
                continue
            if ids[cid]["field"] != field:
                invalid.append({"problem": "id_of_another_field", "id": cid, "field": field})
                continue
            if cid in decisions and decisions[cid]["accept"] != accept:
                invalid.append({"problem": "contradictory_decision", "id": cid, "field": field})
                decisions[cid]["contradictory"] = True
                continue
            decisions.setdefault(cid, {"id": cid, "accept": accept, "reason": reason})
    return [d for d in decisions.values() if not d.pop("contradictory", False)], invalid


def parse_m_reply(reply: Any, snippets: dict[str, dict], fields: Iterable[str]) -> tuple[list[dict], list[dict]]:
    """([{field, value, unit, snippet, span, quote}], invalid). The quote is snippet_text[start:end]; a span outside
    the snippet, an unknown snippet or a field not in the packet is invalid and ignored."""
    wanted = set(fields)
    items, invalid = [], []
    rows = reply.get("fields") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return [], [{"problem": "no_fields_list"}]
    done: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            invalid.append({"problem": "not_an_object"})
            continue
        field = str(row.get("field") or "")
        sid = str(row.get("snippet") or "")
        span = row.get("span")
        if field not in wanted:
            invalid.append({"problem": "field_not_in_packet", "field": field})
            continue
        if field in done:
            invalid.append({"problem": "duplicate_field", "field": field})
            continue
        if sid not in snippets:
            invalid.append({"problem": "unknown_snippet", "field": field, "snippet": sid})
            continue
        text = snippets[sid]["text"]
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in span)
                or not 0 <= span[0] < span[1] <= len(text)):
            invalid.append({"problem": "span_out_of_range", "field": field, "snippet": sid, "span": span})
            continue
        if row.get("value") in (None, ""):
            invalid.append({"problem": "value_missing", "field": field})
            continue
        done.add(field)
        items.append({"field": field, "value": row.get("value"), "unit": row.get("unit") or None, "snippet": sid,
                      "span": list(span), "quote": text[span[0]:span[1]], "document_id": snippets[sid]["document_id"]})
    return items, invalid


# --- B4 storage --------------------------------------------------------------------------------------------------------

def store_arguments(item: dict, decision: dict) -> dict:
    """store_evidence arguments for an accepted U / A candidate: the candidate's own value, unit and document and
    its admissible quote; variant_match "exact" only when the model accepted with reason ok (a claim; the runtime
    computes the binding)."""
    cand = item["candidate"]
    args = {"field": item["field"], "value": cand.get("value"), "unit": cand.get("unit"),
            "document_id": cand.get("document_id"), "quote": item["quote"]}
    if decision.get("reason") == "ok":
        args["variant_match"] = "exact"
    return {k: v for k, v in args.items() if v not in (None, "")}


def located_arguments(item: dict) -> dict:
    args = {"field": item["field"], "value": item["value"], "unit": item.get("unit"),
            "document_id": item["document_id"], "quote": item["quote"]}
    return {k: v for k, v in args.items() if v not in (None, "")}


def synthetic_call(call_id: str, args: dict) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "store_evidence", "arguments": json.dumps(args, ensure_ascii=False, default=str)}}

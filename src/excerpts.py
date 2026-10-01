"""Bounded working memory for field-recovery retries: `prior_relevant_excerpts`.

A retry is a fresh conversation. Without this, attempt #2 knows (from
already_attempted_operations) that a document was inspected but not what was
found, so it re-reads whole documents. This module selects, from material
ALREADY EXPOSED to the model in this vehicle run, a small set of excerpts that
relate to the requested field:

* sources: successful find_in_document hits, extract_html / get_cached_document
  text slices, extract_tables rows and get_structured_data leaves, read from the
  tool results in events.jsonl (the same trace the finalizer bundle uses; nothing
  is stored a second time and no cached document is read here);
* relevance: generic and schema-driven - same-field recovery work, documents
  behind the field's evidence, and token overlap with the field spec (name,
  description, group, unit), the field's earlier queries and its evidence values;
* deterministic deduplication of overlapping spans / contained text, preferring
  the more focused excerpt;
* hard caps on items, total characters and characters per excerpt.

Excerpts are context, never evidence: nothing here creates an evidence record,
calls a model or judges truth.
"""

from __future__ import annotations

import re
from typing import Any

from .storage import trace

DEFAULT_MAX_ITEMS = 8
DEFAULT_MAX_CHARS = 8000
DEFAULT_MAX_EXCERPT_CHARS = 1500
TABLE_MAX_ROWS = 8
TABLE_CELL_CHARS = 60
STRUCTURED_MAX_LINES = 12
# Most focused first: a hit snippet beats a table, structured data, then a broad text slice.
FOCUS = {"find_in_document": 0, "extract_tables": 1, "get_structured_data": 2, "extract_html": 3,
         "get_cached_document": 4}
STOPWORDS = {"the", "and", "with", "for", "per", "max", "time", "from", "this", "that", "are", "was"}


def tokens(*texts: Any) -> set[str]:
    words: set[str] = set()
    for text in texts:
        for word in re.split(r"[^\w]+", str(text or "").lower()):
            if len(word) >= 3 and not word.isdigit():
                words.add(word)
    return words - STOPWORDS


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def fragments(events: list[dict]) -> list[dict]:
    """Material already returned to the model by document-inspection tools (zero-hit / failed calls give none)."""
    out: list[dict] = []
    for order, pair in enumerate(trace.tool_pairs(events)):
        result = pair["result"] if isinstance(pair["result"], dict) else None
        name = pair["name"]
        if not result or result.get("error") or name not in FOCUS:
            continue
        args = trace.parse_args(pair["arguments"])
        base = {"document_id": result.get("document_id") or args.get("document_id") or args.get("key"),
                "origin_tool": name, "step": pair.get("step"), "phase": pair.get("phase") or "research",
                "field": pair.get("field"), "attempt": pair.get("attempt"), "order": order}
        if name == "find_in_document":
            scope = str(result.get("scope") or args.get("scope") or "text")
            ctx = max(50, min(int(args.get("context_chars") or 300), 2000))
            for hit in result.get("hits") or []:
                snippet = (hit.get("snippet") or "").strip()
                if not snippet:
                    continue
                offset = hit.get("offset")
                frag = {**base, "query": args.get("query") or result.get("query"), "scope": scope, "offset": offset,
                        "text": snippet}
                if scope == "text" and isinstance(offset, int):
                    start = max(0, offset - ctx)
                    frag["span"] = (start, start + len(hit.get("snippet") or ""))
                out.append(frag)
        elif name in ("extract_html", "get_cached_document"):
            if name == "get_cached_document" and not result.get("found"):
                continue
            text = result.get("text") or ""
            if text.strip():
                offset = int(result.get("offset") or 0)
                out.append({**base, "offset": offset, "text": text, "span": (offset, offset + len(text))})
        elif name == "extract_tables":
            for table in result.get("tables") or []:
                if table.get("rows"):
                    out.append({**base, "table": table})
        elif name == "get_structured_data":
            data = result.get("data")
            if data is not None or result.get("data_preview"):
                out.append({**base, "structured": data if data is not None else result.get("data_preview")})
    return out


def _first_match(text: str, keys: set[str], phrases: list[str]) -> int | None:
    lower = text.lower()
    positions = [lower.find(k) for k in list(keys) + phrases if k and lower.find(k) >= 0]
    return min(positions) if positions else None


def _window(frag: dict, keys: set[str], phrases: list[str], cap: int) -> dict:
    """The part of a long text slice around the first field-related term, at most `cap` chars."""
    text = frag["text"]
    if len(text) <= cap:
        return frag
    pos = _first_match(text, keys, phrases)
    start = 0 if pos is None else max(0, min(pos - cap // 3, len(text) - cap))
    piece = text[start:start + cap]
    out = {**frag, "text": piece}
    if frag.get("span"):
        out["span"] = (frag["span"][0] + start, frag["span"][0] + start + len(piece))
    return out


def _row_hits(row: list, keys: set[str], phrases: list[str]) -> bool:
    joined = " ".join(str(c) for c in row)
    return bool(tokens(joined) & keys) or any(p in joined.lower() for p in phrases)


def _table_text(frag: dict, keys: set[str], phrases: list[str], same_field: bool) -> str:
    table = frag["table"]
    rows = table.get("rows") or []
    header, body = rows[:1], rows[1:]
    matching = [r for r in body if _row_hits(r, keys, phrases)]
    if not matching and not _row_hits(header[0] if header else [], keys, phrases) and not same_field:
        return ""
    chosen = (matching or body)[:TABLE_MAX_ROWS - len(header)]
    cell = lambda c: str(c)[:TABLE_CELL_CHARS]  # noqa: E731
    lines = [" | ".join(cell(c) for c in r) for r in header + chosen]
    label = f"[table {table.get('index')}{' p' + str(table['page']) if table.get('page') else ''}; " \
            f"{len(chosen)} of {len(body)} rows]"
    return label + "\n" + "\n".join(lines)


def _leaves(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [line for k, v in value.items() for line in _leaves(v, f"{path}.{k}" if path else str(k))]
    if isinstance(value, list):
        return [line for i, v in enumerate(value[:50]) for line in _leaves(v, f"{path}[{i}]")]
    if value in (None, "", [], {}):
        return []
    return [f"{path}: {str(value)[:200]}"]


def _structured_text(frag: dict, keys: set[str], phrases: list[str], same_field: bool) -> str:
    data = frag["structured"]
    lines = _leaves(data) if not isinstance(data, str) else [data[:2000]]
    matching = [ln for ln in lines if tokens(ln) & keys or any(p in ln.lower() for p in phrases)]
    chosen = matching or (lines if same_field else [])
    return "\n".join(chosen[:STRUCTURED_MAX_LINES])


def select_prior_excerpts(events: list[dict], spec: dict, field_evidence: list[dict], doc_metas: list[dict],
                          *, max_items: int = DEFAULT_MAX_ITEMS, max_chars: int = DEFAULT_MAX_CHARS,
                          max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS) -> tuple[list[dict], dict]:
    """(excerpts, stats) for a retry of `spec`. Bounded, deduplicated, relevance-ranked; never evidence."""
    name = spec["name"]
    pairs = trace.tool_pairs(events)
    field_queries = [trace.parse_args(p["arguments"]).get("query") for p in pairs if p.get("field") == name]
    keys = tokens(name.replace("_", " "), spec.get("description"), spec.get("group"), spec.get("unit"),
                  *field_queries)
    phrases = sorted({_norm(str(e.get("value"))) for e in field_evidence
                      if e.get("value") not in (None, "") and len(str(e.get("value"))) >= 2})
    evidence_docs = {e.get("document_id") for e in field_evidence if e.get("document_id")}
    evidence_urls = {e.get("source_url") for e in field_evidence if e.get("source_url")}
    url_of = {m.get("document_id"): m.get("final_url") or m.get("url") for m in doc_metas if m.get("document_id")}
    stats = {"candidates": 0, "below_relevance": 0, "deduplicated": 0, "cut_by_caps": 0}

    scored = []
    for frag in fragments(events):
        same_field = frag.get("field") == name
        if "table" in frag:
            frag = {**frag, "text": _table_text(frag, keys, phrases, same_field)}
        elif "structured" in frag:
            frag = {**frag, "text": _structured_text(frag, keys, phrases, same_field)}
        frag = _window(frag, keys, phrases, max_excerpt_chars) if frag.get("text") else frag
        text = (frag.get("text") or "").strip()
        if not text:
            continue
        stats["candidates"] += 1
        doc = frag.get("document_id")
        overlap = len(keys & tokens(text, frag.get("query"))) + sum(1 for p in phrases if p in text.lower())
        score = (100 if same_field else 0) + \
            (50 if doc in evidence_docs or url_of.get(doc) in evidence_urls else 0) + 10 * min(overlap, 5)
        if score <= 0:
            stats["below_relevance"] += 1
            continue
        scored.append((score, frag))

    scored.sort(key=lambda sf: (-sf[0], FOCUS[sf[1]["origin_tool"]], len(sf[1]["text"]), -(sf[1]["step"] or 0)))
    chosen: list[dict] = []
    total = 0
    for score, frag in scored:
        text = frag["text"].strip()[:max_excerpt_chars]
        if any(_duplicate(frag, text, kept) for kept in chosen):
            stats["deduplicated"] += 1
            continue
        if len(chosen) >= max_items or total >= max_chars:
            stats["cut_by_caps"] += 1
            continue
        room = max_chars - total
        if len(text) > room:
            if room < 200:
                stats["cut_by_caps"] += 1
                continue
            text = text[:room]
        entry = {"document_id": frag.get("document_id"), "source_url": url_of.get(frag.get("document_id")),
                 "origin_tool": frag["origin_tool"], "step": frag.get("step"), "phase": frag.get("phase"),
                 "relevance": score}
        for key in ("field", "attempt", "query", "offset"):
            if frag.get(key) not in (None, ""):
                entry[key] = frag[key]
        entry["text"] = text
        entry["_span"] = frag.get("span")
        chosen.append(entry)
        total += len(text)
    excerpts = [{k: v for k, v in e.items() if k != "_span"} for e in chosen]
    stats.update(items=len(excerpts), chars=total)
    return excerpts, stats


def _duplicate(frag: dict, text: str, kept: dict) -> bool:
    """Same document and overlapping text span (>= half of the smaller one), or contained text."""
    span, other = frag.get("span"), kept.get("_span")
    if span and other and frag.get("document_id") == kept.get("document_id"):
        overlap = min(span[1], other[1]) - max(span[0], other[0])
        if overlap > 0 and overlap >= 0.5 * min(span[1] - span[0], other[1] - other[0]):
            return True
    a, b = _norm(text), _norm(kept["text"])
    return bool(a) and (a in b or b in a)


def excerpt_chars(excerpts: list[dict]) -> int:
    return sum(len(e.get("text") or "") for e in excerpts)


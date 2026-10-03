"""Grounded candidates (Part E): a model POINTS at where a document states a value; code cuts the quote.

Some fields are stated in free prose (equipment, warranty) that no parser rule reads. For open applicable fields with
NO admissible candidate after the adjudication dry run, the top documents (usable, not another variant, official or
target-market, ranked by tail_planner.rank_documents) each get ONE no-tool call:

    input   vehicle identity, target market, the selected fields' definitions (<= 15), the document text as numbered
            blocks (HTML: visible-text lines grouped into blocks <= 600 chars; PDF: per page; table rows as
            "label | value" lines), alias-hit blocks first, <= 24,000 chars
    output  {"items": [{"field", "value", "unit", "block": "b12", "start": 41, "end": 96}]}
    code    validates field names, block ids and offsets, cuts quote = block_text[start:end] (word boundaries,
            <= 300 chars), requires the quote in the cached document (quote_in_source), builds a candidate
            (extraction_method grounded_llm, parser_confidence 0.5) and dry-runs admit() on it

An admissible grounded candidate joins its field's candidates for adjudication as class A, so a model judges it a
second time in context. Nothing here stores evidence: only adjudication + Evidence Admission can. No votes, no model
confidence. Pure helpers only; the runner lives in src/agent.py (run_grounded_candidates).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

GROUNDED_VERSION = "grounded-v1"
BLOCK_CHARS = 600
PAGE_BLOCK_CHARS = 3000          # a PDF page longer than this is split at line boundaries
MAX_PACKET_TEXT = 24000
MAX_FIELDS = 15
MAX_DOCUMENTS = 3
MAX_TOKENS = 4000                # reasoning shares this budget with the JSON answer (glm-5.3 always reasons)
QUOTE_MAX = 300
PARSER_CONFIDENCE = 0.5
METHOD = "grounded_llm"
PAGE_MARK = re.compile(r"^\[page (\d+)\]\s*$")


def _group(lines: list[str], limit: int) -> list[str]:
    blocks, current = [], ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(line) > limit:                         # one long line: its own block(s), cut at word boundaries
            if current:
                blocks.append(current)
                current = ""
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                blocks.append(line[:cut].strip())
                line = line[cut:].strip()
            if line:
                current = line
            continue
        if current and len(current) + 1 + len(line) > limit:
            blocks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        blocks.append(current)
    return blocks


def document_blocks(text: str, *, is_pdf: bool, tables: list[dict] | None = None, alias_pattern=None,
                    normalize=None, max_chars: int = MAX_PACKET_TEXT) -> list[dict]:
    """[{id, text, page?}] of one document: HTML / text lines grouped into blocks of <= BLOCK_CHARS, PDF per page,
    plus table rows as "label | value" lines. Blocks whose text names a dictionary alias come first, then the rest in
    document order, until `max_chars`. Ids (b1, b2, ...) follow document order, so they are stable."""
    raw: list[dict] = []
    if is_pdf:
        page, lines = None, []
        pages: list[tuple[int | None, list[str]]] = []
        for line in (text or "").splitlines():
            mark = PAGE_MARK.match(line.strip())
            if mark:
                if lines:
                    pages.append((page, lines))
                page, lines = int(mark.group(1)), []
            else:
                lines.append(line)
        if lines:
            pages.append((page, lines))
        for number, page_lines in pages:
            for block in _group(page_lines, PAGE_BLOCK_CHARS):
                raw.append({"text": block, "page": number})
    else:
        raw += [{"text": block} for block in _group((text or "").splitlines(), BLOCK_CHARS)]
    rows = []
    for table in tables or []:
        for row in table.get("rows") or []:
            cells = [str(c or "").strip() for c in row]
            if len(cells) >= 2 and cells[0] and any(cells[1:]):
                rows.append(" | ".join(c for c in cells if c))
    raw += [{"text": block, "table_rows": True} for block in _group(rows, BLOCK_CHARS)]
    for index, block in enumerate(raw, start=1):
        block["id"] = f"b{index}"
    norm = normalize or (lambda s: s.lower())

    def hit(block: dict) -> bool:
        return bool(alias_pattern is not None and alias_pattern.search(norm(block["text"])))

    ordered = [b for b in raw if hit(b)] + [b for b in raw if not hit(b)]
    out, used = [], 0
    for block in ordered:
        if used + len(block["text"]) > max_chars:
            continue
        out.append(block)
        used += len(block["text"])
    return out


def definition(spec: dict) -> str:
    return str(spec.get("semantic_definition") or spec.get("description") or spec.get("name"))


def grounded_packet(*, identity: dict, target_market: str, specs: list[dict], blocks: list[dict],
                    source: str | None = None) -> dict:
    return {"task": "locate_values", "vehicle_identity": identity, "target_market": target_market,
            "source": source,
            "fields": [{"field": s["name"], "definition": definition(s),
                        **{k: s.get(k) for k in ("normalized_unit", "value_type") if s.get(k)}}
                       for s in specs[:MAX_FIELDS]],
            "blocks": [{"id": b["id"], "text": b["text"]} for b in blocks],
            "reply_shape": {"items": [{"field": "<name>", "value": "<value>", "unit": "<unit>", "block": "b12",
                                       "start": 41, "end": 96}]},
            "offset_rule": "start / end are character offsets [start, end) inside that block's text, covering the "
                           "statement (the label and the value); omit every field the document does not state"}


def _word_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def parse_reply(reply: Any, blocks: dict[str, dict], fields: Iterable[str]) -> tuple[list[dict], list[dict]]:
    """([{field, value, unit, block, start, end, quote}], invalid). An unknown field or block, offsets outside the
    block, an empty value or a quote longer than QUOTE_MAX after word expansion is invalid (logged, dropped)."""
    wanted = set(fields)
    rows = reply.get("items") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return [], [{"problem": "no_items_list"}]
    items, invalid = [], []
    for row in rows:
        if not isinstance(row, dict):
            invalid.append({"problem": "not_an_object"})
            continue
        field, bid = str(row.get("field") or ""), str(row.get("block") or "")
        start, end = row.get("start"), row.get("end")
        if field not in wanted:
            invalid.append({"problem": "field_not_requested", "field": field})
            continue
        if bid not in blocks:
            invalid.append({"problem": "unknown_block", "field": field, "block": bid})
            continue
        text = blocks[bid]["text"]
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)) \
                or not 0 <= start < end <= len(text):
            invalid.append({"problem": "span_out_of_range", "field": field, "block": bid, "start": start, "end": end})
            continue
        if row.get("value") in (None, "", [], {}):
            invalid.append({"problem": "value_missing", "field": field, "block": bid})
            continue
        s, e = _word_span(text, start, end)
        quote = re.sub(r"\s+", " ", text[s:e]).strip()
        if not quote or len(quote) > QUOTE_MAX:
            invalid.append({"problem": "quote_too_long" if quote else "empty_quote", "field": field, "block": bid})
            continue
        items.append({"field": field, "value": row.get("value"), "unit": row.get("unit") or None, "block": bid,
                      "start": start, "end": end, "quote": quote, "page": blocks[bid].get("page")})
    return items, invalid


def candidate(item: dict, *, document_id: str, source_url: str | None) -> dict:
    out = {"field": item["field"], "value": item["value"], "unit": item.get("unit"), "document_id": document_id,
           "quote": item["quote"], "extraction_method": METHOD, "parser_confidence": PARSER_CONFIDENCE,
           "source_url": source_url, "origin": "grounded", "block": item.get("block"), "page": item.get("page")}
    return {k: v for k, v in out.items() if v not in (None, "")}

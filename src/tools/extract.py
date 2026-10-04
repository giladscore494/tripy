"""Extraction tools over stored documents: text, tables, focused search, structured data."""

from __future__ import annotations

import io
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "object", "canvas")
STATE_PATTERNS = (
    "__INITIAL_STATE__", "__PRELOADED_STATE__", "__APOLLO_STATE__", "__NUXT__",
    "__DATA__", "__STATE__", "__APP_DATA__", "dataLayer",
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _clean_lines(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def visible_text(html: str) -> str:
    soup = _soup(html)
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    return _clean_lines(soup.get_text("\n"))


def html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:300] if match else ""


def html_links(html, base: str) -> list[dict]:
    """Every http(s) outbound link of an HTML page (str or parsed soup) as {text, url}, absolute, page order, first
    occurrence of each URL. Shared by extract_html and the research phase's navigation links."""
    soup = _soup(html) if isinstance(html, str) else html
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base or "", a["href"])
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            links.append({"text": a.get_text(" ", strip=True)[:120], "url": href})
    return links


class DocumentNotFound(LookupError):
    pass


def _load(ctx, document_id: str) -> tuple[dict, str | None]:
    """Return (meta, html-or-None). Counts as a document opening for metrics."""
    meta = ctx.cache.get(document_id)
    if not meta:
        raise DocumentNotFound(f"Unknown document_id {document_id!r}; fetch the URL first.")
    ctx.note_document(document_id)
    if meta.get("doc_type") == "html" or meta.get("kind") == "rendered":
        body = ctx.cache.read_body(document_id)
        return meta, body.decode("utf-8", errors="replace")
    return meta, None


def _chunk(text: str, offset: int, max_chars: int) -> dict:
    offset = max(0, int(offset or 0))
    piece = text[offset : offset + max_chars]
    end = offset + len(piece)
    return {"offset": offset, "text": piece, "total_chars": len(text),
            "next_offset": end if end < len(text) else None}


def _text_cap(ctx, max_chars) -> int:
    return max(200, min(int(max_chars or ctx.config.max_text_chars), ctx.config.max_text_chars))


def extract_html(ctx, document_id: str, offset: int = 0, max_chars: int | None = None) -> dict:
    meta, html = _load(ctx, document_id)
    max_chars = _text_cap(ctx, max_chars)
    text = ctx.cache.read_text(document_id)
    result = {"document_id": document_id, "url": meta.get("final_url") or meta.get("url"),
              "title": meta.get("title", "")}
    if html is not None and not offset:
        soup = _soup(html)
        base = meta.get("final_url") or meta.get("url") or ""
        result["headings"] = [
            {"level": int(h.name[1]), "text": h.get_text(" ", strip=True)[:200]}
            for h in soup.find_all(re.compile(r"^h[1-6]$")) if h.get_text(strip=True)
        ][:80]
        result["lists"] = [
            [li.get_text(" ", strip=True)[:200] for li in ul.find_all("li", recursive=False)][:30]
            for ul in soup.find_all(["ul", "ol"])
            if 1 < len(ul.find_all("li", recursive=False)) <= 60
        ][:25]
        links = html_links(soup, base)
        result["links"] = links[:ctx.config.max_links]
        result["links_total"] = len(links)
    result.update(_chunk(text, offset, max_chars))
    return result


def _html_tables(html: str) -> list[dict]:
    soup = _soup(html)
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if rows:
            caption = table.find("caption")
            tables.append({"source": "html_table", "caption": caption.get_text(" ", strip=True) if caption else "",
                           "rows": rows})
    for dl in soup.find_all("dl"):
        rows = []
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            rows.append([dt.get_text(" ", strip=True), dd.get_text(" ", strip=True) if dd else ""])
        if rows:
            tables.append({"source": "definition_list", "caption": "", "rows": rows})
    return tables


# Second pass for spec matrices WITHOUT ruling lines (brochures): pdfplumber's text strategies. Bounded and optional:
# a failure falls back to the default (ruling-line) tables.
TEXT_TABLE_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3,
                       "join_tolerance": 3, "intersection_tolerance": 5, "text_x_tolerance": 2, "text_y_tolerance": 2,
                       "min_words_vertical": 2, "min_words_horizontal": 1}
MAX_TEXT_TABLES = 30
TEXT_PASS_MAX_PAGES = 60          # the text-strategy pass reads at most the first 60 pages of a document ...
TEXT_PASS_MAX_S = 5.0             # ... and stops once it has spent 5 s on the document (noted as a harvest cap)
MIN_TEXT_TABLE_ROWS, MIN_TEXT_TABLE_COLS, MIN_TEXT_TABLE_LABELS = 3, 2, 2


class TableVocabulary:
    """What the second PDF table pass recognizes: any field alias (first-column labels) and the trim header terms,
    from the full field dictionary (vehicle-independent, so the derived table cache stays shared)."""

    def __init__(self, alias=None, trim_header=None, hebrew_aliases=(), identity_rows=None):
        self.alias, self.trim_header, self.hebrew_aliases = alias, trim_header, tuple(hebrew_aliases)
        self.identity_rows = identity_rows

    @classmethod
    def default(cls) -> "TableVocabulary":
        from ..candidate_harvest import dictionary_for
        from ..fields import load_schema

        d = dictionary_for(load_schema())
        return cls(d.any_alias, d.trim_header, d.hebrew_aliases, d.column_identity_rows)

    def alias_hits(self, text: str) -> int:
        from ..candidate_harvest import normalize_text

        return len(self.alias.findall(normalize_text(text or ""))) if self.alias is not None else 0

    def is_label(self, cell: str) -> bool:
        from ..candidate_harvest import normalize_text

        return bool(self.alias is not None and cell and self.alias.search(normalize_text(cell)))

    def is_trim_header(self, cell: str) -> bool:
        from ..candidate_harvest import normalize_text

        return bool(self.trim_header is not None and cell and self.trim_header.search(normalize_text(cell)))


def _logical_cell(cell: str, vocabulary: TableVocabulary) -> str:
    """A cell in logical order: visually ordered (reversed) Hebrew is repaired as document_segments repairs lines
    (dictionary hit rate, candidate_harvest.logical_rtl_line; the older final-letter / alias rule as a fallback)."""
    from ..candidate_harvest import HEBREW, logical_rtl_line, looks_reversed, reverse_hebrew_line, rtl_dictionary

    if not cell or not HEBREW.search(cell):
        return cell
    logical = logical_rtl_line(cell, rtl_dictionary(vocabulary.hebrew_aliases))
    if logical is not None:
        return logical
    if looks_reversed(cell, vocabulary.hebrew_aliases):
        return reverse_hebrew_line(cell)
    return cell


def text_table_ok(rows: list[list[str]], vocabulary: TableVocabulary) -> bool:
    """A second-pass table is kept only when it looks like a spec matrix: >= 3 rows, >= 2 columns, and >= 2
    first-column cells naming a dictionary field (or a trim header present)."""
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) < MIN_TEXT_TABLE_ROWS or max((len(r) for r in rows), default=0) < MIN_TEXT_TABLE_COLS:
        return False
    labels = sum(1 for r in rows if r and vocabulary.is_label(r[0]))
    header = any(vocabulary.is_trim_header(c) for r in rows[:2] for c in r)
    return labels >= MIN_TEXT_TABLE_LABELS or header


def wants_text_pass(default_tables_on_page: int, page_text: str, vocabulary: TableVocabulary) -> bool:
    """Run the text-strategy pass on a page where the default pass found no table, or whose text names >= 3
    dictionary aliases (a spec page may hold a ruled table AND a borderless matrix). A page that names no field
    alias and no trim header cannot yield a kept table (text_table_ok), so it is never parsed a second time."""
    hits = vocabulary.alias_hits(page_text)
    if hits >= 3:
        return True
    return default_tables_on_page == 0 and (hits > 0 or vocabulary.is_trim_header(page_text))


CELL_GAP = 9.0          # points between two words of the same row that start a new cell


def _text_strategy_tables(page, vocabulary: TableVocabulary) -> list[list[list[str]]]:
    """Borderless tables of one page: the table areas and row bands come from pdfplumber's text strategies; the cells
    of each row are rebuilt from WHOLE words split at wide gaps (the strategy's own column edges cut words such as
    "Hybrid" or separate "4,650" from "mm"). Right-to-left rows (the label in the last cell) are put label first, and
    reversed Hebrew cells are repaired."""
    out = []
    for table in page.find_tables(TEXT_TABLE_SETTINGS) or []:
        # the strategy's bbox ends at its last column edge and would cut the last column's words: keep the table's
        # row band across the page width
        x0, top, x1, bottom = table.bbox
        band = (0, max(0, top - 2), page.width, min(page.height, bottom + 2))
        words = page.crop(band).extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
        lines: list[list[dict]] = []
        for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
            if lines and abs(lines[-1][0]["top"] - word["top"]) <= 3:
                lines[-1].append(word)
            else:
                lines.append([word])
        rows = []
        for line in lines:
            cells: list[list[str]] = []
            last = None
            for word in sorted(line, key=lambda w: w["x0"]):
                if last is None or word["x0"] - last["x1"] > CELL_GAP:
                    cells.append([])
                cells[-1].append(word["text"])
                last = word
            rows.append([_logical_cell(" ".join(c), vocabulary) for c in cells])
        rtl = sum(1 for r in rows if len(r) > 1 and vocabulary.is_label(r[-1]) and not vocabulary.is_label(r[0]))
        ltr = sum(1 for r in rows if len(r) > 1 and vocabulary.is_label(r[0]))
        if rtl > ltr:
            rows = [list(reversed(r)) for r in rows]
        out.append(rows)
    return out


def _pdf_tables(body: bytes, max_pages: int = 200, vocabulary: TableVocabulary | None = None,
                text_max_pages: int = TEXT_PASS_MAX_PAGES, text_max_s: float = TEXT_PASS_MAX_S) -> list[dict]:
    import time

    import pdfplumber

    from ..candidate_harvest import note_harvest_cap

    tables, extra = [], []
    text_spent, capped, page_count = 0.0, None, 0
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages[:max_pages], start=1):
            found = 0
            seen_first: set[str] = set()
            for raw in page.extract_tables() or []:
                rows = [[(cell or "").strip() for cell in row] for row in raw if row and any(row)]
                if rows:
                    found += 1
                    seen_first |= {r[0] for r in rows if r and r[0]}
                    tables.append({"source": "pdf_table", "page": page_no, "caption": "", "rows": rows})
            if vocabulary is None or len(extra) >= MAX_TEXT_TABLES or capped:
                continue
            if page_no > text_max_pages or text_spent > text_max_s:
                capped = {"reason": "pages", "pages_read": text_max_pages} if page_no > text_max_pages else \
                    {"reason": "time_budget", "pages_read": page_no - 1, "time_budget_s": text_max_s}
                continue
            t0 = time.monotonic()
            try:      # never raises: the default tables stand
                if not wants_text_pass(found, page.extract_text() or "", vocabulary):
                    continue
                for rows in _text_strategy_tables(page, vocabulary):
                    if not text_table_ok(rows, vocabulary):
                        continue
                    if {r[0] for r in rows if r and r[0]} <= seen_first:
                        continue              # the default pass already read these rows
                    extra.append({"source": "pdf_table_text", "page": page_no, "caption": "", "rows": rows})
                    if len(extra) >= MAX_TEXT_TABLES:
                        break
            except Exception:  # noqa: BLE001
                continue
            finally:
                text_spent += time.monotonic() - t0
    if capped:
        note_harvest_cap(stage="pdf_text_tables", pages=min(page_count, max_pages), **capped)
    # appended after the default tables, so every default table keeps its index (candidates cite table_index)
    return tables + extra


def with_column_identities(tables: list[dict], vocabulary: "TableVocabulary | None" = None) -> list[dict]:
    """Each multi-variant table gets its per-column identity text (candidate_harvest.column_identities), computed once
    and stored with the table. Tables cached before it existed get it computed by the harvest instead."""
    from ..candidate_harvest import column_identities, table_header

    try:
        vocabulary = vocabulary or TableVocabulary.default()
    except Exception:  # noqa: BLE001 - identity text is an aid; the tables stand without it
        return tables
    for table in tables:
        rows = table.get("rows") or []
        idents = column_identities(rows, vocabulary.identity_rows,
                                   header=table_header(rows, vocabulary.trim_header) is not None)
        if any(idents):
            table["column_identity"] = idents
    return tables


def document_tables(cache, document_id: str, meta: dict, html: str | None) -> list[dict]:
    """All tables of a cached document, extracted once (derived cache, single flight). PDF tables are cached as
    "tables_v3" (default + text-strategy pass + the PR #42 right-to-left cell repair), so documents cached before
    either get it too."""
    def compute() -> list[dict]:
        if html is not None:
            return with_column_identities(_html_tables(html))
        if meta.get("doc_type") == "pdf":
            body = cache.read_body(document_id)
            try:
                vocabulary = TableVocabulary.default()
                return with_column_identities(_pdf_tables(body, vocabulary=vocabulary), vocabulary)
            except Exception:  # noqa: BLE001 - the second pass never costs the default tables
                return _pdf_tables(body)
        return []

    name = "tables_v3" if html is None and meta.get("doc_type") == "pdf" else "tables"
    return cache.derived(document_id, name, compute)[0]


def document_structured(cache, document_id: str, html: str) -> dict:
    return cache.derived(document_id, "structured", lambda: _structured(html))[0]


def extract_tables(ctx, document_id: str, max_tables: int | None = None, max_rows: int | None = None,
                   start_table: int = 0) -> dict:
    meta, html = _load(ctx, document_id)
    max_tables = max(1, min(int(max_tables or ctx.config.max_tables), ctx.config.max_tables))
    max_rows = max(1, min(int(max_rows or ctx.config.max_table_rows), ctx.config.max_table_rows))
    start_table = max(0, int(start_table or 0))
    tables = document_tables(ctx.cache, document_id, meta, html)
    out = []
    for index, table in enumerate(tables[start_table : start_table + max_tables], start=start_table):
        rows = table["rows"]
        table = {k: v for k, v in table.items() if k != "column_identity"}     # harvest-only (binding) material
        out.append({**table, "index": index, "n_rows": len(rows),
                    "n_cols": max((len(r) for r in rows), default=0),
                    "rows": rows[:max_rows], "rows_truncated": len(rows) > max_rows})
    end = start_table + len(out)
    result = {"document_id": document_id, "tables_total": len(tables), "tables": out,
              "next_start_table": end if end < len(tables) else None}
    if any(t["rows_truncated"] for t in out):
        result["hint"] = "Long tables are cut; use find_in_document to locate specific rows."
    return result


def _structured(html: str) -> dict:
    soup = _soup(html)
    data: dict = {"json_ld": [], "next_data": None, "application_json": [], "window_state": {}, "meta": {},
                  "microdata": []}
    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        if stype == "application/ld+json":
            try:
                data["json_ld"].append(json.loads(raw))
            except ValueError:
                data["json_ld"].append({"_unparsed": raw[:2000]})
        elif script.get("id") == "__NEXT_DATA__":
            try:
                data["next_data"] = json.loads(raw)
            except ValueError:
                data["next_data"] = {"_unparsed": raw[:2000]}
        elif stype == "application/json":
            try:
                data["application_json"].append({"id": script.get("id"), "data": json.loads(raw)})
            except ValueError:
                pass
        else:
            for name in STATE_PATTERNS:
                idx = raw.find(name)
                if idx < 0:
                    continue
                brace = min((p for p in (raw.find("{", idx), raw.find("[", idx)) if p >= 0), default=-1)
                if brace < 0:
                    continue
                try:
                    value, _ = json.JSONDecoder().raw_decode(raw[brace:])
                    data["window_state"][name] = value
                except ValueError:
                    continue
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        if key and tag.get("content"):
            data["meta"][key] = tag["content"][:500]
    for item in soup.find_all(attrs={"itemprop": True})[:200]:
        value = item.get("content") or item.get_text(" ", strip=True)
        if value:
            data["microdata"].append({"itemprop": item["itemprop"], "value": value[:300]})
    return data


def get_structured_data(ctx, document_id: str, max_chars: int | None = None) -> dict:
    meta, html = _load(ctx, document_id)
    max_chars = _text_cap(ctx, max_chars)
    if html is None:
        return {"document_id": document_id, "error": "not_html",
                "message": f"Document is {meta.get('doc_type')}; use extract_html or extract_tables."}
    data = document_structured(ctx.cache, document_id, html)
    serialized = json.dumps(data, ensure_ascii=False)
    found = {k: bool(v) for k, v in data.items()}
    result = {"document_id": document_id, "found": found, "total_chars": len(serialized)}
    if len(serialized) <= max_chars:
        result["data"] = data
    else:
        result["data_preview"] = serialized[:max_chars]
        result["hint"] = "Truncated. Use find_in_document with scope='structured' to search the full data."
    return result


def _hits(haystack: str, query: str, context_chars: int, max_hits: int) -> list[dict]:
    lower = haystack.lower()
    q = query.lower().strip()
    positions = []
    start = 0
    while q and len(positions) < max_hits:
        idx = lower.find(q, start)
        if idx < 0:
            break
        positions.append((idx, len(q), 1.0))
        start = idx + len(q)
    if not positions:
        terms = [t for t in re.split(r"\s+", q) if len(t) > 1]
        scored = []
        for term in terms:
            for m in re.finditer(re.escape(term), lower):
                window = lower[max(0, m.start() - context_chars) : m.end() + context_chars]
                score = sum(1 for t in terms if t in window) / len(terms)
                scored.append((m.start(), len(term), score))
        scored.sort(key=lambda x: (-x[2], x[0]))
        taken: list[tuple[int, int, float]] = []
        for pos in scored:
            if all(abs(pos[0] - t[0]) > context_chars for t in taken):
                taken.append(pos)
            if len(taken) >= max_hits:
                break
        positions = sorted(taken)
    return [{"offset": idx, "match_score": round(score, 2),
             "snippet": haystack[max(0, idx - context_chars) : idx + length + context_chars]}
            for idx, length, score in positions]


def find_in_document(ctx, document_id: str, query: str, scope: str = "text", context_chars: int = 300,
                     max_hits: int = 15) -> dict:
    meta, html = _load(ctx, document_id)
    scope = (scope or "text").lower()
    if scope == "source":
        haystack = html if html is not None else ctx.cache.read_body(document_id).decode("utf-8", "replace")
    elif scope == "structured":
        if html is None:
            return {"document_id": document_id, "error": "not_html"}
        data = ctx.cache.get_derived(document_id, "structured") or _structured(html)
        haystack = json.dumps(data, ensure_ascii=False)
    else:
        haystack = ctx.cache.read_text(document_id)
    hits = _hits(haystack, query, max(50, min(int(context_chars), 2000)), max(1, min(int(max_hits), 50)))
    return {"document_id": document_id, "query": query, "scope": scope, "hits": hits, "hit_count": len(hits)}


def inspect_document_for_fields(ctx, document_id: str, fields: list | None = None, max_matches_per_field: int = 2,
                                context_chars: int = 120) -> dict:
    """Batch local inspection of ONE stored document for several requested fields in one operation (no model, no
    network, no evidence): every field's label locations as compact snippets with offsets, plus the parser's candidates
    for those fields. Default fields: every applicable requested field of this run."""
    from ..document_inspection import inspect_document

    _load(ctx, document_id)       # unknown document -> DocumentNotFound; counts as a document opening
    specs = list(getattr(ctx.admission, "all_specs", None) or [])
    if not specs:
        from ..fields import resolve_requested_fields

        specs = resolve_requested_fields(None, propulsion=(ctx.vehicle or {}).get("propulsion"))
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    return inspect_document(ctx.cache, document_id, specs, fields or None, context_chars=context_chars,
                            max_matches_per_field=max(1, min(int(max_matches_per_field or 2), 5)),
                            max_chars=int(ctx.config.max_text_chars * 1.4))   # under the default tool-output cap

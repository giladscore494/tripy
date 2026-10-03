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


def _pdf_tables(body: bytes, max_pages: int = 200) -> list[dict]:
    import pdfplumber

    tables = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page_no, page in enumerate(pdf.pages[:max_pages], start=1):
            for raw in page.extract_tables() or []:
                rows = [[(cell or "").strip() for cell in row] for row in raw if row and any(row)]
                if rows:
                    tables.append({"source": "pdf_table", "page": page_no, "caption": "", "rows": rows})
    return tables


def document_tables(cache, document_id: str, meta: dict, html: str | None) -> list[dict]:
    """All tables of a cached document, extracted once (derived cache, single flight)."""
    def compute() -> list[dict]:
        if html is not None:
            return _html_tables(html)
        if meta.get("doc_type") == "pdf":
            return _pdf_tables(cache.read_body(document_id))
        return []

    return cache.derived(document_id, "tables", compute)[0]


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

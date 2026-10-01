"""fetch_url / fetch_pdf: download, store and summarize documents.

Only technical checks apply: http(s) scheme, timeouts and a response size cap.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

import requests

from .extract import html_title, visible_text

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36 MILO-GLM-Benchmark/1")
KEPT_HEADERS = ("content-type", "content-length", "content-encoding", "last-modified", "etag",
                "date", "cache-control", "server", "location")
PREVIEW_CHARS = 3000


def check_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Only http(s) URLs are supported, got {url!r}")
    return url


def http_get(ctx, url: str) -> dict:
    cfg = ctx.config
    resp = ctx.session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                         "Accept-Language": "he,en;q=0.9"},
                           timeout=(cfg.connect_timeout_s, cfg.read_timeout_s),
                           stream=True, allow_redirects=True)
    chunks, size, truncated = [], 0, False
    try:
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            if size + len(chunk) > cfg.max_response_bytes:
                chunks.append(chunk[: cfg.max_response_bytes - size])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        resp.close()
    headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in KEPT_HEADERS}
    return {
        "status": resp.status_code,
        "final_url": resp.url or url,
        "redirects": [r.url for r in getattr(resp, "history", []) or []],
        "headers": headers,
        "content_type": headers.get("content-type", ""),
        "body": b"".join(chunks),
        "truncated": truncated,
    }


def _is_pdf(content_type: str, body: bytes) -> bool:
    return "pdf" in (content_type or "").lower() or body[:5] == b"%PDF-"


def _decode(body: bytes, content_type: str) -> str:
    charset = None
    if "charset=" in (content_type or "").lower():
        charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
    for enc in filter(None, (charset, "utf-8", "windows-1255", "latin-1")):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def pdf_text_and_meta(body: bytes, max_pages: int = 300) -> tuple[str, dict]:
    import pdfplumber

    pages_text, meta = [], {}
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        meta = {"pages": len(pdf.pages), "pdf_metadata": {k: str(v)[:200] for k, v in (pdf.metadata or {}).items()}}
        for i, page in enumerate(pdf.pages[:max_pages], start=1):
            pages_text.append(f"[page {i}]\n{page.extract_text() or ''}")
    return "\n\n".join(pages_text), meta


QUERY_HINT = ("The full text is stored in the document cache. Query it with find_in_document(document_id, query), "
              "extract_tables(document_id), get_structured_data(document_id) or extract_html(document_id, offset) "
              "instead of fetching it again.")


def _summary(record: dict, text: str, cache_hit: bool, preview_chars: int = PREVIEW_CHARS) -> dict:
    return {
        "document_id": record["document_id"],
        "cache_hit": cache_hit,
        "url": record.get("url"),
        "final_url": record.get("final_url"),
        "status": record.get("status"),
        "content_type": record.get("content_type"),
        "bytes": record.get("bytes"),
        "text_chars": record.get("text_chars"),
        "truncated": record.get("truncated", False),
        "title": record.get("title", ""),
        "pages": record.get("pages"),
        "extraction_path": record.get("extraction_path"),
        "text_preview": text[:preview_chars],
        "hint": QUERY_HINT,
    }


def _store(ctx, kind: str, url: str, fetched: dict) -> tuple[dict, str]:
    body, ctype = fetched["body"], fetched["content_type"]
    meta = {k: fetched[k] for k in ("status", "final_url", "redirects", "headers", "content_type", "truncated")}
    if _is_pdf(ctype, body):
        try:
            text, pdf_meta = pdf_text_and_meta(body)
            meta.update(pdf_meta, extraction_path="pdfplumber", doc_type="pdf")
        except Exception as exc:  # broken/truncated PDFs still get stored
            text = ""
            meta.update(extraction_path=f"pdfplumber_failed:{type(exc).__name__}", doc_type="pdf")
    else:
        decoded = _decode(body, ctype)
        head = decoded[:5000].lower()
        if "html" in ctype.lower() or "<html" in head or "<body" in head:
            text = visible_text(decoded)
            meta.update(title=html_title(decoded), extraction_path="html.visible_text", doc_type="html")
        else:
            text = decoded
            meta.update(extraction_path="raw_text", doc_type="text")
    record = ctx.cache.put(kind, url, body, meta, text)
    return record, text


def _fetch(ctx, kind: str, url: str) -> dict:
    url = check_url(url)
    cached = ctx.cache.lookup(kind, url)
    if cached:
        ctx.note_document(cached["document_id"], cache_hit=True)
        result = _summary(cached, ctx.cache.read_text(cached["document_id"]), cache_hit=True,
                          preview_chars=ctx.config.preview_chars)
    else:
        try:
            fetched = http_get(ctx, url)
        except requests.RequestException as exc:
            return {"error": type(exc).__name__, "message": str(exc)[:300], "url": url}
        record, text = _store(ctx, kind, url, fetched)
        ctx.note_document(record["document_id"], cache_hit=False)
        result = _summary(record, text, cache_hit=False, preview_chars=ctx.config.preview_chars)
    ctx.emit("document", document={k: v for k, v in result.items() if k != "text_preview"})
    return result


def fetch_url(ctx, url: str) -> dict:
    return _fetch(ctx, "fetch", url)


def fetch_pdf(ctx, url: str) -> dict:
    return _fetch(ctx, "pdf", url)

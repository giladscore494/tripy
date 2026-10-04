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
    # Single flight per (kind, URL): one worker downloads, concurrent workers wait and reuse the stored
    # document. Other URLs are not blocked.
    with ctx.cache.hold(f"doc:{kind}:{url}") as waited:
        cached = ctx.cache.lookup(kind, url)
        if cached:
            if waited:
                ctx.cache.count("cross_vehicle_document_singleflight_reuses")
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
    url = check_url(url)
    skipped = unreadable_result(ctx, url)
    if skipped is not None:
        return skipped
    return render_fallback(ctx, url, _fetch(ctx, "fetch", url))


def fetch_pdf(ctx, url: str) -> dict:
    url = check_url(url)
    skipped = unreadable_result(ctx, url)
    if skipped is not None:
        return skipped
    return _fetch(ctx, "pdf", url)


# --- PR #43 (H7): automatic render fallback for unreadable official / importer pages ----------------------------------
#
# An HTML fetch of an official or importer domain that returns a 2xx status with fewer than RENDER_FALLBACK_MIN_TEXT
# visible characters (a JavaScript shell or a bot challenge: audi.co.il returned ~584 bytes, 0 text chars for every URL)
# is re-fetched ONCE with render_page (headless Chromium, RENDER_FALLBACK_TIMEOUT_S budget, cached like a fetch:
# a later fetch reuses the rendered document). The decision is the code's, never the model's. If the rendered page is
# still empty or a challenge page, the domain is `unreadable` for the run: no further fetch / render is spent on it
# (counter acq_unreadable_domains). No stealth and no evasion: a challenge page stays unreadable.

_RENDER_UNAVAILABLE = False      # set once render_page reported that this host has no browser
RENDER_FALLBACK_MIN_TEXT = 500
RENDER_FALLBACK_TIMEOUT_S = 25.0
# a rendered page this short that carries one of these markers is a bot challenge / block page, not content
CHALLENGE_MAX_TEXT = 3000
CHALLENGE_MARKERS = ("just a moment", "checking your browser", "cf-browser-verification", "cf-challenge",
                     "challenge-platform", "attention required", "access denied", "request unsuccessful",
                     "_incapsula_resource", "px-captcha", "are you a robot", "verify you are human",
                     "please enable javascript", "bot detection", "captcha")


def _domain(url: str) -> str:
    host = (urlparse(str(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _unreadable(ctx) -> dict:
    found = getattr(ctx, "unreadable_domains", None)
    if found is None:
        found = {}
        try:
            ctx.unreadable_domains = found
        except AttributeError:
            pass
    return found


def unreadable_result(ctx, url: str) -> dict | None:
    """The refusal for a URL of a domain this run already found unreadable (None otherwise): no network is spent."""
    domain = _domain(url)
    reason = _unreadable(ctx).get(domain)
    if reason is None:
        return None
    ctx.counters["acq_unreadable_skips"] += 1
    return {"error": "domain_unreadable", "url": url, "domain": domain, "reason": reason,
            "message": f"{domain} returned no readable content in this run (even rendered); use other sources."}


def _official_domain(ctx, url: str) -> bool:
    from ..source_authority import OFFICIAL_CLASSES, classify_source

    manufacturer = getattr(getattr(ctx, "admission", None), "manufacturer", None) \
        or (getattr(ctx, "vehicle", None) or {}).get("manufacturer")
    return classify_source(url, manufacturer).get("source_authority") in OFFICIAL_CLASSES


def challenge_page(html: str, text_chars: int) -> bool:
    low = (html or "").lower()
    return text_chars < CHALLENGE_MAX_TEXT and any(marker in low for marker in CHALLENGE_MARKERS)


def render_fallback(ctx, url: str, result: dict) -> dict:
    """The fetch result, or, for an empty 2xx HTML page of an official / importer domain, its rendered version (see
    above). Never raises: a failure keeps the fetch result."""
    try:
        return _render_fallback(ctx, url, result)
    except Exception as exc:  # noqa: BLE001 - the fallback must never cost the fetch
        ctx.emit("rendered_fallback_failed", url=url, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return result


def _render_fallback(ctx, url: str, result: dict) -> dict:
    if not isinstance(result, dict) or result.get("error") or result.get("extraction_path") != "html.visible_text":
        return result
    status = result.get("status")
    if not (isinstance(status, int) and 200 <= status < 300) or (result.get("text_chars") or 0) >= RENDER_FALLBACK_MIN_TEXT:
        return result
    if not _official_domain(ctx, url):
        return result
    from .render import render_page

    global _RENDER_UNAVAILABLE
    if _RENDER_UNAVAILABLE:
        return result
    ctx.counters["acq_render_fallbacks"] += 1
    rendered = render_page(ctx, url, timeout_s=RENDER_FALLBACK_TIMEOUT_S)
    if isinstance(rendered, dict) and rendered.get("error") == "render_unavailable":
        # no browser on this host (a host fact, not a verdict on the site): the fetch stands, nothing is marked, and
        # no later fetch of this process tries again
        _RENDER_UNAVAILABLE = True
        ctx.emit("rendered_fallback", url=url, fetch_document_id=result.get("document_id"), readable=False,
                 render_error="render_unavailable", message=rendered.get("message"))
        return result
    text_chars = rendered.get("text_chars") or 0 if isinstance(rendered, dict) else 0
    html = ""
    if isinstance(rendered, dict) and rendered.get("document_id"):
        html = ctx.cache.read_body(rendered["document_id"]).decode("utf-8", errors="replace")
    challenge = challenge_page(html, text_chars)
    readable = isinstance(rendered, dict) and not rendered.get("error") and text_chars >= RENDER_FALLBACK_MIN_TEXT \
        and not challenge
    event = {"url": url, "fetch_document_id": result.get("document_id"), "fetch_text_chars": result.get("text_chars"),
             "rendered_document_id": rendered.get("document_id") if isinstance(rendered, dict) else None,
             "rendered_text_chars": text_chars, "readable": readable,
             "render_error": rendered.get("error") if isinstance(rendered, dict) else "no_result"}
    ctx.emit("rendered_fallback", **event)
    if readable:
        return {**rendered, "rendered_fallback": True, "fetch_document_id": result.get("document_id")}
    domain = _domain(url)
    reason = "challenge_page" if challenge else (rendered.get("error") if isinstance(rendered, dict)
                                                  and rendered.get("error") else "empty_after_render")
    found = _unreadable(ctx)
    if domain not in found:
        found[domain] = reason
        ctx.counters["acq_unreadable_domains"] = len(found)
        ctx.emit("domain_unreadable", domain=domain, url=url, reason=reason)
    return {**result, "rendered_fallback": True, "unreadable": reason,
            "message": f"{domain} returned no readable content even rendered ({reason}); use other sources."}

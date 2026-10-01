"""render_page: headless Chromium via Playwright, wrapped as one tool.

Playwright is optional. If it is not installed (or no browser is available) the
tool says so and the model can fall back to fetch_url.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from .extract import html_title, visible_text
from .fetch import QUERY_HINT, USER_AGENT, check_url


def _render(url: str, wait_ms: int, timeout_s: float) -> dict:
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="he-IL")
            response = page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=min(15000, timeout_s * 1000))
            except Exception:
                pass
            page.wait_for_timeout(max(0, min(int(wait_ms), 15000)))
            html = page.content()
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => ({text: (e.innerText||'').trim().slice(0,120), url: e.href}))")
            return {"status": response.status if response else None, "final_url": page.url,
                    "html": html, "links": links}
        finally:
            browser.close()


def render_page(ctx, url: str, wait_ms: int = 2500) -> dict:
    url = check_url(url)
    cached = ctx.cache.lookup("rendered", url)
    if cached:
        ctx.note_document(cached["document_id"], cache_hit=True)
        text = ctx.cache.read_text(cached["document_id"])
        links = ctx.cache.get_derived(cached["document_id"], "links") or []
        result = {"document_id": cached["document_id"], "cache_hit": True, "url": url,
                  "final_url": cached.get("final_url"), "status": cached.get("status"),
                  "title": cached.get("title", ""), "text_chars": len(text),
                  "text_preview": text[:ctx.config.preview_chars], "links": links[:ctx.config.max_links],
                  "links_total": len(links), "hint": QUERY_HINT}
        ctx.emit("document", document={k: v for k, v in result.items() if k not in ("text_preview", "links")})
        return result
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return {"error": "render_unavailable", "url": url,
                "message": "Playwright is not installed on this host. Use fetch_url instead."}
    try:
        # Run in a fresh thread: Playwright's sync API refuses threads with a running event loop.
        with ThreadPoolExecutor(max_workers=1) as pool:
            rendered = pool.submit(_render, url, wait_ms, ctx.config.render_timeout_s).result(
                timeout=ctx.config.render_timeout_s + 30)
    except Exception as exc:
        return {"error": "render_failed", "url": url, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
    html = rendered["html"]
    body = html.encode("utf-8")[: ctx.config.max_response_bytes]
    text = visible_text(html)
    links = [link for link in rendered["links"] if str(link.get("url", "")).startswith("http")]
    record = ctx.cache.put("rendered", url, body, {
        "status": rendered["status"], "final_url": rendered["final_url"], "content_type": "text/html (rendered)",
        "headers": {}, "title": html_title(html), "doc_type": "html", "extraction_path": "playwright.chromium",
        "truncated": len(html.encode("utf-8")) > ctx.config.max_response_bytes,
    }, text)
    ctx.cache.put_derived(record["document_id"], "links", links)
    ctx.note_document(record["document_id"], cache_hit=False)
    result = {"document_id": record["document_id"], "cache_hit": False, "url": url,
              "final_url": rendered["final_url"], "status": rendered["status"], "title": record["title"],
              "text_chars": len(text), "text_preview": text[:ctx.config.preview_chars],
              "links": links[:ctx.config.max_links], "links_total": len(links), "hint": QUERY_HINT}
    ctx.emit("document", document={k: v for k, v in result.items() if k not in ("text_preview", "links")})
    return result

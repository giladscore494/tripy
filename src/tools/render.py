"""render_page: headless Chromium via Playwright, wrapped as one tool.

Playwright is optional. If it is not installed (or no browser is available) the
tool says so and the model can fall back to fetch_url.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from .extract import html_title, visible_text
from .fetch import QUERY_HINT, USER_AGENT, check_url


BROWSER_MISSING = "Executable doesn't exist"


def _log():
    from ..server_logging import get_logger

    return get_logger("render")


def _launch_options() -> dict:
    launch = {"headless": True}
    if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
    return launch


def boot_check(timeout_s: float = 30.0) -> dict:
    """PR #44 (P5): does this host actually launch Chromium? {playwright, chromium_path, launch: ok | error, error?,
    browser_version?, js_ok?, ms}: the Playwright version, the browser executable it resolves, and a real headless launch
    that runs a line of JavaScript in a blank page. Never raises (run once at boot by src/startup_check.py)."""
    t0 = time.monotonic()
    out: dict = {"playwright": None, "chromium_path": None, "launch": "error"}
    try:
        from importlib.metadata import version

        out["playwright"] = version("playwright")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"playwright not installed: {type(exc).__name__}"
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out

    def probe() -> dict:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = _launch_options().get("executable_path") or p.chromium.executable_path
            found = {"chromium_path": path, "chromium_exists": os.path.exists(path)}
            browser = p.chromium.launch(**_launch_options())
            try:
                page = browser.new_page()
                page.set_content("<body><script>document.body.textContent = 'js:' + (6 * 7)</script></body>")
                found.update(browser_version=browser.version, js_ok=page.inner_text("body") == "js:42")
            finally:
                browser.close()
            return found

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            out.update(pool.submit(probe).result(timeout=timeout_s))
        out["launch"] = "ok"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


def _playwright_render(url: str, wait_ms: int, timeout_s: float) -> dict:
    from playwright.sync_api import sync_playwright

    launch = _launch_options()
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


_render = _playwright_render       # tests replace _render (tests/conftest.py); _playwright_render stays the real one


def render_page(ctx, url: str, wait_ms: int = 2500, timeout_s: float | None = None, *,
                challenge: bool = True) -> dict:
    """`challenge=False`: the caller (fetch.render_fallback) judges the rendered page itself."""
    url = check_url(url)
    from ..source_authority import fetch_allowed, note_policy_block, policy_refusal
    from .fetch import challenge_check, unreadable_result

    if not fetch_allowed(url):
        note_policy_block(ctx, url, "render")
        ctx.emit("policy_blocked", url=url, stage="render", domain=policy_refusal(url, "render")["domain"])
        return policy_refusal(url, "render")

    skipped = unreadable_result(ctx, url)
    if skipped is not None:
        _log().info("render_page url=%s outcome=skipped_unreadable reason=%s", url, skipped.get("reason"))
        return skipped
    t0 = time.monotonic()
    with ctx.cache.hold(f"doc:rendered:{url}") as waited:  # single flight per URL (see storage/cache.py)
        result = _render_page(ctx, url, wait_ms, waited, timeout_s)
    if challenge:
        result = challenge_check(ctx, url, result)
    # PR #44 (P5): one server-log line per render (url, status, text chars, ms, outcome)
    _log().info("render_page url=%s status=%s text_chars=%s ms=%d cache_hit=%s outcome=%s", url,
                result.get("status"), result.get("text_chars"), int((time.monotonic() - t0) * 1000),
                result.get("cache_hit"), result.get("error") or result.get("unreadable") or "ok")
    return result


def _render_page(ctx, url: str, wait_ms: int, waited: bool, timeout_s: float | None = None) -> dict:
    cached = ctx.cache.lookup("rendered", url)
    if cached and waited:
        ctx.cache.count("cross_vehicle_document_singleflight_reuses")
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
    budget = float(timeout_s or ctx.config.render_timeout_s)
    try:
        # Run in a fresh thread: Playwright's sync API refuses threads with a running event loop.
        with ThreadPoolExecutor(max_workers=1) as pool:
            rendered = pool.submit(_render, url, wait_ms, budget).result(timeout=budget + 30)
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        if BROWSER_MISSING in str(exc):
            # Playwright is installed but its browser is not (`playwright install chromium` never ran on this host)
            return {"error": "render_unavailable", "url": url, "message": message}
        return {"error": "render_failed", "url": url, "message": message}
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

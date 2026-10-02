"""search_web / search_official_domains.

Backends:
  * "glm"        — the GLM web_search API (same provider and key as the model)
  * "duckduckgo" — keyless HTML endpoint, useful when the GLM search API is not enabled

Results are returned in backend order with no reliability ranking.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .fetch import USER_AGENT

# Convenience defaults only. The model may pass any domains, and plain
# search_web is never restricted.
DEFAULT_OFFICIAL_DOMAINS: dict[str, list[str]] = {
    "טויוטה": ["toyota.co.il", "toyota-europe.com", "toyota.com", "global.toyota"],
    "אאודי": ["audi.co.il", "audi.com", "audi.de", "audi-mediacenter.com"],
    "ב מ וו": ["bmw.co.il", "bmw.com", "bmwgroup.com", "mini.co.il", "mini.com"],
    "מרצדס": ["mercedes-benz.co.il", "mercedes-benz.com", "mercedes-benz.de", "group.mercedes-benz.com"],
    "יונדאי": ["hyundai.co.il", "hyundai.com", "hyundai.news"],
    "קאדילאק": ["cadillac.co.il", "cadillac.com", "cadillaceurope.com", "news.gm.com"],
    "אקספנג": ["xpeng.com", "heyxpeng.com"],
}
MAX_DOMAINS = 6


def _ddg_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else href
    return href


def parse_duckduckgo(html: str, limit: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for node in soup.select(".result"):
        link = node.select_one("a.result__a")
        if not link or not link.get("href"):
            continue
        url = _ddg_url(link["href"])
        if "duckduckgo.com/y.js" in url:  # sponsored
            continue
        snippet = node.select_one(".result__snippet")
        results.append({"title": link.get_text(" ", strip=True), "url": url,
                        "snippet": snippet.get_text(" ", strip=True) if snippet else "", "site": urlparse(url).netloc,
                        "published": ""})
        if len(results) >= limit:
            break
    return results


def _duckduckgo(ctx, query: str, count: int, domain: str | None) -> list[dict]:
    q = f"site:{domain} {query}" if domain else query
    resp = ctx.session.get("https://html.duckduckgo.com/html/", params={"q": q},
                           headers={"User-Agent": USER_AGENT, "Accept-Language": "he,en;q=0.9"},
                           timeout=(ctx.config.connect_timeout_s, ctx.config.read_timeout_s))
    resp.raise_for_status()
    return parse_duckduckgo(resp.text, count)


def search_provider(ctx) -> tuple[str, str]:
    """(backend, engine): which provider answers a search. Part of the search cache key AND of a negative route's
    identity (src/research_memory.py), so the two can never disagree about what counts as the same search."""
    return ctx.config.search_backend, getattr(getattr(ctx.glm, "settings", None), "search_engine", "")


def search_key(ctx, query: str, count: int, domain: str | None) -> str:
    backend, engine = search_provider(ctx)
    return json.dumps([backend, engine, query, count, domain or ""], ensure_ascii=False)


def planned_searches(name: str, args: dict, defaults: list[str] | None = None) -> list[tuple[str, int, str | None]]:
    """The (query, effective count, domain) provider searches one search tool call makes, exactly as search_web /
    search_official_domains execute it (defaults and clamping applied): search_web one, count = max_results clamped
    to 1..20 (default 8); search_official_domains one per domain (its own list, else the manufacturer defaults, at
    most MAX_DOMAINS) with count 5, or one plain count-8 search when there is no domain. Invalid arguments: none.
    Shared by the search budget and negative route memory."""
    args = args if isinstance(args, dict) else {}
    query = str(args.get("query") or "")
    if not query:
        return []
    try:
        if name == "search_web":
            return [(query, max(1, min(int(args.get("max_results") or 8), 20)), args.get("domain") or None)]
        if name == "search_official_domains":
            domains = args.get("domains") or defaults or []
            if isinstance(domains, str):
                domains = domains.split(",")
            chosen = [str(d).strip().lower() for d in domains if d and str(d).strip()][:MAX_DOMAINS]
            return [(query, 5, d) for d in chosen] or [(query, 8, None)]
    except (TypeError, ValueError):     # invalid arguments: dispatch refuses the call, no provider search happens
        return []
    return []


def planned_provider_calls(ctx, name: str, args: dict) -> int:
    """How many underlying provider searches this call would make right now: one per (query, domain) not already
    in the shared search cache. search_official_domains searches each of its domains separately."""
    return sum(1 for query, count, domain in planned_searches(name, args, default_domains(ctx.vehicle))
               if not ctx.cache.has_search(search_key(ctx, query, count, domain)))


def _run_search(ctx, query: str, count: int, domain: str | None) -> tuple[list[dict], bool]:
    backend = ctx.config.search_backend
    key = search_key(ctx, query, count, domain)

    def network() -> list[dict]:
        # Runs at most once per key across all concurrent workers (single flight in the cache).
        ctx.counters["search_cache_misses"] += 1
        if backend == "glm":
            if ctx.glm is None:
                raise RuntimeError("GLM search backend selected but no GLM client is configured")
            try:
                results = ctx.glm.web_search(query, count=count, domain=domain)
            except Exception:
                ctx.counters["search_api_errors"] += 1
                raise
            ctx.counters["search_api_calls"] += 1  # billable GLM web_search calls (cache hits excluded)
            return results
        if backend == "duckduckgo":
            return _duckduckgo(ctx, query, count, domain)
        raise ValueError(f"Unknown search backend {backend!r}")

    results, hit = ctx.cache.search_singleflight(key, network)
    if hit:
        ctx.counters["search_cache_hits"] += 1
    return results, hit


def search_web(ctx, query: str, max_results: int = 8, domain: str | None = None) -> dict:
    count = max(1, min(int(max_results or 8), 20))
    results, hit = _run_search(ctx, query, count, domain)
    ctx.emit("search", query=query, domain=domain, backend=ctx.config.search_backend,
             result_count=len(results), cache_hit=hit)
    return {"query": query, "domain": domain, "backend": ctx.config.search_backend, "cache_hit": hit,
            "results": results}


def default_domains(vehicle: dict) -> list[str]:
    manufacturer = (vehicle or {}).get("manufacturer") or (vehicle or {}).get("tozar") or ""
    return list(DEFAULT_OFFICIAL_DOMAINS.get(manufacturer.strip(), []))


def search_official_domains(ctx, query: str, domains: list[str] | None = None) -> dict:
    chosen = [d.strip().lower() for d in (domains or default_domains(ctx.vehicle)) if d and d.strip()]
    chosen = chosen[:MAX_DOMAINS]
    if not chosen:
        out = search_web(ctx, query)
        out["note"] = "No default domains for this manufacturer; ran a plain web search."
        return out
    merged, seen, per_domain = [], set(), {}
    for domain in chosen:
        try:
            results, hit = _run_search(ctx, query, 5, domain)
        except Exception as exc:
            per_domain[domain] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            continue
        per_domain[domain] = {"result_count": len(results), "cache_hit": hit}
        for item in results:
            if item.get("url") and item["url"] not in seen:
                seen.add(item["url"])
                merged.append({**item, "domain_filter": domain})
    ctx.emit("search", query=query, domains=chosen, backend=ctx.config.search_backend, result_count=len(merged))
    return {"query": query, "domains_used": chosen, "per_domain": per_domain, "results": merged,
            "note": "Domain bias is a convenience. Sources outside these domains are equally usable."}

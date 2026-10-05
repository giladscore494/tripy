"""search_web / search_official_domains.

Backends (src/tools/search_backends, PR #46): "glm" (the GLM web_search API, search-prime), "serper" (Google results),
"gemini" (Gemini grounding, URLs only) and "duckduckgo" (keyless HTML). The run's `search_backend` answers every search;
its optional `search_fallback_backend` answers a search only when the primary returned 0 usable results (after the
root-only / site sanity of search_backends.sanitize), logged as `search_fallback_used`. A backend without its key is
refused (BackendUnavailable), never swapped.

Results are returned in backend order with no reliability ranking.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup


# Convenience defaults only. The model may pass any domains, and plain
# search_web is never restricted. Global manufacturer / media domains only: the target-market IMPORTER domains are
# derived from data/source_rules.json (importer_domains), so a brand's importer site is never missing from a hand list.
GLOBAL_OFFICIAL_DOMAINS: dict[str, list[str]] = {
    "טויוטה": ["toyota-europe.com", "toyota.com", "global.toyota"],
    "אאודי": ["audi.com", "audi.de", "audi-mediacenter.com"],
    "ב מ וו": ["bmw.com", "bmwgroup.com", "mini.com"],
    "מרצדס": ["mercedes-benz.com", "mercedes-benz.de", "group.mercedes-benz.com"],
    "יונדאי": ["hyundai.com", "hyundai.news"],
    "קאדילאק": ["cadillac.com", "cadillaceurope.com", "news.gm.com"],
    "אקספנג": ["xpeng.com", "heyxpeng.com"],
}


def importer_domains(manufacturer: str | None, rule_set: dict | None = None) -> list[str]:
    """The brand's target-market importer domains: every importer slug (data/source_rules.json brands.<brand>.
    importer_slugs) under every importer TLD, with the TLD's commercial second level when it has one (il -> co.il):
    heyxpeng -> heyxpeng.co.il."""
    from ..source_authority import _brand, rules

    rs = rule_set if rule_set is not None else rules()
    two_level = set(rs.get("two_level_public_suffixes") or [])
    out: list[str] = []
    for slug in _brand(rs, manufacturer).get("importer_slugs") or []:
        for tld in rs.get("importer_tlds") or []:
            suffix = f"co.{tld}" if f"co.{tld}" in two_level else tld
            out.append(f"{str(slug).lower()}.{suffix}")
    return list(dict.fromkeys(out))


def official_domain_defaults(manufacturer: str | None) -> list[str]:
    """Importer domains first (derived), then the brand's global domains."""
    key = str(manufacturer or "").strip()
    return list(dict.fromkeys(importer_domains(key) + GLOBAL_OFFICIAL_DOMAINS.get(key, [])))


MAX_DOMAINS = 6
DEFAULT_COUNTRY, DEFAULT_LANG = "il", "he"


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


def search_provider(ctx, backend: str | None = None) -> tuple[str, str]:
    """(backend, engine): which provider answers a search. Part of the search cache key AND of a negative route's
    identity (src/research_memory.py), so the two can never disagree about what counts as the same search."""
    backend = backend or ctx.config.search_backend
    if backend == "glm":
        return backend, getattr(getattr(ctx.glm, "settings", None), "search_engine", "")
    if backend == "gemini":
        from .search_backends import backend_settings

        return backend, str(backend_settings("gemini").get("model") or "")
    return backend, ""


def search_key(ctx, query: str, count: int, domain: str | None, backend: str | None = None,
               country: str = DEFAULT_COUNTRY, lang: str = DEFAULT_LANG) -> str:
    """The search cache key: (backend, engine, query, count, site), plus country / lang when they are not the engine's
    defaults (so every key cached before PR #46 stays valid)."""
    backend, engine = search_provider(ctx, backend)
    parts = [backend, engine, query, count, domain or ""]
    if (country, lang) != (DEFAULT_COUNTRY, DEFAULT_LANG):
        parts += [country, lang]
    return json.dumps(parts, ensure_ascii=False)


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


def _backend(ctx, name: str):
    from .search_backends import make_backend

    return make_backend(name, session=ctx.session, glm=ctx.glm,
                        timeout=(ctx.config.connect_timeout_s, ctx.config.read_timeout_s))


def _provider_search(ctx, backend: str, query: str, count: int, domain: str | None) -> tuple[list[dict], bool, dict]:
    """(usable results, cache hit, sanity) of one backend: the provider's results cached per (backend, query, site, ...)
    then the root-only / site sanity applied."""
    from .search_backends import BILLABLE, sanitize

    key = search_key(ctx, query, count, domain, backend)
    calls: list = []

    def network() -> list[dict]:
        # Runs at most once per key across all concurrent workers (single flight in the cache).
        ctx.counters["search_cache_misses"] += 1
        try:
            results, info = _backend(ctx, backend).query(query, site=domain, count=count)
        except Exception:
            ctx.counters["search_api_errors"] += 1
            raise
        if backend in BILLABLE and info.billable:
            ctx.counters["search_api_calls"] += 1  # billable provider searches (cache hits excluded)
        ctx.counters[f"search_calls:{backend}"] += 1
        if info.usd:
            ctx.counters[f"search_usd:{backend}"] += info.usd
        if backend == "gemini":
            ctx.counters["gemini_redirects_resolved"] += info.redirects_resolved
            ctx.counters["gemini_redirects_failed"] += info.redirects_failed
        calls.append(info)
        return [r.as_dict() for r in results]

    results, hit = ctx.cache.search_singleflight(key, network)
    if hit:
        ctx.counters["search_cache_hits"] += 1
    usable, sanity = sanitize(results, backend=backend, site=domain, query=query)
    if sanity["root_only"]:
        ctx.counters["search_root_only_urls"] += len(sanity["root_only"])
        ctx.emit("root_only_url", backend=backend, query=query, site=sanity["site"], results=sanity["root_only"])
    if sanity["off_site"]:
        ctx.counters["search_off_site"] += sanity["off_site"]
    info = calls[0] if calls else None
    sanity = {"site": sanity["site"], "root_only": len(sanity["root_only"]), "off_site": sanity["off_site"],
              "raw_count": len(results)}
    if info is not None:
        sanity.update({"usd": info.usd, "latency_ms": info.latency_ms})
        if info.queries and backend == "gemini":
            sanity["web_search_queries"] = info.queries
        if backend == "gemini":
            sanity.update({"redirects_resolved": info.redirects_resolved, "redirects_failed": info.redirects_failed})
    if info is not None or not hit:
        ctx.emit("search_backend_call", backend=backend, query=query, cache_hit=hit, **sanity)
    return usable, hit, sanity


def _run_search(ctx, query: str, count: int, domain: str | None) -> tuple[list[dict], bool]:
    results, hit, _ = _run_search_info(ctx, query, count, domain)
    return results, hit


def _run_search_info(ctx, query: str, count: int, domain: str | None) -> tuple[list[dict], bool, dict]:
    """The run's search: the primary backend; the fallback backend only when the primary returned 0 usable results.
    {backend, fallback_used, ...sanity} describes what answered."""
    primary = ctx.config.search_backend
    results, hit, info = _provider_search(ctx, primary, query, count, domain)
    info = {"backend": primary, **info}
    fallback = getattr(ctx.config, "search_fallback_backend", "") or ""
    if not results and fallback and fallback not in (primary, "none"):
        results, hit, extra = _provider_search(ctx, fallback, query, count, domain)
        ctx.counters["search_fallback_used"] += 1
        ctx.emit("search_fallback_used", primary=primary, fallback=fallback, query=query, site=domain,
                 result_count=len(results))
        info = {"backend": fallback, "fallback_used": True, "primary": primary, **extra}
    return results, hit, info


def search_web(ctx, query: str, max_results: int = 8, domain: str | None = None) -> dict:
    count = max(1, min(int(max_results or 8), 20))
    results, hit, info = _run_search_info(ctx, query, count, domain)
    ctx.emit("search", query=query, domain=domain, backend=info["backend"], result_count=len(results), cache_hit=hit,
             **({"fallback_used": True, "primary_backend": info["primary"]} if info.get("fallback_used") else {}))
    out = {"query": query, "domain": domain, "backend": info["backend"], "cache_hit": hit, "results": results}
    if info.get("fallback_used"):
        out["fallback_used"] = True
    return out


def default_domains(vehicle: dict) -> list[str]:
    manufacturer = (vehicle or {}).get("manufacturer") or (vehicle or {}).get("tozar") or ""
    return official_domain_defaults(manufacturer)


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
            results, hit, info = _run_search_info(ctx, query, 5, domain)
        except Exception as exc:
            per_domain[domain] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            continue
        per_domain[domain] = {"result_count": len(results), "cache_hit": hit, "backend": info["backend"]}
        for item in results:
            if item.get("url") and item["url"] not in seen:
                seen.add(item["url"])
                merged.append({**item, "domain_filter": domain})
    ctx.emit("search", query=query, domains=chosen, backend=ctx.config.search_backend, result_count=len(merged))
    return {"query": query, "domains_used": chosen, "per_domain": per_domain, "results": merged,
            "note": "Domain bias is a convenience. Sources outside these domains are equally usable."}

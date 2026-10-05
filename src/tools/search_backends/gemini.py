"""gemini: Grounding with Google Search through the Gemini API, used as a URL finder only.

    request    POST models/gemini-3.8-flash:generateContent with the `google_search` tool and a neutral instruction
               ("find the web pages for: {query}"); no JSON schema / structured output (Gemini 3.x silently disables
               grounding, or returns empty grounding metadata, when the two are combined)
    URLs       read ONLY from candidates[0].groundingMetadata.groundingChunks[].web.{uri, title}. The model's text is
               discarded: it is never parsed for URLs or values
    redirects  a chunk URI is a vertexaisearch.cloud.google.com/grounding-api-redirect/... link: each is resolved to its
               target with ONE request that does not download the body (HEAD, else GET with redirects not followed and
               the body never read; the `Location` header). The redirect is kept as raw_url. Resolved / failed are
               counted (gemini_redirects_resolved / gemini_redirects_failed); a failed one is dropped
    cost       tokens (usageMetadata) + billed search queries (groundingMetadata.webSearchQueries), prices from
               data/search_backends.json; webSearchQueries are recorded in the search event

Google's display requirements for grounded results (Search Suggestions) apply where a UI shows Gemini search results
to a user. The engine never shows them: the URLs feed fetches and the resolver only (README, "Search backends").
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import CallInfo, SearchResult, backend_settings, site_of_query
from .base import Backend

DEFAULT_MODEL = "gemini-3.8-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)
INSTRUCTION = "find the web pages for: {query}"


def request_body(query: str, site: str | None = None) -> dict:
    """The neutral grounded request: the google_search tool, no response schema, no structured output."""
    q = query if (not site or site_of_query(query)) else f"site:{site} {query}"
    return {"contents": [{"role": "user", "parts": [{"text": INSTRUCTION.format(query=q)}]}],
            "tools": [{"google_search": {}}]}


def grounding(data: dict) -> dict:
    """{chunks: [{uri, title}], queries: [...], usage: {...}} of a generateContent response. The candidate's text is
    never read."""
    candidates = (data or {}).get("candidates") or []
    meta = (candidates[0] or {}).get("groundingMetadata") if candidates and isinstance(candidates[0], dict) else None
    meta = meta if isinstance(meta, dict) else {}
    chunks = []
    for chunk in meta.get("groundingChunks") or []:
        web = (chunk or {}).get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict) and web.get("uri"):
            chunks.append({"uri": str(web["uri"]), "title": str(web.get("title") or "")})
    return {"chunks": chunks, "queries": [str(q) for q in meta.get("webSearchQueries") or [] if q],
            "usage": dict((data or {}).get("usageMetadata") or {})}


def is_redirect(uri: str) -> bool:
    host = urlparse(uri).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in REDIRECT_HOSTS)


def resolve_redirect(session, uri: str, timeout) -> str | None:
    """The target of a grounding redirect: the `Location` of ONE request that never downloads the body (HEAD; a server
    refusing HEAD gets a GET with redirects off whose body is never read). None when there is no Location."""
    try:
        resp = session.head(uri, allow_redirects=False, timeout=timeout)
        location = resp.headers.get("Location") or resp.headers.get("location")
        if location:
            return location
        if resp.status_code not in (405, 501):
            return None
    except Exception:  # noqa: BLE001 - one failed redirect costs only its result
        pass
    try:
        resp = session.get(uri, allow_redirects=False, stream=True, timeout=timeout)
        try:
            return resp.headers.get("Location") or resp.headers.get("location") or None
        finally:
            resp.close()
    except Exception:  # noqa: BLE001
        return None


def cost(usage: dict, billed_queries: int, cfg: dict | None = None) -> float:
    cfg = cfg if cfg is not None else backend_settings("gemini")
    prompt = float(usage.get("promptTokenCount") or 0)
    output = float(usage.get("candidatesTokenCount") or 0) + float(usage.get("thoughtsTokenCount") or 0)
    usd = prompt * float(cfg.get("input_usd_per_mtok") or 0) / 1e6 + output * float(cfg.get("output_usd_per_mtok")
                                                                                     or 0) / 1e6
    return round(usd + billed_queries * float(cfg.get("usd_per_search_query") or 0), 6)


class GeminiBackend(Backend):
    name = "gemini"

    def __init__(self, session, api_key: str, timeout=(10.0, 40.0)):
        self.session, self.api_key, self.timeout = session, api_key, timeout

    def query(self, query, *, site=None, country="il", lang="he", count=10):
        cfg = backend_settings(self.name)
        model = cfg.get("model") or DEFAULT_MODEL
        t0 = self.started()
        resp = self.session.post((cfg.get("endpoint") or ENDPOINT).format(model=model), json=request_body(query, site),
                                 headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                                 timeout=self.timeout)
        resp.raise_for_status()
        found = grounding(resp.json())
        info = CallInfo(backend=self.name, queries=found["queries"], usage=found["usage"])
        results, seen = [], set()
        for chunk in found["chunks"]:
            raw = chunk["uri"]
            url = raw
            if is_redirect(raw):
                url = resolve_redirect(self.session, raw, self.timeout)
                if not url:
                    info.redirects_failed += 1
                    continue
                info.redirects_resolved += 1
            if url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(url=url, title=chunk["title"], snippet="", rank=len(results) + 1,
                                        backend=self.name, raw_url=raw, site_native=bool(site or site_of_query(query))))
            if len(results) >= count:
                break
        info.usd = cost(found["usage"], len(found["queries"]), cfg)
        info.latency_ms = self.elapsed_ms(t0)
        return results, info
